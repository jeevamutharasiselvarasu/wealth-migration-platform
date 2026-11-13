import sqlite3

import pytest

from control_plane.services import sql_target as st
from control_plane.services import target_validation as tv

ENT = {
    "Account": {"staging_table": "Account", "expected_rows": 60, "key": "ACCT_NUM", "unmapped_required": [],
                "tables": {"ORION_MCL_ACCOUNT": {"columns": {"ACCT_NUM": "ACCOUNT_NUMBER", "HH_ID": "HH_CODE"}, "required": ["ACCOUNT_NUMBER", "CUSTODIAN"]}}},
    "Household": {"staging_table": "Household", "expected_rows": 30, "key": "HH_ID", "unmapped_required": ["ORION_MCL_HOUSEHOLD.HOUSEHOLD_SSN"],
                  "tables": {"ORION_MCL_HOUSEHOLD": {"columns": {"HH_ID": "HH_GROUPING_CODE"}, "required": []}}},
}


def plan():
    return tv.build_plan(schema="simplicity", staging_schema="staging", entities=ENT)


def by(checks, name, entity=None):
    return [c for c in checks if c["check"] == name and (entity is None or c["entity"] == entity)]


def test_plan_builds_one_count_query_per_check_with_bracketed_identifiers():
    p = plan()
    kinds = {(c["check"], c["entity"], c["field"]) for c in p}
    assert ("STAGING_ROW_COUNT", "Account", "") in kinds and ("KEYS_LOADED", "Account", "ACCT_NUM") in kinds
    assert ("TARGET_KEY_UNIQUE", "Account", "ACCOUNT_NUMBER") in kinds and ("REQUIRED_NOT_BLANK", "Account", "CUSTODIAN") in kinds
    assert any(c["check"] == "NO_ORPHANS" and c["entity"] == "Account" for c in p)
    sqls = [c["sql"] for c in p if c["sql"]]
    assert all(q.startswith("SELECT COUNT(") for q in sqls)
    assert any("FROM [staging].[Account]" in q for q in sqls) and any("[simplicity].[ORION_MCL_ACCOUNT]" in q for q in sqls)
    assert not any("DROP" in q.upper() or "DELETE" in q.upper() or "UPDATE" in q.upper() for q in sqls)


def test_unsafe_identifiers_are_refused():
    bad = {"Account": {**ENT["Account"], "tables": {"T; DROP TABLE x": {"columns": {}, "required": []}}}}
    with pytest.raises(ValueError):
        tv.build_plan(schema="simplicity", staging_schema="staging", entities=bad)


def test_evaluate_turns_counts_into_findings_without_values():
    def execute(sql):
        if "FROM [staging].[Account]" in sql and sql.startswith("SELECT COUNT(*) FROM"):
            return 58  # staging short of the 60 the platform wrote
        if "CUSTODIAN" in sql and "IS NULL OR" in sql:
            return 3
        return 0
    checks = tv.evaluate(plan(), execute, {"status": "SUCCEEDED"})
    assert by(checks, "STAGING_ROW_COUNT", "Account")[0]["status"] == "FAIL" and "58" in by(checks, "STAGING_ROW_COUNT", "Account")[0]["detail"]
    assert by(checks, "STAGING_ROW_COUNT", "Household")[0]["status"] == "FAIL"  # 0 rows seen, 30 written
    req = {c["field"]: c for c in by(checks, "REQUIRED_NOT_BLANK")}
    assert req["CUSTODIAN"]["status"] == "FAIL" and "3 target row" in req["CUSTODIAN"]["detail"] and req["ACCOUNT_NUMBER"]["status"] == "PASS"
    assert by(checks, "REQUIRED_FIELDS_MAPPED", "Household")[0]["status"] == "WARN"
    assert by(checks, "PIPELINE_RUN")[0]["status"] == "PASS"
    assert all(c["ref"].startswith(c["check"] + "|") for c in checks) and tv.overall(checks) == "FAIL"


def test_all_clear_passes_and_a_missing_or_failed_run_is_not_a_pass():
    ok = lambda sql: 60 if sql.startswith("SELECT COUNT(*) FROM [staging].[Account]") else (30 if sql.startswith("SELECT COUNT(*) FROM [staging].[Household]") else 0)
    clean = {**ENT["Household"], "unmapped_required": []}
    p = tv.build_plan(schema="simplicity", staging_schema="staging", entities={**ENT, "Household": clean})
    checks = tv.evaluate(p, ok, {"status": "SUCCEEDED"})
    assert tv.overall(checks) == "PASS"
    assert by(tv.evaluate(p, ok, None), "PIPELINE_RUN")[0]["status"] == "NOT_EVALUATED"
    assert by(tv.evaluate(p, ok, {"status": "FAILED"}), "PIPELINE_RUN")[0]["status"] == "FAIL"


def test_a_query_that_cannot_run_is_not_evaluated_not_passed():
    def execute(sql):
        raise RuntimeError("Invalid object name 'staging.Account'")
    checks = tv.evaluate(plan(), execute, {"status": "SUCCEEDED"})
    assert by(checks, "STAGING_ROW_COUNT", "Account")[0]["status"] == "NOT_EVALUATED" and tv.overall(checks) != "PASS"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    st.ensure_table(c)
    st.save(c, "Ann", {"server": "s.database.windows.net", "database_name": "xodus", "username": "u", "password_env_var": "SQL_TEST_PW", "schema_name": "simplicity"})
    monkeypatch.setenv("SQL_TEST_PW", "secret-pw")
    return c


