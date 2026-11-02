import pytest
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


def test_files_without_the_link_column_do_not_count_as_missing_relationship_values():
    mappings = {"Account": {"ACCT": "ACCT_NUM", "HH": "HH_ID"}, "Household": {"H": "HH_ID"}}
    files = {
        "Account": [
            {"filename": "accounts.csv", "records": [{"ACCT": "A1", "HH": "H1"}]},
            {"filename": "registration.csv", "records": [{"ACCT": "A1"}, {"ACCT": "A2"}]},
        ],
        "Household": [{"filename": "hh.csv", "records": [{"H": "H1"}]}],
    }
    link = [c for c in run_checks(CONTRACT, mappings, files)["checks"]
            if c["check"] == "RELATIONSHIP" and "Household.HH_ID" in c["field"]][0]
    assert link["status"] == "PASS"


# ---------------------------------------------------------------- XODUS-009 scenarios and exceptions
def _scn(check_type, field, params, stage="PRE", entity="Account"):
    return {"scenario_id": "SCN-T", "entity_name": entity, "check_type": check_type, "field_name": field, "params": params,
            "stage": stage, "description": "why"}


def test_scenarios_are_evaluated_per_file_with_counts_only_and_post_is_not_evaluated():
    mappings = {"Account": {"ACCT": "ACCT_NUM", "REG": "REG_TYPE"}}
    files = {"Account": [{"filename": "a.csv", "records": [{"ACCT": "A1", "REG": "IRA"}, {"ACCT": "A2", "REG": "XXX"}, {"ACCT": "A2", "REG": ""}]}]}
    scenarios = [_scn("ALLOWED_VALUES", "REG_TYPE", {"values": ["IRA", "INDIVIDUAL"]}), _scn("NOT_BLANK", "REG_TYPE", {}),
                 _scn("ROW_COUNT_MIN", None, {"min": 5}), _scn("MAX_LENGTH", "ACCT_NUM", {"max": 5}),
                 _scn("NOT_BLANK", "OPEN_DATE", {}), _scn("UNIQUE", "ACCT_NUM", {}, stage="POST")]
    checks = [c for c in run_checks(CONTRACT, mappings, files, scenarios)["checks"] if c["check"].startswith("SCENARIO")]
    assert any(c["status"] == "FAIL" and c.get("field") == "REG_TYPE" and "1 value(s) outside" in c["detail"] for c in checks)
    assert any(c["status"] == "FAIL" and "1 blank" in c["detail"] for c in checks)
    assert any(c["status"] == "FAIL" and "3 row(s); at least 5" in c["detail"] for c in checks)
    assert any(c["status"] == "PASS" and "within 5" in c["detail"] for c in checks)
    assert any(c["status"] == "NOT_EVALUATED" and "no confirmed file supplies OPEN_DATE" in c["detail"] for c in checks)
    assert any(c["status"] == "NOT_EVALUATED" and "post-execution" in c["detail"] for c in checks)
    assert all("A1" not in c["detail"] and "XXX" not in c["detail"] for c in checks)  # no record values leak
    assert all(c["ref"] for c in run_checks(CONTRACT, mappings, files, scenarios)["checks"])


def test_scenario_validation_rejects_unsupported_input():
    import pytest
    from control_plane.services import validation_scenarios as vs
    with pytest.raises(ValueError, match="Unsupported"):
        vs.validate_params("RUN_SQL", "ACCT_NUM", {})
    with pytest.raises(ValueError, match="canonical field"):
        vs.validate_params("NOT_BLANK", "acct; drop", {})
    with pytest.raises(ValueError, match="declared formats"):
        vs.validate_params("DATE_PARSEABLE", "OPEN_DATE", {"input_formats": ["whatever"]})
    with pytest.raises(ValueError, match="min must not exceed"):
        vs.validate_params("NUMERIC_RANGE", "QTY", {"min": 5, "max": 1})


