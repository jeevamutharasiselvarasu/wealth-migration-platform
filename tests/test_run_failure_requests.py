import asyncio
import json

import httpx


def test_a_failed_pipeline_step_can_be_raised_as_an_information_request(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from control_plane.services import run_monitor
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "f2.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data.update({"migration_id": "MIG-F2", "migration_setup": {"entities": ["Account", "Household"]}})
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setattr(app_module, "publish_event", lambda *a, **k: None)
    for sid, role in (("sid-ma", "Migration Analyst"), ("sid-pa", "Platform Admin")):
        monkeypatch.setitem(app_module.session_mgr.active_sessions, sid, {"email": f"{sid}@example.invalid", "name": role, "role": role})
    db.init_db()
    summary = {"status": "FAILED", "run_id": "RUN-1", "trigger_file": "curated/MIG-F2.ready", "steps": [],
               "failures": [{"step": "Copy_Account", "cause": "A column does not fit.", "recommendation": "Fix it.", "action": None, "message": "String truncated"}]}
    conn = db.get_db_connection()
    run_monitor.ensure_table(conn)
    conn.execute("INSERT INTO adf_run_status (migration_id, pipeline_run_id, status, started_utc, finished_utc, checked_utc, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 ("MIG-F2", "RUN-1", "FAILED", "t", "t", "t", json.dumps(summary)))
    conn.commit()
    conn.close()

    async def call(sid, method, path, data=None):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
            return await client.request(method, path, data=data, follow_redirects=False, headers={"accept": "text/html"})

    assert 'data-testid="rm-rfi"' in asyncio.run(call("sid-ma", "GET", "/execution")).text
    r = asyncio.run(call("sid-pa", "POST", "/api/execution/raise-request", {"step": "Copy_Account", "entity": "Account"}))
    assert "raised" not in r.headers.get("location", "")  # the Platform Admin never acts on migration runs
    assert "error=" in asyncio.run(call("sid-ma", "POST", "/api/execution/raise-request", {"step": "Merge_Into_Target", "entity": "Account"})).headers["location"]
    assert "error=" in asyncio.run(call("sid-ma", "POST", "/api/execution/raise-request", {"step": "Copy_Account", "entity": "Client"})).headers["location"]  # out of scope
    r = asyncio.run(call("sid-ma", "POST", "/api/execution/raise-request", {"step": "Copy_Account", "entity": "Account", "note": "Send a corrected accounts file."}))
    assert "notice=" in r.headers["location"]
    raised = [q for q in store.questionnaire_data["mapping_information_requests"] if q["canonical_field"].startswith("PIPELINE:")]
    assert len(raised) == 1 and raised[0]["entity"] == "Account" and raised[0]["status"] == "OPEN"
    assert "Copy_Account" in raised[0]["question"] and "String truncated" in raised[0]["question"] and "corrected accounts file" in raised[0]["question"]
    assert "error=" in asyncio.run(call("sid-ma", "POST", "/api/execution/raise-request", {"step": "Copy_Account", "entity": "Account"})).headers["location"]  # once per failure
    conn = db.get_db_connection()
    assert conn.execute("SELECT status FROM mapping_information_requests WHERE request_id = ?", (raised[0]["request_id"],)).fetchone()[0] == "OPEN"
    conn.close()
