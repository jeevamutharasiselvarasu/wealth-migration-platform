import asyncio
import sqlite3

import httpx
import pytest

from control_plane.services import platform_knowledge as pk


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_built_in_files_are_listed_with_what_reads_them(conn, tmp_path):
    (tmp_path / "field_vocabulary.yaml").write_text("a: b", encoding="utf-8")
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    rows = {r["name"]: r for r in pk.list_all(conn, tmp_path)}
    assert rows["field_vocabulary.yaml"]["category"] == "vocabulary" and "inference" in rows["field_vocabulary.yaml"]["used_by"]
    assert rows["notes.md"]["category"] == "other" and rows["notes.md"]["source"] == "built-in"
    assert pk.text_of(conn, "BUILTIN-field_vocabulary.yaml", tmp_path) == "a: b"
    assert pk.text_of(conn, "BUILTIN-../secret.txt", tmp_path) is None  # never reads outside the folder


def test_added_assets_are_validated_stored_as_text_and_can_be_retired(conn, tmp_path):
    with pytest.raises(ValueError):
        pk.add(conn, "run.exe", "other", b"x", "Admin")
    with pytest.raises(ValueError):
        pk.add(conn, "a.md", "nope", b"x", "Admin")
    with pytest.raises(ValueError):
        pk.add(conn, "a.md", "other", b"", "Admin")
    rec = pk.add(conn, "../glossary.md", "domain_research", b"# Glossary", "Admin")
    assert rec["name"] == "glossary.md"
    assert pk.text_of(conn, rec["asset_id"], tmp_path) == "# Glossary"
    assert [a["asset_id"] for a in pk.list_all(conn, tmp_path)] == [rec["asset_id"]]
    pk.retire(conn, rec["asset_id"])
    assert pk.list_all(conn, tmp_path) == [] and pk.text_of(conn, rec["asset_id"], tmp_path) is None
    with pytest.raises(ValueError):
        pk.retire(conn, rec["asset_id"])


def test_platform_knowledge_page_is_the_admins_and_firm_knowledge_is_not(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "pk.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    for sid, role in (("sid-pa", "Platform Admin"), ("sid-ma", "Migration Analyst")):
        monkeypatch.setitem(app_module.session_mgr.active_sessions, sid, {"email": f"{sid}@example.invalid", "name": role, "role": role})

    async def call(sid, method, path, **kw):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
            return await client.request(method, path, follow_redirects=False, headers={"accept": "text/html"}, **kw)

    page = asyncio.run(call("sid-pa", "GET", "/platform-knowledge")).text
    assert "field_vocabulary.yaml" in page and 'data-testid="pk-add"' in page and 'href="/firm-knowledge"' not in page
    assert asyncio.run(call("sid-pa", "GET", "/firm-knowledge")).headers["location"].startswith("/setup?error=")
    r = asyncio.run(call("sid-ma", "POST", "/api/platform-knowledge", data={"category": "other"}, files={"file": ("x.md", b"x")}))
    assert "added+to+platform+knowledge" not in r.headers.get("location", "")
    assert 'data-testid="pk-add"' not in asyncio.run(call("sid-ma", "GET", "/platform-knowledge")).text  # read-only for migration users
    r = asyncio.run(call("sid-pa", "POST", "/api/platform-knowledge", data={"category": "domain_research"}, files={"file": ("glossary.md", b"# Shared glossary")}))
    assert "notice=" in r.headers["location"]
    page = asyncio.run(call("sid-pa", "GET", "/platform-knowledge")).text
    assert "glossary.md" in page
