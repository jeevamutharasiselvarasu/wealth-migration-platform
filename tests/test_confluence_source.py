import asyncio

import httpx
import pytest

from control_plane.services import confluence_source as cs


def test_links_are_limited_to_https_atlassian_pages():
    assert cs.parse_link("https://acme.atlassian.net/wiki/spaces/DM/pages/12345/Plan") == ("acme.atlassian.net", "12345")
    assert cs.parse_link("https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=77")[1] == "77"
    for bad in ["http://acme.atlassian.net/wiki/spaces/DM/pages/1/x", "https://evil.example.com/wiki/spaces/DM/pages/1/x",
                "https://acme.atlassian.net.evil.com/wiki/pages/1", "https://u:p@acme.atlassian.net/wiki/pages/1",
                "https://acme.atlassian.net/wiki/spaces/DM/overview", ""]:
        with pytest.raises(cs.ConfluenceError):
            cs.parse_link(bad)


def test_storage_markup_becomes_visible_text_only():
    text = cs.storage_to_text("<h1>Rules</h1><p>Estate &amp; Trust</p><script>alert(1)</script><ul><li>A</li><li>B</li></ul>")
    assert text == "Rules\nEstate & Trust\nA\nB"


def test_fetch_needs_server_credentials_and_never_returns_them(monkeypatch):
    link = "https://acme.atlassian.net/wiki/spaces/DM/pages/12345/Plan"
    monkeypatch.delenv("CONFLUENCE_API_TOKEN", raising=False)
    with pytest.raises(cs.ConfluenceError, match="not configured"):
        cs.fetch_page(link)
    monkeypatch.setenv("CONFLUENCE_EMAIL", "svc@example.com")
    monkeypatch.setenv("CONFLUENCE_API_TOKEN", "secret-token")
    seen = {}

    def getter(url, auth):
        seen["url"], seen["auth"] = url, auth
        return {"title": "Plan", "body": {"storage": {"value": "<p>Estate has no code</p>"}}}

    page = cs.fetch_page(link, getter)
    assert seen["url"].startswith("https://acme.atlassian.net/wiki/rest/api/content/12345") and seen["auth"] == ("svc@example.com", "secret-token")
    assert page["text"] == "Estate has no code" and "secret-token" not in str(page)

    def down(url, auth):
        raise httpx.ConnectError("x")

    with pytest.raises(cs.ConfluenceError, match="Could not reach"):
        cs.fetch_page(link, down)


def test_confluence_route_registers_page_as_knowledge_asset_and_rejects_bad_links(tmp_path, monkeypatch):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-C"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "t@example.invalid", "name": "Ann", "role": "Migration Analyst"})
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "viewer", {"email": "v@example.invalid", "name": "Vic", "role": "Validation Specialist"})
    monkeypatch.setattr(app_module.confluence_source, "fetch_page", lambda link: (
        {"title": "Amplify rules", "text": "Estate has no code", "url": "https://acme.atlassian.net/wiki/pages/1", "page_id": "1"}
        if "acme.atlassian.net" in link else (_ for _ in ()).throw(cs.ConfluenceError("Only Atlassian Cloud links are allowed."))))

    async def call(sid, data):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
            return await client.post("/api/knowledge/confluence", data=data, follow_redirects=False, headers={"accept": "text/html"})

    ok = asyncio.run(call("sid", {"category": "business_rules", "link": "https://acme.atlassian.net/wiki/spaces/D/pages/1/x"}))
    assert ok.status_code == 303
    doc = store.firm_knowledge_docs[-1]
    assert doc["filename"] == "Amplify rules.md" and doc["source_url"].endswith("/pages/1") and doc["full_text"] == "Estate has no code"
    assert asyncio.run(call("sid", {"category": "business_rules", "link": "https://evil.example.com/x"})).status_code == 400
    denied = asyncio.run(call("viewer", {"category": "business_rules", "link": "https://acme.atlassian.net/wiki/spaces/D/pages/1/x"}))
    assert "may+not+perform" in denied.headers["location"]
    assert len(store.firm_knowledge_docs) == 1
