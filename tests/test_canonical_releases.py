import asyncio

import httpx
import pytest

from control_plane.services import canonical_overlay as co

REQ = {"request_id": "CCR-1", "migration_id": "MIG-A", "entity_name": "Account", "canonical_field": "account_title",
       "change_type": "ADD_FIELD", "proposed_change": "Free-text title", "status": "APPROVED"}


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    import control_plane.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "rel.db")
    return db


def test_a_change_is_live_in_its_own_workspace_and_elsewhere_only_after_release_and_adoption(db_path):
    conn = db_path.get_db_connection()
    co.pinned_release(conn, "MIG-B")  # workspace B exists before the change, on release 0
    co.apply_request(conn, REQ, "Sam SME", [])
    conn.commit()
    assert co.active_added_fields("Account", "MIG-A") == ["ACCOUNT_TITLE"]  # the workspace that asked uses it at once
    assert co.active_added_fields("Account", "MIG-B") == []
    assert co.notice(conn, "MIG-B") is None  # nothing released yet
    with pytest.raises(ValueError):
        co.publish_release(conn, "Sam SME", "")
    rel = co.publish_release(conn, "Sam SME", "Account title")
    conn.commit()
    assert rel["release_no"] == 1 and rel["changes"][0]["field"] == "ACCOUNT_TITLE" and co.unreleased(conn) == []
    with pytest.raises(ValueError):
        co.publish_release(conn, "Sam SME", "again")  # nothing left to release
    assert co.active_added_fields("Account", "MIG-B") == []  # B keeps its pinned release
    note = co.notice(conn, "MIG-B")
    assert note["pinned"] == 0 and note["latest"] == 1 and note["releases"][0]["changes"][0]["field"] == "ACCOUNT_TITLE"
    assert co.pinned_release(conn, "MIG-C") == 1 and co.notice(conn, "MIG-C") is None  # a new workspace starts on the latest release
    assert co.adopt_latest(conn, "MIG-B", "Ana") == 1
    conn.commit()
    assert co.active_added_fields("Account", "MIG-B") == ["ACCOUNT_TITLE"] and co.notice(conn, "MIG-B") is None
    with pytest.raises(ValueError):
        co.adopt_latest(conn, "MIG-B", "Ana")
    assert co.active_added_fields("Account", "") == ["ACCOUNT_TITLE"]  # no workspace: every applied field
    conn.close()


def test_changes_applied_before_releases_existed_become_release_1_for_every_workspace(db_path):
    conn = db_path.get_db_connection()
    conn.execute("""CREATE TABLE canonical_model_overlay (overlay_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, migration_id TEXT NOT NULL,
        entity_name TEXT NOT NULL, canonical_field TEXT NOT NULL, change_type TEXT NOT NULL, definition TEXT NOT NULL, applied_by TEXT NOT NULL, applied_utc TEXT NOT NULL)""")
    conn.execute("INSERT INTO canonical_model_overlay VALUES ('OVL-1', 'CCR-1', 'MIG-A', 'Account', 'ACCOUNT_TITLE', 'ADD_FIELD', 'd', 'Sam', '2026-01-01')")
    conn.commit()
    co.ensure_table(conn)
    conn.commit()
    assert co.latest_release(conn) == 1 and co.unreleased(conn) == [] and co.releases(conn)[0]["released_by"] == "system"
    co.ensure_table(conn)  # runs once: no second release
    assert len(co.releases(conn)) == 1
    conn.close()
    assert co.active_added_fields("Account", "MIG-OTHER") == ["ACCOUNT_TITLE"]  # still live everywhere, as before releases


def test_release_routes_and_the_dashboard_notice(db_path, tmp_path, monkeypatch):
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-B"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setattr(app_module, "publish_event", lambda *a, **k: None)
    for sid, role in (("sid-sme", "Domain SME"), ("sid-ma", "Migration Analyst")):
        monkeypatch.setitem(app_module.session_mgr.active_sessions, sid, {"email": f"{sid}@example.invalid", "name": role, "role": role})
    conn = db_path.get_db_connection()
    co.pinned_release(conn, "MIG-B")
    co.apply_request(conn, REQ, "Sam SME", [])
    conn.commit()
    conn.close()

    async def call(sid, method, path, data=None):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
            return await client.request(method, path, data=data, follow_redirects=False, headers={"accept": "text/html"})

    assert 'data-testid="publish-release"' in asyncio.run(call("sid-sme", "GET", "/canonical-changes")).text
    assert "may+not+perform" in asyncio.run(call("sid-ma", "POST", "/api/canonical-releases", {"notes": "x"})).headers["location"]
    assert "notice=" in asyncio.run(call("sid-sme", "POST", "/api/canonical-releases", {"notes": "Account title"})).headers["location"]
    page = asyncio.run(call("sid-ma", "GET", "/")).text
    assert 'data-testid="canonical-release-notice"' in page and "Account.ACCOUNT_TITLE" in page
    assert "may+not+perform" in asyncio.run(call("sid-sme", "POST", "/api/canonical-releases/adopt", {"return_to": "/"})).headers["location"]
    r = asyncio.run(call("sid-ma", "POST", "/api/canonical-releases/adopt", {"return_to": "/"}))
    assert r.headers["location"].startswith("/?notice=")
    assert 'data-testid="canonical-release-notice"' not in asyncio.run(call("sid-ma", "GET", "/")).text
