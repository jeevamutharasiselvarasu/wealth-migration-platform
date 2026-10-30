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
    assert ex.simulate(_package(), {"Account": []})["status"] == "SUCCEEDED"


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
