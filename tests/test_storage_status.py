"""Storage status is an admin view of counts only. (The platform ships no demo data: tests load a sample from tests/fixtures.)"""
from tests.test_firm_config import _post_demo_load, _setup_client


def test_storage_status_is_admin_only_and_reports_counts_without_values(tmp_path, monkeypatch):
    import asyncio
    import httpx
    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "storage-status")
    assert _post_demo_load(app_module, session_id).status_code == 303
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "analyst-s", {"email": "a@example.invalid", "name": "A", "role": "Migration Analyst"})

    async def get(sid):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://testserver", cookies={"session_id": sid}) as c:
            return await c.get("/api/admin/storage-status")
    app_module.session_mgr.active_sessions[session_id]["role"] = "Platform Admin"  # storage status is a platform-admin view
    ok, denied = asyncio.run(get(session_id)), asyncio.run(get("analyst-s"))
    assert denied.status_code == 403
    body = ok.json()
    assert ok.status_code == 200 and body["database"] in {"sqlite", "postgresql"}
    assert body["tables"].get("workflow_events", 0) > 0 and body["workflow_events_by_type"].get("SOURCE_FILE_UPLOADED") == 9
    assert "ACCT" not in ok.text and "LEGACY" not in ok.text  # counts only, no source values