class _Cur:
    def __init__(self, log):
        self.log, self.last = log, None

    def execute(self, sql):
        self.log.append(sql)
        self.last = sql

    def fetchone(self):
        return (0,)


class _Db:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)

    def close(self):
        self.log.append("CLOSED")


def test_run_stores_the_result_reads_only_and_closes_the_connection(conn):
    log = []
    out = tv.run(conn, "MIG-1", "Ann", ENT, connect=lambda cfg, pw: _Db(log))
    assert out["run_id"].startswith("TVAL-") and log[-1] == "CLOSED" and all(q.startswith("SELECT") for q in log[:-1])
    assert tv.latest(conn, "MIG-1")["run_id"] == out["run_id"] and tv.latest(conn, "MIG-2") is None
    assert out["status"] in ("FAIL", "WARN")  # staging counts are 0 against 60/30 written; no pipeline run recorded


def test_sign_in_failure_is_readable_and_hides_the_password(conn):
    def boom(cfg, pw):
        raise RuntimeError("Login failed secret-pw")
    with pytest.raises(ValueError) as e:
        tv.run(conn, "MIG-1", "Ann", ENT, connect=boom)
    assert "secret-pw" not in str(e.value) and "Could not sign in" in str(e.value)


def test_routes_are_role_gated_like_mapping_validation():
    from ui.role_policy import allowed_roles_for
    assert allowed_roles_for("POST", "/api/target-validation/run") == allowed_roles_for("POST", "/api/validation/run")
    assert allowed_roles_for("POST", "/api/target-validation/exceptions") == allowed_roles_for("POST", "/api/validation/exceptions")
    assert allowed_roles_for("POST", "/api/target-validation/exceptions/EXC-1/decision") == allowed_roles_for("POST", "/api/validation/exceptions/EXC-1/decision")


def test_entity_without_a_mapped_key_column_is_compared_by_row_count():
    ent = {"Client": {"staging_table": "Client", "expected_rows": 40, "key": "CLIENT_ID", "unmapped_required": [],
                      "tables": {"ORION_MCL_CLIENT": {"columns": {"CITY": "CITY"}, "required": []}}}}
    p = tv.build_plan(schema="simplicity", staging_schema="staging", entities=ent)
    assert not any(c["check"] == "KEYS_LOADED" for c in p)
    ok = tv.evaluate(p, lambda q: 40, None)
    assert [c["status"] for c in ok if c["check"] == "ROWS_LOADED"] == ["PASS"]
    short = tv.evaluate(p, lambda q: 39 if "ORION_MCL_CLIENT" in q else 40, None)
    assert [c["status"] for c in short if c["check"] == "ROWS_LOADED"] == ["FAIL"]
    none = {"Client": {**ent["Client"], "expected_rows": None}}
    assert [c["status"] for c in tv.evaluate(tv.build_plan(schema="simplicity", staging_schema="staging", entities=none), lambda q: 5, None) if c["check"] == "ROWS_LOADED"] == ["WARN"]


def test_coded_values_are_compared_with_the_targets_reference_list():
    ent = {"Account": {**ENT["Account"], "tables": {"ORION_MCL_ACCOUNT": {**ENT["Account"]["tables"]["ORION_MCL_ACCOUNT"],
                                                                          "code_lists": {"CUSTODIAN": ["Schwab", "Pershing", "O'Neil & Co", " "], "ACCOUNT_TYPE": []}}}}}
    p = tv.build_plan(schema="simplicity", staging_schema="staging", entities=ent)
    ref = by(p, "VALUES_IN_REFERENCE_LIST")
    assert len(ref) == 1 and ref[0]["field"] == "CUSTODIAN" and ref[0]["expected"] == 3  # blank codes and empty lists are skipped
    assert "NOT IN (N'O''Neil & Co', N'Pershing', N'Schwab')" in ref[0]["sql"]  # quotes escaped, never a value read back
    found = by(tv.evaluate(ref, lambda q: 2, None), "VALUES_IN_REFERENCE_LIST")
    assert found[0]["status"] == "FAIL" and "2 target row(s)" in found[0]["detail"]
    assert by(tv.evaluate(ref, lambda q: 0, None), "VALUES_IN_REFERENCE_LIST")[0]["status"] == "PASS"
    big = {"Account": {**ent["Account"], "tables": {"T": {"columns": {}, "required": [], "code_lists": {"C": [str(i) for i in range(tv.MAX_LIST_CODES + 1)]}}}}}
    assert by(tv.evaluate(tv.build_plan(schema="s", staging_schema="staging", entities=big), lambda q: 0, None), "VALUES_IN_REFERENCE_LIST")[0]["status"] == "NOT_EVALUATED"


def test_an_orphan_finding_names_the_entity_the_reference_points_at():
    from control_plane.services import target_validation as tv
    assert tv._target_of({"field": "ACCOUNT_NUMBER → Account.ACCOUNT_NUMBER"}) == "account"
    assert tv._target_of({"field": "HH_GROUPING_CODE → Household.HH_GROUPING_CODE"}) == "household"
    assert tv._target_of({"field": ""}) == "record"