def test_exception_proposed_by_validator_approved_by_analyst_never_the_proposer(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "val.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    store = WorkspaceStateStore()
    store.questionnaire_data["migration_id"] = "MIG-V"
    monkeypatch.setattr(app_module, "state_store", store)
    sessions = app_module.session_mgr.active_sessions
    monkeypatch.setitem(sessions, "sid", {"email": "v@example.invalid", "name": "Val", "role": "Validation Specialist"})
    monkeypatch.setitem(sessions, "sid2", {"email": "a@example.invalid", "name": "Ann", "role": "Migration Analyst"})
    db.init_db()
    stored = tmp_path / "accounts.csv"
    stored.write_text("ACCT,HH\nA1,H1\nA1,H1\n")
    store.entity_source_files["Account"] = {"ENTITY_MASTER": {"filename": "accounts.csv", "stored_path": str(stored), "inference_approval_status": "APPROVED"}}
    conn = sqlite3.connect(tmp_path / "val.db")
    for src, canon in (("ACCT", "ACCT_NUM"), ("HH", "HH_ID")):
        conn.execute("INSERT INTO canonical_mappings (migration_id, entity_name, source_field, canonical_field, mapping_type, transformation_expression, "
                     "confidence_score, is_confirmed, created_by, updated_utc) VALUES ('MIG-V', 'Account', ?, ?, 'DIRECT', 'DIRECT_COPY', 0.9, 1, 'x', '2026')", (src, canon))
    conn.commit()

    async def call(sid, role, method, path, data=None):
        sessions[sid]["role"] = role
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
            return await client.request(method, path, data=data, follow_redirects=False)

    form = {"entity_name": "Account", "check_type": "MAX_LENGTH", "field_name": "ACCT_NUM", "max_value": "1", "description": "ids are short", "stage": "PRE"}
    assert asyncio.run(call("sid2", "Migration Analyst", "POST", "/api/validation/scenarios", form)).status_code == 403
    assert "notice=" in asyncio.run(call("sid", "Validation Specialist", "POST", "/api/validation/scenarios", form)).headers["location"]
    assert "error=" in asyncio.run(call("sid", "Validation Specialist", "POST", "/api/validation/scenarios", {**form, "check_type": "RUN_SQL"})).headers["location"]
    asyncio.run(call("sid", "Validation Specialist", "POST", "/api/validation/run"))
    run_id, results = conn.execute("SELECT run_id, results_json FROM validation_runs").fetchone()
    import json as _j
    failing = [c for c in _j.loads(results)["checks"] if c["check"] == "KEY_UNIQUE"][0]
    exc_form = {"run_id": run_id, "check_ref": failing["ref"], "reason": "known duplicate in legacy extract"}
    assert asyncio.run(call("sid2", "Migration Analyst", "POST", "/api/validation/exceptions", exc_form)).status_code == 403
    assert "error=" in asyncio.run(call("sid", "Validation Specialist", "POST", "/api/validation/exceptions", {**exc_form, "check_ref": "nope"})).headers["location"]
    assert "notice=" in asyncio.run(call("sid", "Validation Specialist", "POST", "/api/validation/exceptions", exc_form)).headers["location"]
    assert "error=" in asyncio.run(call("sid", "Validation Specialist", "POST", "/api/validation/exceptions", exc_form)).headers["location"]  # no duplicate
    eid = conn.execute("SELECT exception_id FROM validation_exceptions").fetchone()[0]
    decide = f"/api/validation/exceptions/{eid}/decision"
    assert asyncio.run(call("sid", "Validation Specialist", "POST", decide, {"decision": "APPROVED"})).status_code == 403  # validator cannot approve
    sessions["sid"]["name"] = "Val"
    sessions["sid2"]["name"] = "Val"  # same identity as proposer
    assert "cannot+decide" in asyncio.run(call("sid2", "Migration Analyst", "POST", decide, {"decision": "APPROVED"})).headers["location"]
    sessions["sid2"]["name"] = "Ann"
    assert "error=" in asyncio.run(call("sid2", "Migration Analyst", "POST", decide, {"decision": "REJECTED"})).headers["location"]  # reject needs a comment
    assert "notice=" in asyncio.run(call("sid2", "Migration Analyst", "POST", decide, {"decision": "APPROVED", "comments": "accepted"})).headers["location"]
    page = asyncio.run(call("sid", "Validation Specialist", "GET", "/reconciliation")).text
    assert "Validation scenarios" in page and "SCENARIO SCN-" in page and f"Exception {eid} APPROVED" in page and "decided by Ann" in page
    row = conn.execute("SELECT status, decided_by FROM validation_exceptions").fetchone()
    assert row == ("APPROVED", "Ann")
    from control_plane.services import validation_scenarios as _vs
    conn.row_factory = sqlite3.Row
    last = conn.execute("SELECT migration_id, run_id, status, results_json FROM validation_runs ORDER BY executed_utc DESC LIMIT 1").fetchone()
    checks = __import__("json").loads(last["results_json"])["checks"]
    expected = _vs.effective_status(checks, _vs.list_exceptions(conn, last["migration_id"], last["run_id"]))
    assert app_module.workflow_progress()["validation_status"] == expected  # the workflow bar uses the effective status
    assert expected != "FAIL" or last["status"] == "FAIL"
    ev = asyncio.run(call("sid", "Validation Specialist", "GET", "/api/validation/signoff-evidence"))
    assert ev.status_code == 200 and "attachment" in ev.headers["content-disposition"]
    doc = ev.json()
    assert doc["schema"] == "xodus.signoff-evidence/v1" and doc["validation_run"]["run_id"] == last["run_id"] and doc["validation_run"]["effective_status"] == expected
    assert any(e["status"] == "APPROVED" and e["decided_by"] == "Ann" and e["reason"] for e in doc["exceptions"])
    assert "no source record values" in " ".join(doc["notes"])

    # A stale stored ADF package blocks saving the sign-off unless the reviewer states why.
    from control_plane.services import artifact_store as _as
    monkeypatch.setattr(_as, "STORAGE_ROOT", tmp_path / "storage")
    _as.save(conn, "MIG-V", "ADF_PACKAGE", b"zip", "Val", provenance={"approved_target_mapping_ids": ["T1"], "approved_mapping_set_sha256": "old", "validation_run_id": "VAL-OLD"})
    conn.commit()
    save = "/api/validation/signoff-evidence/save"
    for data in (None, {"stale_reason": "too short"}):
        blocked = asyncio.run(call("sid", "Validation Specialist", "POST", save, data)).headers["location"]
        assert "error=" in blocked and "is+stale" in blocked
    assert "is+stale" in asyncio.run(call("sid", "Validation Specialist", "GET", "/api/validation/signoff-evidence")).headers["location"]
    assert not conn.execute("SELECT 1 FROM stored_artifacts WHERE kind = 'SIGNOFF_EVIDENCE'").fetchall()
    page = asyncio.run(call("sid", "Validation Specialist", "GET", "/reconciliation")).text
    assert "stale-reason" in page and "STALE" in page
    ok = asyncio.run(call("sid", "Validation Specialist", "POST", save, {"stale_reason": "Rehearsal only; package regenerated after sign-off review"})).headers["location"]
    assert "notice=" in ok
    saved = _as.latest(conn, "MIG-V", "SIGNOFF_EVIDENCE")
    saved_doc = _j.loads(_as.read(conn, saved["artifact_id"])["data"])
    assert saved_doc["stale_package_override"]["reviewer_reason"].startswith("Rehearsal only") and saved_doc["stale_package_override"]["reviewer"] == "Val" and saved_doc["stale_package_override"]["stale_reasons"]
    assert saved_doc["adf_package"]["mapping_set_matches_current"] is False


def _spec():
    return {"entities": {"Account": {"fields": [{"name": "T.ACCT_NO", "required": True}, {"name": "T.NOTE", "required": False}, {"name": "T.OWNER", "required": True}]},
                         "Client": {"fields": []}}}


def test_default_scenarios_come_from_required_approved_fields_and_keys_and_stay_draft_until_adopted(tmp_path):
    from control_plane.services import validation_scenarios as vs
    maps = [{"entity_name": "Account", "target_field": "T.ACCT_NO", "canonical_field": "ACCT_NUM", "status": "APPROVED"},
            {"entity_name": "Account", "target_field": "T.NOTE", "canonical_field": "NOTES", "status": "APPROVED"},      # not required
            {"entity_name": "Account", "target_field": "T.OWNER", "canonical_field": "OWNER_ID", "status": "PROPOSED"}]  # not approved
    conn = sqlite3.connect(tmp_path / "v.db")
    conn.row_factory = sqlite3.Row
    found = vs.suggest_defaults(_spec(), maps, [])
    assert {(f["entity"], f["check_type"], f["field"]) for f in found} == {("Account", "NOT_BLANK", "ACCT_NUM"), ("Account", "UNIQUE", "ACCT_NUM"), ("Client", "UNIQUE", "CLIENT_ID")}
    assert vs.suggest_defaults(None, maps, []) == []
    assert vs.save_suggestions(conn, "M", found, "Val") == 3
    assert vs.list_scenarios(conn, "M", active_only=True) == []          # drafts are not evaluated
    assert vs.suggest_defaults(_spec(), maps, vs.list_scenarios(conn, "M")) == []   # no duplicates once drafted
    sid = vs.list_scenarios(conn, "M")[0]["scenario_id"]
    vs.adopt_scenario(conn, sid)
    assert [s["scenario_id"] for s in vs.list_scenarios(conn, "M", active_only=True)] == [sid]
    with pytest.raises(ValueError):
        vs.adopt_scenario(conn, sid)
    dismissed = vs.list_scenarios(conn, "M")[1]["scenario_id"]
    vs.retire_scenario(conn, dismissed)
    assert vs.suggest_defaults(_spec(), maps, vs.list_scenarios(conn, "M")) != []  # a dismissed suggestion may be suggested again


def test_suggest_and_adopt_routes_are_validator_or_admin_only():
    from ui.role_policy import allowed_roles_for
    for path in ("/api/validation/scenarios/suggest", "/api/validation/scenarios/SCN-1/adopt"):
        assert allowed_roles_for("POST", path) == {"Platform Admin", "Validation Specialist"}


def test_signoff_evidence_is_saved_with_the_migration_and_hash_checked(tmp_path):
    import sqlite3
    from control_plane.services import artifact_store as store
    conn = sqlite3.connect(":memory:")
    a = store.save(conn, "MIG-X", "SIGNOFF_EVIDENCE", b'{"k": 1}', "Reviewer", root=tmp_path)
    b = store.save(conn, "MIG-X", "SIGNOFF_EVIDENCE", b'{"k": 2}', "Reviewer", root=tmp_path)
    assert (a["artifact_version"], b["artifact_version"]) == (1, 2) and (tmp_path / a["rel_path"]).read_bytes() == b'{"k": 1}'
    assert [r["artifact_version"] for r in store.list_artifacts(conn, "MIG-X")] == [2, 1] and store.list_artifacts(conn, "MIG-OTHER") == []
    assert store.read(conn, a["artifact_id"], root=tmp_path)["data"] == b'{"k": 1}'
    (tmp_path / a["rel_path"]).write_bytes(b"x")
    with pytest.raises(ValueError):
        store.read(conn, a["artifact_id"], root=tmp_path)
    with pytest.raises(ValueError):
        store.save(conn, "MIG-X", "UNKNOWN", b"", "Reviewer", root=tmp_path)


def test_stored_package_records_provenance_and_signoff_cites_it(tmp_path):
    import sqlite3
    from control_plane.services import artifact_store as store
    conn = sqlite3.connect(":memory:")
    approved = [{"target_mapping_id": "TM-2", "status": "APPROVED"}, {"target_mapping_id": "TM-1", "status": "APPROVED"}, {"target_mapping_id": "TM-9", "status": "PROPOSED"}]
    conn.execute("CREATE TABLE validation_runs (run_id TEXT, migration_id TEXT, status TEXT, executed_utc TEXT)")
    conn.execute("INSERT INTO validation_runs VALUES ('VAL-1', 'MIG-X', 'FAIL', '2026-11-02T10:00:00')")
    prov = store.build_provenance(conn, "MIG-X", {"spec_version": "abc"}, approved)
    assert prov["approved_target_mapping_ids"] == ["TM-1", "TM-2"] and prov["validation_run_id"] == "VAL-1" and prov["target_model_version"] == "abc" and prov["contract_package"] is None
    art = store.save(conn, "MIG-X", "ADF_PACKAGE", b"zip", "Reviewer", root=tmp_path, provenance=prov)
    assert store.latest(conn, "MIG-X", "ADF_PACKAGE")["provenance"]["approved_target_mapping_ids"] == ["TM-1", "TM-2"] and art["provenance"] == prov
    ref = store.package_reference(conn, "MIG-X", prov, "VAL-1")
    assert ref["artifact_id"] == art["artifact_id"] and ref["mapping_set_matches_current"] and ref["built_from_signed_off_validation_run"] and ref["approved_target_mapping_count"] == 2
    changed = store.build_provenance(conn, "MIG-X", {"spec_version": "abc"}, approved[:1])  # a mapping was un-approved after the package was built
    assert store.package_reference(conn, "MIG-X", changed, "VAL-1")["mapping_set_matches_current"] is False
    assert store.package_reference(conn, "MIG-X", prov, "VAL-2")["built_from_signed_off_validation_run"] is False
    assert store.package_reference(conn, "MIG-NONE", prov, "VAL-1") is None


def test_staleness_names_each_way_a_stored_package_can_fall_behind():
    from control_plane.services import artifact_store as store
    base = {"approved_target_mapping_ids": ["T1", "T2"], "approved_mapping_set_sha256": "m1", "target_model_version": "v1", "validation_run_id": "VAL-1",
            "contract_package": {"version": 3, "content_sha256": "c3"}}
    art = {"provenance": dict(base)}
    assert store.staleness(art, dict(base)) == []
    joined = " ".join(store.staleness(art, {**base, "approved_target_mapping_ids": ["T1"], "approved_mapping_set_sha256": "m2", "validation_run_id": "VAL-2",
                                           "target_model_version": "v2", "contract_package": {"version": 4, "content_sha256": "c4"}}))
    for needle in ("approved target mappings changed (2 when built, 1 now; 0 added, 1 removed)", "target model changed", "VAL-1", "VAL-2", "v3 when built, v4 now"):
        assert needle in joined
    assert "regenerate" in store.staleness({"provenance": {}}, base)[0]


def test_provenance_covers_confirmed_source_mappings_and_flags_changes():
    import sqlite3
    from control_plane.services import artifact_store as store
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE canonical_mappings (mapping_id TEXT, migration_id TEXT, entity_name TEXT, source_field TEXT, canonical_field TEXT, is_confirmed INTEGER)")
    conn.execute("INSERT INTO canonical_mappings VALUES ('CM-1', 'MIG-X', 'Account', 'A', 'ACCT_NUM', 1), (NULL, 'MIG-X', 'Client', 'C', 'CLIENT_ID', 1), ('CM-3', 'MIG-X', 'Client', 'D', 'X', 0)")
    prov = store.build_provenance(conn, "MIG-X", {"spec_version": "v"}, [])
    assert prov["approved_source_mapping_ids"] == ["CM-1", "Client.C>CLIENT_ID"]  # unconfirmed excluded; id-less rows get a stable key
    art = {"provenance": prov}
    assert store.staleness(art, prov) == []
    conn.execute("UPDATE canonical_mappings SET is_confirmed = 0 WHERE mapping_id = 'CM-1'")
    conn.execute("INSERT INTO canonical_mappings VALUES ('CM-4', 'MIG-X', 'Household', 'H', 'HH_ID', 1)")
    reasons = store.staleness(art, store.build_provenance(conn, "MIG-X", {"spec_version": "v"}, []))
    assert reasons == ["The approved source-to-canonical mappings changed (1 added, 1 removed since the package was built)."]
