import asyncio
import sqlite3

import httpx

from control_plane.services.migration_validation import run_checks

CONTRACT = {
    "Account": {"required": ["ACCT_NUM", "HH_ID"], "keys": ["ACCT_NUM"]},
    "Household": {"required": ["HH_ID"], "keys": ["HH_ID"]},
    "Client": {"required": ["CLIENT_ID"], "keys": ["CLIENT_ID"]},
}


def _by(checks, name, entity):
    return [c for c in checks if c["check"] == name and c["entity"] == entity]


def test_unmapped_required_blank_duplicate_and_orphan_findings_are_reported():
    mappings = {"Account": {"ACCT": "ACCT_NUM", "HH": "HH_ID"}, "Household": {"H": "HH_ID"}}
    files = {
        "Account": [{"filename": "accounts.csv", "records": [
            {"ACCT": "A1", "HH": "H1"}, {"ACCT": "A1", "HH": "H9"}, {"ACCT": "", "HH": "H1"}]}],
        "Household": [{"filename": "hh.csv", "records": [{"H": "H1"}, {"H": "H2"}]}],
    }
    result = run_checks(CONTRACT, mappings, files)
    checks = result["checks"]
    assert result["status"] == "FAIL"
    assert _by(checks, "REQUIRED_FIELDS_MAPPED", "Client")[0]["status"] == "FAIL"
    assert "CLIENT_ID" in _by(checks, "REQUIRED_FIELDS_MAPPED", "Client")[0]["detail"]
    assert _by(checks, "REQUIRED_FIELDS_MAPPED", "Account")[0]["status"] == "PASS"
    assert "ACCT_NUM (1 row(s))" in _by(checks, "REQUIRED_VALUES_PRESENT", "Account")[0]["detail"]
    assert _by(checks, "KEY_UNIQUE", "Account")[0]["status"] == "FAIL"
    assert _by(checks, "KEY_UNIQUE", "Household")[0]["status"] == "PASS"
    household_link = [c for c in checks if c["check"] == "RELATIONSHIP" and "Household.HH_ID" in c["field"]][0]
    assert household_link["status"] == "FAIL" and household_link["orphans"] == ["H9"]
    client_link = [c for c in checks if c["check"] == "RELATIONSHIP" and "Client.CLIENT_ID" in c["field"]][0]
    assert client_link["status"] == "NOT_EVALUATED"


def test_clean_data_passes_without_inventing_unmapped_relationship_results():
    mappings = {"Household": {"H": "HH_ID"}}
    files = {"Household": [{"filename": "hh.csv", "records": [{"H": "H1"}, {"H": "H2"}]}]}
    result = run_checks({"Household": CONTRACT["Household"]}, mappings, files)
    assert [c for c in result["checks"] if c["entity"] == "Household" and c["status"] != "PASS"] == []
    assert result["summary"]["FAIL"] == 0


def test_validation_run_is_role_gated_persisted_and_shown(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "val.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-V"
    monkeypatch.setattr(app_module, "state_store", store)
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid",
                        {"email": "t@example.invalid", "name": "Val Specialist", "role": "Validation Specialist"})
    db.init_db()
    stored = tmp_path / "accounts.csv"
    stored.write_text("ACCT,HH\nA1,H1\nA1,H1\n")
    store.entity_source_files["Account"] = {"ENTITY_MASTER": {
        "filename": "accounts.csv", "stored_path": str(stored), "inference_approval_status": "APPROVED"}}

    async def call(role, method, path):
        app_module.session_mgr.active_sessions["sid"]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.request(method, path, follow_redirects=False)

    assert asyncio.run(call("Migration Analyst", "POST", "/api/validation/run")).status_code == 403
    blocked = asyncio.run(call("Validation Specialist", "POST", "/api/validation/run"))
    assert "error=" in blocked.headers["location"]  # no approved mappings yet

    conn = sqlite3.connect(tmp_path / "val.db")
    for src, canon in (("ACCT", "ACCT_NUM"), ("HH", "HH_ID")):
        conn.execute("INSERT INTO canonical_mappings (migration_id, entity_name, source_field, canonical_field, "
                     "mapping_type, transformation_expression, confidence_score, is_confirmed, created_by, updated_utc) "
                     "VALUES ('MIG-V', 'Account', ?, ?, 'DIRECT', 'DIRECT_COPY', 0.9, 1, 'x', '2026-01-01')", (src, canon))
    conn.commit()
    done = asyncio.run(call("Validation Specialist", "POST", "/api/validation/run"))
    assert done.status_code == 303 and done.headers["location"].endswith("#validation-results")
    row = conn.execute("SELECT status, executed_by FROM validation_runs").fetchone()
    assert row == ("FAIL", "Val Specialist")  # duplicate ACCT_NUM and unmapped Household/Client required fields
    page = asyncio.run(call("Validation Specialist", "GET", "/reconciliation")).text
    assert "Validation results" in page and "KEY_UNIQUE" in page and "Run validation again" in page
    analyst_page = asyncio.run(call("Migration Analyst", "GET", "/reconciliation")).text
    assert "Run validation again" not in analyst_page
