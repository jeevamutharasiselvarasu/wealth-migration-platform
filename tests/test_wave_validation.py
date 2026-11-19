import asyncio
import sqlite3

import pytest

from control_plane.services import sql_target, validation_scenarios, wave_validation as wv
from tests.test_data_plane import FakeDB, _Conn, _Form

REGISTRY = [{"entity": "Household", "wave": 0, "depends_on": ""}, {"entity": "Advisor", "wave": 0, "depends_on": ""},
            {"entity": "Account", "wave": 1, "depends_on": "Household"}]


def _state(entity, status, stage="mirror", rows=10):
    return {"entity": entity, "stage": stage, "status": status, "rows_written": rows, "last_run_id": "r1", "package_version": "v1", "updated_utc": "2026-11-19 10:00:00.000"}


def _result(rid, entity, check, status, table="t", field="", wave=0):
    return {"result_id": rid, "wave": wave, "entity": entity, "check_name": check, "table_name": table, "field_name": field, "status": status,
            "observed_count": 3, "expected_count": None, "detail": f"{check} {status}", "source": "BUILTIN", "executed_utc": "2026-11-19 10:01:00"}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_references_round_trip_and_reject_anything_that_is_not_a_wave_finding():
    ref = wv.check_ref("Account", "KEYS_LOADED", "mig_ORION_MCL_ACCOUNT", "ACCT_NUM")
    assert wv.parse_ref(ref) == {"entity": "Account", "check": "KEYS_LOADED", "table": "mig_ORION_MCL_ACCOUNT", "field": "ACCT_NUM"}
    assert wv.parse_ref(wv.check_ref("Account", "ENTITY_BUILT", "", ""))["table"] == ""
    for bad in ("", "TVAL|Account|x|y|z", "WAVE|Account", "WAVE||x||"):
        assert wv.parse_ref(bad) is None


def test_the_board_groups_entities_by_wave_and_a_wave_passes_only_when_every_entity_is_validated():
    states = [_state("Household", "VALIDATED"), _state("Advisor", "FAILED"), _state("Account", "BUILT")]
    results = [_result(1, "Household", "STAGING_ROW_COUNT", "PASS"), _result(2, "Advisor", "KEYS_LOADED", "FAIL", "t", "ADV_ID"),
               _result(3, "Advisor", "REQUIRED_NOT_BLANK", "PASS")]
    keys = [{"result_id": 2, "row_key": "A-1"}, {"result_id": 2, "row_key": "A-2"}]
    board = wv.assemble("B1", REGISTRY, states, results, keys, [])
    w0, w1 = board["waves"]
    assert [w["wave"] for w in board["waves"]] == [0, 1] and w0["status"] == "FAIL" and w1["status"] == "PENDING"  # a built but unvalidated entity is not a pass
    adv = next(e for e in w0["entities"] if e["entity"] == "Advisor")
    assert adv["status"] == "FAILED" and adv["counts"]["FAIL"] == 1 and adv["checks"][0]["row_keys"] == ["A-1", "A-2"]
    assert next(e for e in w1["entities"])["depends_on"] == ["Household"]
    assert board["summary"] == {"PASS": 0, "FAIL": 1, "STALE": 0, "PENDING": 1} and board["built"] and board["findings"] == 3
    ok = wv.assemble("B1", REGISTRY[:1], [_state("Household", "VALIDATED")], [], [], [])
    assert ok["waves"][0]["status"] == "PASS"
    stale = wv.assemble("B1", REGISTRY, [_state("Household", "VALIDATED"), _state("Advisor", "VALIDATED"), _state("Account", "STALE", "canonical")], [], [], [])
    assert [w["status"] for w in stale["waves"]] == ["PASS", "STALE"]


def test_an_entity_that_was_never_built_is_not_built_and_the_board_says_nothing_was_built():
    board = wv.assemble("B1", REGISTRY, [], [], [], [])
    assert not board["built"] and all(e["status"] == "NOT_BUILT" for w in board["waves"] for e in w["entities"]) and board["waves"][0]["status"] == "PENDING"


