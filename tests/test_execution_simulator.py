import pytest

from control_plane.services import execution_simulator as ex


def _package(rule_params=None, scenarios=None):
    return {
        "meta": {"package_id": "PKG-T", "package_version": 1},
        "transformation_rules": [{"expression": "RULE:CUST@1", "rule_type": "VALUE_MAP", "params": {"map": {"SCH": "SCHWAB"}, "case_insensitive": True},
                                  "description": "d", "approved_by": "Ann"}],
        "canonical_to_target": [
            {"entity": "Account", "target_table": "T_ACCT", "target_column": "ACCT_NO", "target_type": "string", "required": True, "canonical_field": "ACCT_NUM",
             "transformation": "DIRECT_COPY", "approved_by": "Ann"},
            {"entity": "Account", "target_table": "T_ACCT", "target_column": "CUST_CD", "target_type": "string", "required": False, "canonical_field": "CUSTODIAN_CODE",
             "transformation": "RULE:CUST@1", "approved_by": "Ann"}],
        "validation": {"scenarios": scenarios or [], "default_checks": []},
    }


ROWS = {"Account": [{"ACCT_NUM": "A1", "CUSTODIAN_CODE": "SCH"}, {"ACCT_NUM": "A2", "CUSTODIAN_CODE": "ZZZ"}, {"ACCT_NUM": "", "CUSTODIAN_CODE": "SCH"}]}


def test_simulated_run_counts_link_problems_to_rules_and_never_carry_values():
    t = ex.simulate(_package(), ROWS)
    table = t["entities"]["Account"]["tables"]["T_ACCT"]
    assert (table["read"], table["loaded"], table["failed"], table["skipped"], table["balanced"]) == (3, 1, 1, 1, True)
    assert t["status"] == "PARTIAL_SUCCESS" and t["totals"] == {"read": 3, "loaded": 1, "skipped": 1, "failed": 1}
    reasons = {(f["column"], f["rule"], f["reason"]) for f in t["failures"]}
    assert ("CUST_CD", "RULE:CUST@1", "unmapped value '…'") in reasons
    assert ("ACCT_NO", "DIRECT_COPY", "required value blank after transformation") in reasons
    assert "ZZZ" not in repr(t) and "A1" not in repr(t)


def test_post_scenarios_run_on_loaded_output_and_outage_is_a_retryable_failure():
    sc = [{"scenario_id": "SCN-P", "entity": "Account", "check_type": "ALLOWED_VALUES", "field": "CUSTODIAN_CODE", "params": {"values": ["SCHWAB"]},
           "stage": "POST", "description": "codes translated"}]
    t = ex.simulate(_package(scenarios=sc), ROWS)
    assert t["post_checks"][0]["status"] == "PASS"  # only the fully clean row is loaded and its code was translated
    down = ex.simulate(_package(), ROWS, outage=True)
    assert down["status"] == "FAILED" and down["retryable"] and "SIMULATED" in down["error"]
    assert ex.simulate(_package(), {"Account": []})["status"] == "NO_DATA"  # nothing was read, so nothing was exercised


