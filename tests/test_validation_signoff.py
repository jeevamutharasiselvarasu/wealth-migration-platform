import sqlite3

import pytest

from control_plane.services import validation_signoff as vs


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_sign_off_needs_a_comment_the_latest_run_and_a_pass(conn):
    with pytest.raises(ValueError, match="comment"):
        vs.sign_off(conn, "M1", "R2", "PASS", "  ", "Val", "R2")
    with pytest.raises(ValueError, match="latest"):
        vs.sign_off(conn, "M1", "R1", "PASS", "ok", "Val", "R2")
    with pytest.raises(ValueError, match="not PASS"):
        vs.sign_off(conn, "M1", "R2", "FAIL", "ok", "Val", "R2")
    assert vs.current(conn, "M1", "R2") is None


def test_sign_off_is_recorded_for_one_run_and_goes_stale_when_validation_is_rerun(conn):
    res = vs.sign_off(conn, "M1", "R2", "PASS", "Counts match, exceptions reviewed", "Val Specialist", "R2")
    assert res["signoff_id"].startswith("SO-")
    cur = vs.current(conn, "M1", "R2")
    assert cur["signed_by"] == "Val Specialist" and cur["comment"].startswith("Counts match") and cur["status"] == "SIGNED_OFF"
    assert vs.current(conn, "M1", "R3") is None  # a newer run needs its own sign-off
    assert vs.current(conn, "OTHER", "R2") is None


def test_signoff_form_and_route_for_the_validation_specialist(tmp_path, monkeypatch):
    import asyncio
    import json

    import httpx

    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from control_plane.services import target_validation
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "signoff.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    app_module.state_store.questionnaire_data["migration_id"] = "MIG-SO"
    for sid, role in (("sid-v", "Validation Specialist"), ("sid-a", "Migration Analyst")):
        monkeypatch.setitem(app_module.session_mgr.active_sessions, sid, {"email": f"{sid}@example.invalid", "name": role, "role": role})
    check = {"ref": "C1", "check": "Row count", "entity": "Account", "status": "PASS", "detail": "10 of 10"}
    result = {"status": "PASS", "checks": [check], "summary": {"PASS": 1, "WARN": 0, "FAIL": 0, "NOT_EVALUATED": 0}}
    conn = db.get_db_connection()
    target_validation.ensure_table(conn)
    conn.execute("INSERT INTO target_validation_runs VALUES ('TVAL-1', 'MIG-SO', 'PASS', ?, 'VS', '2026-11-06T10:00:00+00:00')", (json.dumps(result),))
    conn.commit()
    conn.close()

    async def call(sid, method, path, data=None):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
            return await client.request(method, path, data=data, follow_redirects=False, headers={"accept": "text/html"})

    assert 'data-testid="tval-signoff-form"' in asyncio.run(call("sid-v", "GET", "/validation-rules")).text
    assert 'data-testid="tval-signoff-form"' not in asyncio.run(call("sid-a", "GET", "/validation-rules")).text
    r = asyncio.run(call("sid-a", "POST", "/api/target-validation/signoff", {"run_id": "TVAL-1", "comment": "ok"}))
    assert r.status_code in (303, 403) and "signed+off" not in r.headers.get("location", "")
    r = asyncio.run(call("sid-v", "POST", "/api/target-validation/signoff", {"run_id": "TVAL-1", "comment": ""}))
    assert "error=" in r.headers["location"]
    r = asyncio.run(call("sid-v", "POST", "/api/target-validation/signoff", {"run_id": "TVAL-1", "comment": "Counts reconciled."}))
    assert "notice=" in r.headers["location"] and "signed+off" in r.headers["location"]
    page = asyncio.run(call("sid-v", "GET", "/validation-rules")).text
    assert 'data-testid="tval-signed-off"' in page and "Counts reconciled." in page