def test_a_waiver_is_attached_to_its_finding_and_only_a_failure_can_be_excepted():
    fail = _result(2, "Advisor", "KEYS_LOADED", "FAIL", "t", "ADV_ID")
    waived = _result(3, "Advisor", "REQUIRED_NOT_BLANK", "WAIVED", "t", "X")
    waiver = {"entity": "Advisor", "check_name": "REQUIRED_NOT_BLANK", "table_name": "t", "field_name": "X", "reason": "legacy blanks", "approved_by": "Bo", "written_utc": "x"}
    board = wv.assemble("B1", REGISTRY, [_state("Advisor", "VALIDATED")], [fail, waived], [], [waiver])
    adv = next(e for w in board["waves"] for e in w["entities"] if e["entity"] == "Advisor")
    assert next(c for c in adv["checks"] if c["status"] == "WAIVED")["waiver"]["approved_by"] == "Bo" and adv["counts"]["WAIVED"] == 1
    assert wv.failing_finding(board, wv.check_ref("Advisor", "KEYS_LOADED", "t", "ADV_ID"))["wave"] == 0
    assert wv.failing_finding(board, wv.check_ref("Advisor", "REQUIRED_NOT_BLANK", "t", "X")) is None  # already waived
    assert wv.failing_finding(board, wv.check_ref("Nope", "KEYS_LOADED", "t", "")) is None


def _configured(conn, monkeypatch):
    sql_target.ensure_table(conn)
    monkeypatch.setenv("SQL_TARGET_PASSWORD", "pw")
    sql_target.save(conn, "Ann", {"server": "s.database.windows.net", "database_name": "xodus", "username": "u", "password_env_var": "SQL_TARGET_PASSWORD", "schema_name": "simplicity"})


def test_the_board_is_read_from_the_xodus_database_for_one_batch(conn, monkeypatch):
    _configured(conn, monkeypatch)
    db = FakeDB([("FROM xodus_ops.xodus_entity_registry", [("Household", 0, ""), ("Account", 1, "Household")]),
                 ("FROM xodus_ops.xodus_entity_state", [("Household", "mirror", "VALIDATED", 5, "r1", "v1", "2026-11-19")]),
                 ("FROM xodus_ops.xodus_validation_result", [(1, 0, "Household", "KEYS_LOADED", "t", "HH", "PASS", 0, None, "ok", "BUILTIN", "2026-11-19 10:00:00")]),
                 ("xodus_validation_row_key", []), ("xodus_wave_waiver", [])])
    board = wv.read_board(conn, "mig1", "MIG-1", "B1", connect=lambda cfg, pw: db)
    assert board["waves"][0]["status"] == "PASS" and board["waves"][1]["status"] == "PENDING"
    reads = [p for s, p in db.sql]
    assert ("mig1",) in reads and ("MIG-1", "B1") in reads  # values are parameters, never part of the SQL
    assert not db.committed and all(s.lstrip().upper().startswith("SELECT") for s, _ in db.sql)  # read-only


def test_a_waiver_is_written_with_its_approver_and_committed(conn, monkeypatch):
    _configured(conn, monkeypatch)
    db = FakeDB([])
    ref = wv.check_ref("Advisor", "KEYS_LOADED", "t", "ADV_ID")
    wv.record_waiver(conn, "MIG-1", "B1", ref, "known gap", "Bo", connect=lambda cfg, pw: db)
    sql, params = db.sql[0]
    assert sql.startswith("INSERT INTO xodus_ops.xodus_wave_waiver") and db.committed
    assert params == ("MIG-1", "B1", "Advisor", "KEYS_LOADED", "t", "ADV_ID", "known gap", "Bo")
    for bad in (("not a ref", "r", "Bo"), (ref, " ", "Bo"), (ref, "r", "")):
        with pytest.raises(ValueError):
            wv.record_waiver(conn, "MIG-1", "B1", *bad, connect=lambda cfg, pw: db)


def _app(monkeypatch, conn_, man, run, board_rows=None):
    import ui.app as app_module
    monkeypatch.setitem(app_module.state_store.questionnaire_data, "migration_id", "MIG-T")
    monkeypatch.setattr(app_module, "get_db_connection", lambda: _Conn(conn_))
    monkeypatch.setattr(app_module, "_data_plane_state", lambda m, refresh_runs=False: {"manifest": man, "run": run})
    monkeypatch.setattr(app_module, "_target_setup_audit", lambda *a, **k: None)
    return app_module