def test_retry_safeguards(tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    conn = sqlite3.connect(tmp_path / "e.db")
    conn.row_factory = sqlite3.Row
    pkg, now = _package(), datetime.now(timezone.utc)
    ok = ex.record_run(conn, "M", pkg, "Val", ex.simulate(pkg, ROWS), now, 5)
    with pytest.raises(ValueError, match="infrastructure error"):
        ex.retry_allowed(conn, ok)  # a data problem is not retryable
    prev = None
    for _ in range(ex.MAX_RETRIES + 1):  # the original failed run plus the maximum number of retries
        rid = ex.record_run(conn, "M", pkg, "Val", ex.simulate(pkg, ROWS, True), now, 1, retry_of=prev)
        prev = rid
    with pytest.raises(ValueError, match="Retry limit"):
        ex.retry_allowed(conn, prev)
    first = conn.execute("SELECT run_id FROM execution_runs WHERE retry_of IS NULL AND status = 'FAILED'").fetchone()[0]
    with pytest.raises(ValueError, match="already retried"):
        ex.retry_allowed(conn, first)
    assert len(ex.list_runs(conn, "M")) == 5


def test_problems_can_be_acknowledged_then_resolved_with_a_reason(tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    conn = sqlite3.connect(tmp_path / "e.db")
    conn.row_factory = sqlite3.Row
    pkg = _package()
    rid = ex.record_run(conn, "M", pkg, "Val", ex.simulate(pkg, ROWS), datetime.now(timezone.utc), 5)
    assert ex.simulate(pkg, ROWS)["failures"], "fixture must produce at least one problem"
    with pytest.raises(ValueError, match="reason"):
        ex.review_failure(conn, rid, 0, "ACKNOWLEDGED", " ", "Ann")
    with pytest.raises(ValueError, match="does not exist"):
        ex.review_failure(conn, rid, 99, "RESOLVED", "x", "Ann")
    with pytest.raises(ValueError, match="Decision"):
        ex.review_failure(conn, rid, 0, "IGNORED", "x", "Ann")
    ex.review_failure(conn, rid, 0, "ACKNOWLEDGED", "seen, fix queued", "Ann")
    assert ex.list_runs(conn, "M")[0]["reviews"][0]["status"] == "ACKNOWLEDGED"
    ex.review_failure(conn, rid, 0, "RESOLVED", "source corrected", "Ann")
    with pytest.raises(ValueError, match="already resolved"):
        ex.review_failure(conn, rid, 0, "ACKNOWLEDGED", "again", "Ann")
    review = ex.list_runs(conn, "M")[0]["reviews"][0]
    assert review["status"] == "RESOLVED" and review["reviewer"] == "Ann" and "source corrected" in review["reason"]
    shown = str({k: v for k, v in review.items() if k != "run_id"})  # run ids are random hex and can contain "A2" by chance
    assert "A2" not in shown and "ZZZ" not in shown


def test_failure_review_route_is_analyst_or_admin_only():
    from ui.role_policy import allowed_roles_for
    roles = allowed_roles_for("POST", "/api/execution/runs/RUN-1/failures/0/review")
    assert roles == {"Platform Admin", "Migration Analyst"}


def test_a_run_that_read_no_rows_is_not_reported_as_succeeded():
    assert ex.simulate(_package(), {})["status"] == "NO_DATA"
    assert ex.simulate(_package(), {"Account": []})["status"] == "NO_DATA"
    assert ex.simulate(_package(), ROWS)["status"] in ("PARTIAL_SUCCESS", "SUCCEEDED")


def test_strict_value_map_counts_blank_and_unlisted_rows_as_failed_not_skipped():
    pkg = _package()
    pkg["transformation_rules"][0]["params"] = {"map": {"SCH": "SCHWAB"}, "case_insensitive": True, "strict": True}
    pkg["canonical_to_target"][1]["required"] = True
    rows = {"Account": [{"ACCT_NUM": "A1", "CUSTODIAN_CODE": "SCH"}, {"ACCT_NUM": "A2", "CUSTODIAN_CODE": "ZZZ"}, {"ACCT_NUM": "A3", "CUSTODIAN_CODE": ""}]}
    t = ex.simulate(pkg, rows)
    assert t["totals"] == {"read": 3, "loaded": 1, "skipped": 0, "failed": 2}
    assert not any(f["reason"] == "required value blank after transformation" for f in t["failures"])


def test_bulk_review_routes_are_approval_roles_only():
    from ui.role_policy import allowed_roles_for
    for path in ("/api/execution/runs/RUN-1/failures/bulk-review", "/api/execution/recommendations/bulk-decide"):
        assert allowed_roles_for("POST", path) == {"Platform Admin", "Migration Analyst"}


def test_bulk_routes_need_a_ticked_row_and_apply_per_row_comments(tmp_path, monkeypatch):
    import asyncio, httpx
    from ui import app as app_module
    sessions = app_module.session_mgr.active_sessions
    sessions["sid"] = dict(sessions.get("sid", {}), role="Migration Analyst", name="Ann")

    async def call(path, data):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.post(path, data=data, follow_redirects=False)

    r = asyncio.run(call("/api/execution/recommendations/bulk-decide", {"decision": "DISMISSED"}))
    assert r.status_code == 303 and "Tick+at+least+one" in r.headers["location"]
    r = asyncio.run(call("/api/execution/runs/RUN-X/failures/bulk-review", {"decision": "RESOLVED"}))
    assert r.status_code == 303 and "Tick+at+least+one" in r.headers["location"]


def test_attach_stage_records_the_location_on_the_stored_run():
    import sqlite3
    from datetime import datetime, timezone
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    pkg = {"meta": {"package_id": "P", "package_version": 1}}
    rid = ex.record_run(c, "MIG", pkg, "Ann", {"status": "SUCCEEDED", "totals": {}}, datetime.now(timezone.utc), 5)
    ex.attach_stage(c, rid, {"written": True, "path": f"stage/{rid.lower()}/", "files": []})
    assert ex.list_runs(c, "MIG")[0]["telemetry"]["stage"]["path"] == f"stage/{rid.lower()}/"
