import asyncio
import io
import sqlite3

import httpx
import pytest

from control_plane.services import knowledge_chunks as kc, knowledge_sources as ks


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


# ---- chunking ---------------------------------------------------------------------------------------------------------------------------------------
def test_prose_is_cut_along_headings_and_each_chunk_carries_its_heading_and_a_citation():
    text = "Intro line.\n\n# Custodian codes\nSCHWAB is code 1.\n\nFIDELITY is code 2.\n\n## Households\nA household groups clients."
    chunks = kc.chunk_text(text)
    assert [c["citation"] for c in chunks] == ["Part 1", "Section: Custodian codes", "Section: Households"]
    assert chunks[1]["text"].startswith("Custodian codes\nSCHWAB is code 1.") and "FIDELITY is code 2." in chunks[1]["text"]
    assert kc.chunk_text("") == [] and kc.chunk_text("   \n\n ") == []


def test_a_long_section_is_split_into_pieces_that_overlap_and_stay_within_the_limit():
    sentence = "The account number identifies one custodial account. "
    chunks = kc.chunk_text("# Accounts\n" + sentence * 120)
    assert len(chunks) > 1 and all(len(c["text"]) <= kc.MAX_CHARS + 20 for c in chunks)
    assert all(c["citation"] == "Section: Accounts" and c["text"].startswith("Accounts\n") for c in chunks)
    one_word = "x" * 5000
    assert len(kc.chunk_text(one_word)) >= 3  # even text with no break is cut


def test_table_rows_become_labelled_lines_with_row_range_citations_and_empty_cells_are_skipped():
    rows = [["A-1", "Ann", "", "SCHWAB"], ["A-2", "Bo", "nan", ""], ["", "", "", ""], ["A-4", "Cy", "x", "FIDELITY"]]
    chunks = kc.chunk_rows("Accounts", ["ACCT", "NAME", "NOTE", "CUSTODIAN"], rows)
    assert len(chunks) == 1 and chunks[0]["citation"] == "Sheet Accounts, rows 2-5"
    assert chunks[0]["text"].startswith("Sheet Accounts. Columns: ACCT, NAME, NOTE, CUSTODIAN.")
    assert "ACCT=A-1; NAME=Ann; CUSTODIAN=SCHWAB" in chunks[0]["text"] and "NOTE=nan" not in chunks[0]["text"] and "ACCT=A-4; NAME=Cy; NOTE=x; CUSTODIAN=FIDELITY" in chunks[0]["text"]
    many = kc.chunk_rows("S", ["ID", "TEXT"], [[str(i), "word " * 60] for i in range(60)])
    assert len(many) > 1 and many[0]["citation"].startswith("Sheet S, rows 2-") and many[1]["citation"].split("rows ")[1].split("-")[0] == str(int(many[0]["citation"].split("-")[-1]) + 1)