class _Req:
    pass


FAILING_BOARD = wv.assemble("B1", REGISTRY[:1], [_state("Household", "FAILED")], [_result(1, "Household", "KEYS_LOADED", "FAIL", "t", "HH")], [], [])
REF = wv.check_ref("Household", "KEYS_LOADED", "t", "HH")


def test_a_failed_finding_is_proposed_then_approved_by_someone_else_and_recorded_as_a_waiver(monkeypatch):
    conn_ = sqlite3.connect(":memory:")
    conn_.row_factory = sqlite3.Row
    man, run = {"migrationSlug": "mig1"}, {"batch_id": "B1"}
    app_module = _app(monkeypatch, conn_, man, run)
    monkeypatch.setattr(wv, "read_board", lambda *a, **k: FAILING_BOARD)
    written = []
    monkeypatch.setattr(wv, "record_waiver", lambda c, mig, batch, ref, reason, by, **k: written.append((mig, batch, ref, reason, by)) or wv.parse_ref(ref))
    users = iter([{"name": "Val", "role": "Validation Specialist"}, {"name": "Val", "role": "Migration Analyst"}, {"name": "Ana", "role": "Migration Analyst"}])
    monkeypatch.setattr(app_module, "get_current_user", lambda r: next(users))
    monkeypatch.setattr(app_module, "VALIDATION_ROLES", {"Validation Specialist", "Migration Analyst"})
    monkeypatch.setattr(app_module, "APPROVAL_ROLES", {"Migration Analyst"})
    loc = asyncio.run(app_module.propose_wave_exception(_Req(), check_ref=REF, reason="known gap")).headers["location"]
    assert "notice=" in loc and "wave-board" in loc
    exc = validation_scenarios.list_exceptions(conn_, "MIG-T", "WAVE:B1")[0]
    assert exc["check_ref"] == REF and exc["proposed_by"] == "Val" and exc["status"] == "PROPOSED"
    # the proposer cannot decide it: nothing is written
    loc = asyncio.run(app_module.decide_wave_exception(_Req(), exc["exception_id"], decision="APPROVED", comments="")).headers["location"]
    assert "error=" in loc and not written
    assert validation_scenarios.list_exceptions(conn_, "MIG-T", "WAVE:B1")[0]["status"] == "PROPOSED"
    loc = asyncio.run(app_module.decide_wave_exception(_Req(), exc["exception_id"], decision="APPROVED", comments="")).headers["location"]
    assert "notice=" in loc and written == [("MIG-T", "B1", REF, "known gap", "Ana")]
    assert validation_scenarios.list_exceptions(conn_, "MIG-T", "WAVE:B1")[0]["status"] == "APPROVED"


def test_a_decision_is_not_kept_when_the_waiver_cannot_be_written(monkeypatch):
    conn_ = sqlite3.connect(":memory:")
    conn_.row_factory = sqlite3.Row
    app_module = _app(monkeypatch, conn_, {"migrationSlug": "mig1"}, {"batch_id": "B1"})
    validation_scenarios.ensure_tables(conn_)
    eid = validation_scenarios.propose_exception(conn_, "MIG-T", "WAVE:B1", REF, "known gap", "Val")
    conn_.commit()

    def down(*a, **k):
        raise ValueError("Could not sign in to xodus")
    monkeypatch.setattr(wv, "record_waiver", down)
    monkeypatch.setattr(app_module, "get_current_user", lambda r: {"name": "Ana", "role": "Migration Analyst"})
    monkeypatch.setattr(app_module, "APPROVAL_ROLES", {"Migration Analyst"})
    loc = asyncio.run(app_module.decide_wave_exception(_Req(), eid, decision="APPROVED", comments="")).headers["location"]
    assert "error=" in loc and "Could+not+sign+in" in loc
    assert validation_scenarios.list_exceptions(conn_, "MIG-T", "WAVE:B1")[0]["status"] == "PROPOSED"  # still open: nothing was waived


def test_only_a_failed_finding_of_the_latest_batch_can_be_excepted(monkeypatch):
    conn_ = sqlite3.connect(":memory:")
    conn_.row_factory = sqlite3.Row
    app_module = _app(monkeypatch, conn_, {"migrationSlug": "mig1"}, {"batch_id": "B1"})
    monkeypatch.setattr(wv, "read_board", lambda *a, **k: FAILING_BOARD)
    monkeypatch.setattr(app_module, "get_current_user", lambda r: {"name": "Val", "role": "Validation Specialist"})
    monkeypatch.setattr(app_module, "VALIDATION_ROLES", {"Validation Specialist"})
    loc = asyncio.run(app_module.propose_wave_exception(_Req(), check_ref=wv.check_ref("Household", "OTHER", "t", ""), reason="x")).headers["location"]
    assert "error=" in loc and validation_scenarios.list_exceptions(conn_, "MIG-T", "WAVE:B1") == []
    monkeypatch.setattr(app_module, "get_current_user", lambda r: {"name": "Ann", "role": "Viewer"})
    assert "error=" in asyncio.run(app_module.propose_wave_exception(_Req(), check_ref=REF, reason="x")).headers["location"]


def test_the_board_renders_failures_row_keys_restart_and_exception_forms(monkeypatch):
    import ui.app as app_module
    board = wv.assemble("B1", REGISTRY, [_state("Household", "FAILED"), _state("Account", "STALE", "canonical", None)],
                        [_result(1, "Household", "KEYS_LOADED", "FAIL", "t", "HH")], [{"result_id": 1, "row_key": "H-9"}], [])
    tpl = app_module.templates.env.get_template("_wave_board.html")
    ctx = {"dp": {"board": board, "running": {"run": False}}, "can_decide_exception": True, "can_run_validation": True, "current_user_name": "Ann", "wave_exceptions": {}}
    html = tpl.render(**ctx)
    assert 'data-testid="wave-0"' in html and 'data-testid="wave-1"' in html and "FAILED" in html and "STALE" in html
    assert "H-9" in html and "1 failing row key(s)" in html  # keys only
    assert 'action="/api/wave-validation/exceptions"' in html and 'name="check_ref"' in html
    restart = html.split('data-testid="restart-from-0"')[1].split("</form>")[0]
    assert restart.count('name="entities"') == 3 and "Restart from wave 0" in restart  # the failed wave and everything after it
    assert 'data-testid="restart-from-1"' in html
    waiting = tpl.render(**{**ctx, "wave_exceptions": {board["waves"][0]["entities"][0]["checks"][0]["ref"]: {"status": "PROPOSED", "proposed_by": "Ann", "reason": "r", "exception_id": "EXC-1"}}})
    assert "Waiting for a different approver." in waiting and "/api/wave-validation/exceptions/EXC-1/decision" not in waiting
    other = tpl.render(**{**ctx, "current_user_name": "Bo", "wave_exceptions": {board["waves"][0]["entities"][0]["checks"][0]["ref"]: {"status": "PROPOSED", "proposed_by": "Ann", "reason": "r", "exception_id": "EXC-1"}}})
    assert "/api/wave-validation/exceptions/EXC-1/decision" in other
    assert "could not be read" in tpl.render(dp={"board_error": "no sql", "running": {}}, wave_exceptions={})
    assert "No entity has been built" in tpl.render(dp={"board": wv.assemble("B1", REGISTRY, [], [], [], []), "running": {}}, wave_exceptions={})


def test_failing_keys_show_the_source_file_and_row_when_they_could_be_traced():
    results = [_result(1, "Household", "KEYS_LOADED", "FAIL", "t", "HH")]
    keys = [{"result_id": 1, "row_key": "H-9", "source_file": "raw/corecap/households.csv", "source_row": 42}, {"result_id": 1, "row_key": "H-10", "source_file": None, "source_row": None}]
    board = wv.assemble("B1", REGISTRY[:1], [_state("Household", "FAILED")], results, keys, [])
    assert board["waves"][0]["entities"][0]["checks"][0]["row_keys"] == ["H-9 (households.csv row 42)", "H-10"]