def _workbook():
    from openpyxl import Workbook
    wb = Workbook()
    a = wb.active
    a.title = "Accounts"
    a.append(["ACCT", "CUSTODIAN"])
    a.append(["A-1", "SCHWAB"])
    b = wb.create_sheet("Codes")
    b.append(["CODE", "MEANING"])
    b.append(["1", "Schwab"])
    b.append(["2", "Fidelity"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_files_of_every_kind_are_read_for_their_content_not_just_their_headers(monkeypatch):
    xlsx = kc.chunks_for_file("dictionary.xlsx", _workbook())
    assert [c["citation"] for c in xlsx["chunks"]] == ["Sheet Accounts, rows 2-2", "Sheet Codes, rows 2-3"] and "CODE=2; MEANING=Fidelity" in xlsx["chunks"][1]["text"]
    csv = kc.chunks_for_file("a.csv", b"ACCT,NAME\nA-1,Ann\nA-2,Bo\n")
    assert csv["chunks"][0]["citation"] == "Sheet a.csv, rows 2-3" and "ACCT=A-2; NAME=Bo" in csv["chunks"][0]["text"]
    tsv = kc.chunks_for_file("a.tsv", b"ACCT\tNAME\nA-1\tAnn\n")
    assert "NAME=Ann" in tsv["chunks"][0]["text"]
    md = kc.chunks_for_file("notes.md", b"# Rules\nCustodian is required.")
    assert md["chunks"][0]["citation"] == "Section: Rules"
    assert "account_number" in kc.chunks_for_file("s.json", b'{"fields": ["account_number"]}')["chunks"][0]["text"]
    assert "key: value" in kc.chunks_for_file("m.yaml", b"key: value\n")["chunks"][0]["text"]
    monkeypatch.setattr(kc.document_text, "pdf_pages", lambda content: ["First page text.", "", "Third page text."])
    pdf = kc.chunks_for_file("spec.pdf", b"%PDF")
    assert [c["citation"] for c in pdf["chunks"]] == ["Page 1", "Page 3"]
    monkeypatch.setattr(kc.document_text, "extract", lambda suffix, content: "# Slide 1\nHello")
    assert kc.chunks_for_file("deck.pptx", b"x")["chunks"][0]["text"].endswith("Hello")


def test_files_that_cannot_be_read_are_refused_with_a_reason_and_big_ones_are_cut_and_say_so(monkeypatch):
    for name, content, why in (("a.exe", b"x", "Supported files"), ("a.md", b"", "empty"), ("a.json", b"{bad", "not valid"), ("a.md", b"  \n ", "no readable text"),
                               ("a.xlsx", b"not a workbook", "could not be read")):
        with pytest.raises(ValueError, match=why):
            kc.chunks_for_file(name, content)
    monkeypatch.setattr(kc, "MAX_ROWS_PER_SHEET", 3)
    cut = kc.chunks_for_file("big.csv", b"A,B\n" + b"1,2\n" * 10)
    assert cut["truncated"] and "first 3 rows" in cut["note"] and "rows 2-4" in cut["chunks"][0]["citation"]
    monkeypatch.setattr(kc, "MAX_CHUNKS", 2)
    many = kc.chunks_for_file("m.md", ("# H\npara\n\n" * 1).encode() + b"\n".join(b"# S%d\ntext" % i for i in range(6)))
    assert len(many["chunks"]) == 2 and many["truncated"] and "first 2 chunks" in many["note"]
    monkeypatch.setattr(kc, "MAX_BYTES", 5)
    with pytest.raises(ValueError, match="20 MB"):
        kc.chunks_for_file("a.md", b"123456")


def test_the_store_replaces_an_assets_chunks_tags_entities_and_hides_retired_assets(conn):
    chunks = [{"text": "An Account belongs to a Household.", "citation": "Part 1"}, {"text": "Nothing relevant.", "citation": "Part 2"}]
    n = kc.index_asset(conn, migration_id="M1", scope="firm", asset_id="M1:DOC-001", asset_name="a.md", source="file", source_ref="a.md", version="v1", chunks=chunks,
                       entity_names=["Account", "Household", "Client"])
    assert n == 2
    stored = kc.chunks_of(conn, "M1:DOC-001")
    assert stored[0]["entities"] == "Account,Household" and stored[1]["entities"] is None and stored[0]["citation"] == "Part 1"
    kc.index_asset(conn, migration_id="M1", scope="firm", asset_id="M1:DOC-001", asset_name="a.md", source="web", source_ref="u", version="v2", chunks=chunks[:1])
    assert kc.stats(conn, "M1")["M1:DOC-001"] == {"chunks": 1, "chars": len(chunks[0]["text"]), "version": "v2", "source": "web"}  # replaced, not added to
    assert kc.stats(conn, "M2") == {}
    assert kc.retire_asset(conn, "M1:DOC-001") == 1 and kc.stats(conn, "M1") == {} and kc.chunks_of(conn, "M1:DOC-001") == []
    with pytest.raises(ValueError):
        kc.index_asset(conn, migration_id="M1", scope="other", asset_id="x", asset_name="x", source="file", source_ref="", version="", chunks=[])


# ---- web links --------------------------------------------------------------------------------------------------------------------------------------
PUBLIC = lambda host, port, **k: [(0, 0, 0, "", ("93.184.216.34", 443))]  # noqa: E731


def _page(body=b"<html><head><title>Guide</title></head><body><h1>Codes</h1><p>SCHWAB is 1</p></body></html>", kind="text/html; charset=utf-8", status=200, extra=None):
    return status, {"content-type": kind, **(extra or {})}, body


def test_only_admin_approved_plain_host_names_can_be_added(conn):
    assert ks.add_domain(conn, "Admin", " Docs.Example.com. ") == "docs.example.com"
    for bad, why in (("docs.example.com", "already"), ("localhost", "host name"), ("10.0.0.5", "host name"), ("example", "host name"), ("a b.com", "host name"),
                     ("https://docs.example.com/x", "host name"), ("", "host name")):
        with pytest.raises(ks.SourceError, match=why):
            ks.add_domain(conn, "Admin", bad)
    assert [d["host"] for d in ks.list_domains(conn)] == ["docs.example.com"]
    ks.remove_domain(conn, "docs.example.com")
    with pytest.raises(ks.SourceError):
        ks.remove_domain(conn, "docs.example.com")


def test_a_page_is_read_as_text_with_headings_from_an_approved_public_https_host(conn):
    ks.add_domain(conn, "Admin", "example.com")
    got = ks.fetch_web(conn, "https://docs.example.com/guide", getter=lambda u: _page(), resolver=PUBLIC)  # a subdomain of an approved host counts
    assert got["title"] == "Guide" and got["text"] == "# Codes\nSCHWAB is 1" and len(got["version"]) == 12
    assert ks.fetch_web(conn, "https://example.com/x.md", getter=lambda u: _page(b"# T\nbody", "text/markdown"), resolver=PUBLIC)["text"] == "# T\nbody"
    assert ks.fetch_web(conn, "https://example.com/a.txt", getter=lambda u: _page(b"plain", "text/plain; charset=latin-1"), resolver=PUBLIC)["title"] == "a.txt"
    assert ks.fetch_web(conn, "https://example.com/g", getter=lambda u: _page(), resolver=PUBLIC)["version"] == got["version"]  # the same text gives the same version


def test_a_web_link_is_refused_unless_https_approved_public_small_and_readable(conn, monkeypatch):
    ks.add_domain(conn, "Admin", "example.com")
    ok = lambda u: _page()  # noqa: E731
    cases = [("http://example.com/x", "https link", ok, PUBLIC), ("https://user:pw@example.com/x", "https link", ok, PUBLIC), ("https://example.com:8443/x", "https link", ok, PUBLIC),
             ("ftp://example.com/x", "https link", ok, PUBLIC), ("https://other.org/x", "not an approved web source", ok, PUBLIC),
             ("https://notexample.com/x", "not an approved web source", ok, PUBLIC),
             ("https://example.com/x", "public address", ok, lambda h, p, **k: [(0, 0, 0, "", ("127.0.0.1", 443))]),
             ("https://example.com/x", "public address", ok, lambda h, p, **k: [(0, 0, 0, "", ("93.184.216.34", 443)), (0, 0, 0, "", ("10.1.2.3", 443))]),
             ("https://example.com/x", "public address", ok, lambda h, p, **k: [(0, 0, 0, "", ("169.254.169.254", 443))]),
             ("https://example.com/x", "public address", ok, lambda h, p, **k: [(0, 0, 0, "", ("::1", 443, 0, 0))]),
             ("https://example.com/x", "look up", ok, lambda h, p, **k: (_ for _ in ()).throw(OSError("dns"))),
             ("https://example.com/x", "HTTP 404", lambda u: _page(status=404), PUBLIC),
             ("https://example.com/x", "larger than 2 MB", lambda u: _page(b"x" * (ks.MAX_BYTES + 1), "text/plain"), PUBLIC),
             ("https://example.com/x", "Only HTML", lambda u: _page(b"MZ", "application/octet-stream"), PUBLIC),
             ("https://example.com/x", "no readable text", lambda u: _page(b"<html><script>x</script></html>"), PUBLIC),
             ("https://example.com/x", "Could not reach", lambda u: (_ for _ in ()).throw(httpx.ConnectError("x")), PUBLIC)]
    for url, why, getter, resolver in cases:
        with pytest.raises(ks.SourceError, match=why):
            ks.fetch_web(conn, url, getter=getter, resolver=resolver)


def test_redirects_are_followed_by_hand_and_every_hop_is_checked_again(conn):
    ks.add_domain(conn, "Admin", "example.com")
    hops = {"https://example.com/a": _page(b"", "text/html", 302, {"location": "/b"}), "https://example.com/b": _page()}
    assert ks.fetch_web(conn, "https://example.com/a", getter=lambda u: hops[u], resolver=PUBLIC)["url"] == "https://example.com/b"
    elsewhere = lambda u: _page(b"", "text/html", 302, {"location": "https://evil.org/x"})  # noqa: E731
    with pytest.raises(ks.SourceError, match="not an approved web source"):
        ks.fetch_web(conn, "https://example.com/a", getter=elsewhere, resolver=PUBLIC)
    downgrade = lambda u: _page(b"", "text/html", 301, {"location": "http://example.com/x"})  # noqa: E731
    with pytest.raises(ks.SourceError, match="https link"):
        ks.fetch_web(conn, "https://example.com/a", getter=downgrade, resolver=PUBLIC)
    loop = lambda u: _page(b"", "text/html", 302, {"location": "https://example.com/a"})  # noqa: E731
    with pytest.raises(ks.SourceError, match="too many times"):
        ks.fetch_web(conn, "https://example.com/a", getter=loop, resolver=PUBLIC)
    with pytest.raises(ks.SourceError, match="without saying where"):
        ks.fetch_web(conn, "https://example.com/a", getter=lambda u: _page(b"", "text/html", 302), resolver=PUBLIC)


# ---- Confluence -------------------------------------------------------------------------------------------------------------------------------------
GOOD = {"base_url": "https://acme.atlassian.net/wiki/", "auth": "basic", "email": "svc@acme.example", "token_env_var": "CONFLUENCE_API_TOKEN", "allowed_spaces": "DATA, mig", "page_cap": "5"}


def test_confluence_settings_are_validated_and_the_token_is_never_stored(conn, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "s3cret-token-value")
    cfg = ks.save_config(conn, "Admin", GOOD)
    assert cfg["base_url"] == "https://acme.atlassian.net/wiki" and cfg["allowed_spaces"] == ["DATA", "mig"] and cfg["page_cap"] == 5
    assert cfg["status"] == "NOT_TESTED" and cfg["token_present"] is True and cfg["token_env_var"] == "CONFLUENCE_API_TOKEN"
    stored = " ".join(str(v) for r in conn.execute("SELECT * FROM confluence_connection").fetchall() for v in tuple(r))
    assert "s3cret-token-value" not in stored  # only the variable's name is kept
    assert ks.save_config(conn, "Admin", {**GOOD, "auth": "bearer", "email": ""})["email"] is None  # a personal access token needs no email
    for over, why in (({"base_url": "http://acme.atlassian.net"}, "https address"), ({"base_url": "https://u:p@acme.atlassian.net"}, "https address"), ({"auth": "none"}, "Sign in"),
                      ({"email": "nope"}, "email"), ({"token_env_var": "ghp_abcdef123456"}, "starts with CONFLUENCE_"), ({"token_env_var": "PATH"}, "starts with CONFLUENCE_"),
                      ({"allowed_spaces": ""}, "space keys"), ({"allowed_spaces": "A B$"}, "space keys"), ({"page_cap": "0"}, "between 1 and 500"), ({"page_cap": "x"}, "number")):
        with pytest.raises(ks.SourceError, match=why):
            ks.save_config(conn, "Admin", {**GOOD, **over})
    assert ks.save_config(conn, "Admin", {**GOOD, "token_env_var": ""})["token_env_var"] == ks.DEFAULT_TOKEN_ENV


def _confluence(spaces=None):
    """A fake Confluence: pages by id with parents, answering the three calls the importer makes."""
    pages = {"1": {"title": "Root", "space": "DATA", "version": 3, "body": "<p>Root text</p>", "parent": None},
             "2": {"title": "Child A", "space": "DATA", "version": 1, "body": "<p>Child A text</p>", "parent": "1"},
             "3": {"title": "Grandchild", "space": "DATA", "version": 2, "body": "<h2>Codes</h2><p>1 = Schwab</p>", "parent": "2"},
             "9": {"title": "Other space", "space": "OPS", "version": 1, "body": "<p>secret</p>", "parent": None},
             "4": {"title": "Empty", "space": "DATA", "version": 1, "body": "", "parent": "1"}}
    calls = []

    def one(pid):
        p = pages[pid]
        return {"id": pid, "title": p["title"], "space": {"key": p["space"]}, "version": {"number": p["version"]}, "body": {"storage": {"value": p["body"]}}}

    def getter(url, params, auth, email, token):
        calls.append((url.split("/rest/api")[1], dict(params), auth, email, token))
        path = url.split("/rest/api")[1]
        if path == "/space":
            return {"results": [{"key": "DATA"}]}
        if path.startswith("/content/") and path.endswith("/child/page"):
            parent = path.split("/")[2]
            return {"results": [one(i) for i, p in pages.items() if p["parent"] == parent][params["start"]:params["start"] + params["limit"]]}
        if path == "/content":
            rows = [one(i) for i, p in pages.items() if p["space"] == params["spaceKey"]]
            return {"results": rows[params["start"]:params["start"] + params["limit"]]}
        if path.startswith("/content/"):
            return one(path.rsplit("/", 1)[1])
        raise AssertionError(path)
    return getter, calls


def _connected(conn, monkeypatch, **over):
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
    ks.save_config(conn, "Admin", {**GOOD, **over})
    getter, calls = _confluence()
    assert ks.test_connection(conn, getter=getter)["status"] == "CONNECTED"
    return getter, calls


def test_the_connection_test_records_whether_confluence_accepted_the_saved_sign_in(conn, monkeypatch):
    with pytest.raises(ks.SourceError, match="Save the Confluence settings"):
        ks.test_connection(conn)
    assert ks.is_connected(conn) is False
    monkeypatch.delenv("CONFLUENCE_API_TOKEN", raising=False)
    ks.save_config(conn, "Admin", GOOD)
    failed = ks.test_connection(conn, getter=_confluence()[0])
    assert failed["status"] == "FAILED" and "CONFLUENCE_API_TOKEN is empty" in failed["check_detail"] and failed["token_present"] is False
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
    getter, calls = _confluence()
    ok = ks.test_connection(conn, getter=getter)
    assert ok["status"] == "CONNECTED" and ks.is_connected(conn) and calls[0][0] == "/space" and calls[0][2:] == ("basic", "svc@acme.example", "tok")

    def refused(url, params, auth, email, token):
        raise ks.SourceError("Confluence refused the saved sign-in. Ask a Platform Admin to check the account and token.")
    assert ks.test_connection(conn, getter=refused)["status"] == "FAILED" and not ks.is_connected(conn)


def test_a_page_a_page_with_children_or_a_space_is_imported_only_from_allowed_spaces_on_the_connected_host(conn, monkeypatch):
    getter, _calls = _connected(conn, monkeypatch)
    link = "https://acme.atlassian.net/wiki/spaces/DATA/pages/1/Root"
    one = ks.fetch_pages(conn, link, "page", getter)
    assert [p["title"] for p in one["pages"]] == ["Root"] and one["pages"][0]["version"] == "3" and one["pages"][0]["text"] == "Root text" and not one["truncated"]
    tree = ks.fetch_pages(conn, link, "children", getter)
    assert [p["title"] for p in tree["pages"]] == ["Root", "Child A", "Grandchild"]  # the empty child is dropped, order is parent first
    assert "## " not in tree["pages"][2]["text"] and "Codes" in tree["pages"][2]["text"]
    space = ks.fetch_pages(conn, "https://acme.atlassian.net/wiki/spaces/DATA/overview", "space", getter)
    assert {p["page_id"] for p in space["pages"]} == {"1", "2", "3"}
    for bad, mode, why in (("https://acme.atlassian.net/wiki/spaces/OPS/pages/9/x", "page", "Space OPS is not allowed"), ("https://acme.atlassian.net/wiki/spaces/OPS/overview", "space", "Space OPS is not allowed"),
                           ("https://evil.example.com/wiki/spaces/DATA/pages/1/x", "page", "not on the connected Confluence"), ("http://acme.atlassian.net/wiki/pages/1", "page", "https"),
                           ("https://acme.atlassian.net/wiki/spaces/DATA/overview", "page", "one page"), ("https://acme.atlassian.net/wiki/pages/1", "space", "name a space"),
                           (link, "everything", "Choose page")):
        with pytest.raises(ks.SourceError, match=why):
            ks.fetch_pages(conn, bad, mode, getter)
    ks.save_config(conn, "Admin", {**GOOD, "page_cap": "2"})
    conn.execute("UPDATE confluence_connection SET status = 'CONNECTED'")
    cut = ks.fetch_pages(conn, link, "children", getter)
    assert len(cut["pages"]) == 2 and cut["truncated"]  # stopped at the page cap


def test_confluence_refuses_until_an_admin_has_connected_it(conn):
    with pytest.raises(ks.SourceError, match="Ask a Platform Admin to connect it"):
        ks.fetch_pages(conn, "https://acme.atlassian.net/wiki/pages/1", "page")


# ---- routes -----------------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def app_env(tmp_path, monkeypatch):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-K" + __import__("uuid").uuid4().hex[:8]
    monkeypatch.setattr(app_module, "state_store", store)
    for sid, name, role in (("admin", "Ada", "Platform Admin"), ("analyst", "Ann", "Migration Analyst"), ("viewer", "Vic", "Validation Specialist"), ("sme", "Sam", "Domain SME")):
        monkeypatch.setitem(app_module.session_mgr.active_sessions, sid, {"email": f"{sid}@example.invalid", "name": name, "role": role})
    monkeypatch.setattr(ks, "_default_getter", lambda url: (_ for _ in ()).throw(AssertionError("no network in tests")))

    class Env:
        pass
    def clear_global_settings():  # the web hosts and the Confluence connection are shared by every migration in the test database
        c = app_module.get_db_connection()
        try:
            ks.ensure_tables(c)
            c.execute("DELETE FROM confluence_connection")
            c.execute("DELETE FROM knowledge_web_domains")
            c.commit()
        finally:
            c.close()
    clear_global_settings()
    env = Env()
    env.app, env.store, env.mid = app_module, store, store.questionnaire_data["migration_id"]

    def call(sid, method, path, data=None, files=None):
        async def go():
            transport = httpx.ASGITransport(app=app_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
                return await client.request(method, path, data=data, files=files, follow_redirects=False, headers={"accept": "text/html"})
        return asyncio.run(go())
    env.call = call

    def chunks():
        c = app_module.get_db_connection()
        try:
            return kc.stats(c, env.mid)
        finally:
            c.close()
    env.chunks = chunks
    yield env
    clear_global_settings()


def _loc(resp):
    from urllib.parse import unquote_plus
    return unquote_plus(resp.headers["location"])


def test_an_uploaded_spreadsheet_is_registered_and_its_rows_are_chunked_and_removing_it_retires_them(app_env):
    r = app_env.call("analyst", "POST", "/api/knowledge/file-upload", {"category": "data_dictionary"}, {"file": ("dict.xlsx", _workbook(), "application/octet-stream")})
    assert r.status_code == 303
    doc = app_env.store.firm_knowledge_docs[-1]
    assert doc["filename"] == "dict.xlsx" and doc.get("indexing_note") == ""
    stats = app_env.chunks()
    assert stats[f"{app_env.mid}:{doc['id']}"]["chunks"] == 2 and stats[f"{app_env.mid}:{doc['id']}"]["source"] == "file"
    unreadable = app_env.call("analyst", "POST", "/api/knowledge/file-upload", {"category": "data_dictionary"}, {"file": ("blank.md", b"x", "text/markdown")})
    assert unreadable.status_code == 303  # still registered; one chunk
    gone = app_env.call("analyst", "POST", "/api/knowledge/remove", {"doc_id": doc["id"]})
    assert gone.status_code == 303 and f"{app_env.mid}:{doc['id']}" not in app_env.chunks()


def test_document_ids_stay_unique_after_a_removal(app_env):
    for n in range(3):
        app_env.call("analyst", "POST", "/api/knowledge/file-upload", {"category": "business_rules"}, {"file": (f"n{n}.md", b"# H\ntext " + bytes([65 + n]), "text/markdown")})
    first = app_env.store.firm_knowledge_docs[0]["id"]
    app_env.call("analyst", "POST", "/api/knowledge/remove", {"doc_id": first})
    app_env.call("analyst", "POST", "/api/knowledge/file-upload", {"category": "business_rules"}, {"file": ("n9.md", b"# H\nmore", "text/markdown")})
    ids = [d["id"] for d in app_env.store.firm_knowledge_docs]
    assert len(ids) == len(set(ids)) == 3


def test_a_web_page_is_imported_replaced_when_it_changes_and_skipped_when_it_does_not(app_env, monkeypatch):
    state = {"text": "Version one", "version": "aaaa"}
    monkeypatch.setattr(ks, "fetch_web", lambda c, url: {"title": "Guide", "text": state["text"], "url": url, "version": state["version"]})
    form = {"category": "business_rules", "url": "https://docs.example.com/guide"}
    r = app_env.call("analyst", "POST", "/api/knowledge/web-link", form)
    assert r.status_code == 303 and "imported" in _loc(r)
    doc = app_env.store.firm_knowledge_docs[-1]
    assert doc["source"] == "web" and doc["source_version"] == "aaaa" and doc["full_text"] == "Version one"
    assert "has not changed" in _loc(app_env.call("analyst", "POST", "/api/knowledge/web-link", form))
    assert len(app_env.store.firm_knowledge_docs) == 1
    state.update(text="Version two", version="bbbb")
    assert "has not changed" not in _loc(app_env.call("analyst", "POST", "/api/knowledge/resync", {"doc_id": doc["id"]}))  # a re-sync notices the change
    docs = app_env.store.firm_knowledge_docs
    assert len(docs) == 1 and docs[0]["source_version"] == "bbbb" and docs[0]["full_text"] == "Version two" and docs[0]["id"] != doc["id"]
    assert list(app_env.chunks()) == [f"{app_env.mid}:{docs[0]['id']}"]  # the old copy's chunks are gone
    assert "has not changed" in _loc(app_env.call("analyst", "POST", "/api/knowledge/resync", {"doc_id": docs[0]["id"]}))
    monkeypatch.setattr(ks, "fetch_web", lambda c, url: (_ for _ in ()).throw(ks.SourceError("evil.org is not an approved web source. Ask a Platform Admin to approve it.")))
    assert app_env.call("analyst", "POST", "/api/knowledge/web-link", {"category": "business_rules", "url": "https://evil.org/x"}).status_code == 400
    assert "approved web source" in _loc(app_env.call("analyst", "POST", "/api/knowledge/resync", {"doc_id": docs[0]["id"]}))
    assert "Only an imported" in _loc(app_env.call("analyst", "POST", "/api/knowledge/resync", {"doc_id": "DOC-404"}))
    assert "may+not+perform" in app_env.call("viewer", "POST", "/api/knowledge/web-link", form).headers["location"]


def _pages(version="1"):
    return [{"page_id": "1", "title": "Root", "text": "Root text", "url": "https://acme.atlassian.net/wiki/pages/1", "space": "DATA", "version": version},
            {"page_id": "2", "title": "Child A", "text": "Child text", "url": "https://acme.atlassian.net/wiki/pages/2", "space": "DATA", "version": "1"}]


def test_confluence_pages_are_added_then_unchanged_then_updated_by_version(app_env, monkeypatch):
    monkeypatch.setattr(ks, "is_connected", lambda c: True)
    served = {"pages": _pages("1"), "truncated": False}
    monkeypatch.setattr(ks, "fetch_pages", lambda c, link, mode: served)
    form = {"category": "data_dictionary", "link": "https://acme.atlassian.net/wiki/spaces/DATA/pages/1/Root", "mode": "children"}
    assert "2 added, 0 updated, 0 unchanged" in _loc(app_env.call("analyst", "POST", "/api/knowledge/confluence", form))
    assert "0 added, 0 updated, 2 unchanged" in _loc(app_env.call("analyst", "POST", "/api/knowledge/confluence", form))
    served["pages"] = _pages("2")
    served["truncated"] = True
    msg = _loc(app_env.call("analyst", "POST", "/api/knowledge/confluence", form))
    assert "0 added, 1 updated, 1 unchanged" in msg and "page cap" in msg
    docs = app_env.store.firm_knowledge_docs
    assert len(docs) == 2 and {d["source_page_id"] for d in docs} == {"1", "2"} and next(d for d in docs if d["source_page_id"] == "1")["source_version"] == "2"
    assert len(app_env.chunks()) == 2
    monkeypatch.setattr(ks, "fetch_pages", lambda c, link, mode: (_ for _ in ()).throw(ks.SourceError("Space OPS is not allowed. Ask a Platform Admin to add it to the allowed spaces.")))
    assert app_env.call("analyst", "POST", "/api/knowledge/confluence", form).status_code == 400
    monkeypatch.setattr(ks, "fetch_pages", lambda c, link, mode: {"pages": _pages("3")[:1], "truncated": False})
    resync = app_env.call("analyst", "POST", "/api/knowledge/resync", {"doc_id": next(d["id"] for d in app_env.store.firm_knowledge_docs if d["source_page_id"] == "1")})
    assert "changed and was imported again" in _loc(resync)


def test_without_a_connection_only_single_pages_work_and_the_user_is_told_to_ask_an_admin(app_env, monkeypatch):
    from control_plane.services import confluence_source
    monkeypatch.setattr(ks, "is_connected", lambda c: False)
    children = app_env.call("analyst", "POST", "/api/knowledge/confluence", {"category": "business_rules", "link": "https://acme.atlassian.net/wiki/pages/1", "mode": "children"})
    assert children.status_code == 400 and "Ask a Platform Admin" in children.text

    def unconfigured(link):
        raise confluence_source.ConfluenceError("Confluence is not configured: the server needs CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN.")
    monkeypatch.setattr(confluence_source, "fetch_page", unconfigured)
    single = app_env.call("analyst", "POST", "/api/knowledge/confluence", {"category": "business_rules", "link": "https://acme.atlassian.net/wiki/pages/1"})
    assert single.status_code == 400 and "Platform Admin can connect Confluence" in single.text


def test_only_a_platform_admin_sets_up_the_sources_and_the_token_never_comes_through_a_form(app_env, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
    form = {"base_url": "https://acme.atlassian.net/wiki", "auth": "basic", "email": "svc@acme.example", "token_env_var": "CONFLUENCE_API_TOKEN", "allowed_spaces": "DATA", "page_cap": "10"}
    for role in ("analyst", "sme", "viewer"):
        assert "may+not+perform" in app_env.call(role, "POST", "/api/platform-knowledge/sources/confluence", form).headers["location"]
        assert "may+not+perform" in app_env.call(role, "POST", "/api/platform-knowledge/sources/domains", {"host": "example.com"}).headers["location"]
    assert "Confluence settings saved" in _loc(app_env.call("admin", "POST", "/api/platform-knowledge/sources/confluence", form))
    bad = app_env.call("admin", "POST", "/api/platform-knowledge/sources/confluence", {**form, "token_env_var": "xoxp-real-looking-token"})
    assert "starts with CONFLUENCE_" in _loc(bad) and "xoxp" not in bad.headers["location"].split("error=")[0]
    monkeypatch.setattr(ks, "_api", lambda cfg, path, params=None, getter=None: {"results": []})
    assert "answered and accepted" in _loc(app_env.call("admin", "POST", "/api/platform-knowledge/sources/confluence/test"))
    assert "approved as a web source" in _loc(app_env.call("admin", "POST", "/api/platform-knowledge/sources/domains", {"host": "docs.example.com"}))
    assert "already approved" in _loc(app_env.call("admin", "POST", "/api/platform-knowledge/sources/domains", {"host": "docs.example.com"}))
    assert "removed" in _loc(app_env.call("admin", "POST", "/api/platform-knowledge/sources/domains/remove", {"host": "docs.example.com"}))


def test_the_pages_show_the_connection_state_the_chunk_counts_and_the_right_controls(app_env, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "tok")
    c = app_env.app.get_db_connection()
    try:
        ks.ensure_tables(c)
        c.execute("DELETE FROM confluence_connection")
        c.execute("DELETE FROM knowledge_web_domains")
        c.commit()
    finally:
        c.close()
    analyst = app_env.call("analyst", "GET", "/firm-knowledge").text
    assert 'data-testid="confluence-status"' in analyst and "Confluence is not connected" in analyst and 'data-testid="cell-content-warning"' in analyst
    assert "No web site is approved yet" in analyst and "/api/knowledge/web-link" in analyst
    admin = app_env.call("admin", "GET", "/platform-knowledge").text
    assert 'data-testid="confluence-connection-form"' in admin and "Not connected." in admin and "Approve site" in admin
    form = {"base_url": "https://acme.atlassian.net/wiki", "auth": "basic", "email": "svc@acme.example", "token_env_var": "CONFLUENCE_API_TOKEN", "allowed_spaces": "DATA", "page_cap": "10"}
    app_env.call("admin", "POST", "/api/platform-knowledge/sources/confluence", form)
    app_env.call("admin", "POST", "/api/platform-knowledge/sources/domains", {"host": "docs.example.com"})
    sme = app_env.call("sme", "GET", "/platform-knowledge").text
    assert "acme.atlassian.net/wiki" in sme and "NOT_TESTED" in sme and "docs.example.com" in sme
    assert 'data-testid="confluence-connection-form"' not in sme and "A Platform Admin connects Confluence" in sme and "tok" not in sme.replace("token", "")  # no form, no secret
    app_env.call("analyst", "POST", "/api/knowledge/file-upload", {"category": "data_dictionary"}, {"file": ("dict.xlsx", _workbook(), "application/octet-stream")})
    page = app_env.call("analyst", "GET", "/firm-knowledge").text
    assert "2 chunk(s)" in page and "docs.example.com" in page
