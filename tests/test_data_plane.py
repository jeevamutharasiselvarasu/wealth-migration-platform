import json
import sqlite3

import httpx
import pytest

from control_plane.services import adf_runs, azure_connection, deployment_check, git_connection, git_pull_requests, promotion_gate, sql_target, target_validation

MANIFEST = {"version": "v1abc", "setupPipeline": "PL_XODUS_Setup_migx", "migrationSlug": "migx", "liveSchema": "simplicity",
            "objects": {"tables": ["xodus_canon.migx_Account"], "procedures": ["xodus_canon.usp_canon_migx_Account"]},
            "liveTables": [{"entity": "Account", "live_table": "ORION_MCL_ACCOUNT", "mirror_table": "migx_ORION_MCL_ACCOUNT",
                            "columns": ["ACCOUNT_NUMBER", "CUSTODIAN"], "key_columns": ["ACCOUNT_NUMBER"]}]}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_deployment_check_blocks_missing_objects_old_versions_and_unmapped_not_null_columns():
    live = {"ORION_MCL_ACCOUNT": {"ACCOUNT_NUMBER": {"type": "nvarchar", "max_length": 255, "nullable": False},
                                  "CUSTODIAN": {"type": "nvarchar", "max_length": 255, "nullable": False},
                                  "FUND_NAME": {"type": "nvarchar", "max_length": 255, "nullable": False},
                                  "OPEN_DATE": {"type": "date", "max_length": None, "nullable": True}}}
    ok = deployment_check.evaluate(MANIFEST, "v1abc", {"a": True}, "v1abc", {"ORION_MCL_ACCOUNT": {k: v for k, v in live["ORION_MCL_ACCOUNT"].items() if k != "FUND_NAME"}})
    assert ok["status"] == "PASS"
    bad = deployment_check.evaluate(MANIFEST, "old", {"a": True, "b": False}, None, live)
    by = {c["check"]: c for c in bad["checks"]}
    assert bad["status"] == "FAIL" and by["XODUS_VERSION"]["status"] == "FAIL" and "PL_XODUS_Setup_migx" in by["XODUS_VERSION"]["detail"]
    assert by["XODUS_OBJECTS"]["status"] == "FAIL" and "b" in by["XODUS_OBJECTS"]["detail"] and by["LANDING_VERSION"]["status"] == "FAIL"
    assert by["LIVE_REQUIRED_MAPPED"]["status"] == "FAIL" and "FUND_NAME" in by["LIVE_REQUIRED_MAPPED"]["detail"]
    gone = deployment_check.evaluate(MANIFEST, "v1abc", {}, "v1abc", {})
    assert any(c["check"] == "LIVE_TABLE" and "never creates" in c["detail"] for c in gone["checks"])


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, part), (code, body) in self.routes.items():
            if m == method and part in url:
                return httpx.Response(code, json=body)
        return httpx.Response(404, json={"message": "Not Found"})


def _azure(conn, monkeypatch, status="CONNECTED"):
    azure_connection.ensure_table(conn)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s")
    azure_connection.save(conn, "Ann", {"tenant_id": "00000000-0000-0000-0000-000000000001", "client_id": "00000000-0000-0000-0000-000000000002",
                                        "subscription_id": "00000000-0000-0000-0000-000000000003", "resource_group": "rg", "factory_name": "adf-x",
                                        "secret_env_var": "AZURE_CLIENT_SECRET"})
    conn.execute("UPDATE azure_connection_config SET status = ?", (status,))


def test_runs_start_only_xodus_pipelines_record_the_approver_and_read_failures(conn, monkeypatch):
    _azure(conn, monkeypatch)
    http = FakeHTTP({("POST", "/oauth2/v2.0/token"): (200, {"access_token": "t"}), ("POST", "/pipelines/PL_XODUS_Run/createRun"): (200, {"runId": "R1"}),
                     ("GET", "/pipelineruns/R1"): (200, {"status": "Failed", "message": "boom"}),
                     ("POST", "/pipelineruns/R1/queryActivityruns"): (200, {"value": [{"activityName": "Copy_Raw", "status": "Failed", "error": {"message": "column X"}}]})})
    with pytest.raises(ValueError):
        adf_runs.start(conn, "MIG-1", "SomeOtherPipeline", {}, "Ann", http=http)
    req = adf_runs.start(conn, "MIG-1", "PL_XODUS_Run", {"MigrationId": "MIG-1", "BatchId": "B1"}, "Ann", batch_id="B1", http=http)
    assert req["adf_run_id"] == "R1" and req["requested_by"] == "Ann"
    assert http.calls[-1][2]["json"] == {"MigrationId": "MIG-1", "BatchId": "B1"} and "Bearer t" in http.calls[-1][2]["headers"]["Authorization"]
    run = adf_runs.refresh(conn, req, http=http)
    assert run["status"] == "FAILED" and run["failures"] == [{"activity": "Copy_Raw", "type": None, "error": "column X"}]
    assert adf_runs.latest(conn, "MIG-1", "PL_XODUS_Run")["status"] == "FAILED"
    conn.execute("UPDATE azure_connection_config SET status = 'FAILED'")
    with pytest.raises(ValueError):
        adf_runs.start(conn, "MIG-1", "PL_XODUS_Run", {}, "Ann", http=http)


def _git(conn, monkeypatch):
    git_connection.ensure_table(conn)
    monkeypatch.setenv("GITHUB_ADF_TOKEN", "tok")
    git_connection.save(conn, "Ann", {"repository": "o/repo", "branch": "main", "adf_folder": "adf", "token_env_var": "GITHUB_ADF_TOKEN"})
    conn.execute("UPDATE git_connection_config SET status = 'CONNECTED'")


def test_pull_request_goes_to_its_own_branch_and_merges_only_when_open(conn, monkeypatch):
    _git(conn, monkeypatch)
    http = FakeHTTP({("GET", "/git/ref/heads/main"): (200, {"object": {"sha": "H"}}), ("GET", "/git/commits/H"): (200, {"tree": {"sha": "T"}}),
                     ("GET", "/git/trees/T"): (200, {"truncated": False, "tree": [
                         {"path": "adf/pipeline/PL_XODUS_MIG_OLD.json", "type": "blob"}, {"path": "adf/dataset/DS_StagingSql_Account.json", "type": "blob"},
                         {"path": "adf/linkedService/LS_BLOB.json", "type": "blob"}, {"path": "adf/pipeline/PL_Other.json", "type": "blob"}]}),
                     ("POST", "/git/trees"): (201, {"sha": "T2"}), ("POST", "/git/commits"): (201, {"sha": "C2"}),
                     ("GET", "/git/ref/heads/xodus/"): (404, {"message": "Not Found"}), ("POST", "/git/refs"): (201, {}),
                     ("POST", "/pulls"): (201, {"number": 7, "html_url": "https://github.com/o/repo/pull/7"}),
                     ("GET", "/pulls/7/reviews"): (200, []), ("GET", "/pulls/7"): (200, {"state": "open", "merged": False, "mergeable": True}),
                     ("PUT", "/pulls/7/merge"): (200, {"sha": "M1", "merged": True})})
    pr = git_pull_requests.open_pull_request(conn, "Ann", "MIG-1", {"pipeline/PL_XODUS_Run.json": "{}"}, "v1abc", "ART-1", "body", http=http)
    assert pr["number"] == 7 and pr["branch"] == "xodus/mig-1-v1abc" and pr["state"] == "OPEN"
    assert pr["removed"] == ["adf/dataset/DS_StagingSql_Account.json", "adf/pipeline/PL_XODUS_MIG_OLD.json"]  # old XODUS files only
    tree = next(c for c in http.calls if c[0] == "POST" and c[1].endswith("/git/trees"))[2]["json"]["tree"]
    assert {e["path"] for e in tree if e.get("sha", "x") is None} == set(pr["removed"])
    created = next(c for c in http.calls if c[0] == "POST" and c[1].endswith("/pulls"))
    assert created[2]["json"]["head"] == "xodus/mig-1-v1abc" and created[2]["json"]["base"] == "main"
    assert not any(c[0] == "PATCH" and "/heads/main" in c[1] for c in http.calls)  # the configured branch is never written directly
    with pytest.raises(ValueError):  # one open pull request per package version
        git_pull_requests.open_pull_request(conn, "Ann", "MIG-1", {"pipeline/x.json": "{}"}, "v1abc", "ART-1", "body", http=http)
    merged = git_pull_requests.merge(conn, 7, "Bo", http=http)
    put = next(c for c in http.calls if c[0] == "PUT")
    assert merged["state"] == "MERGED" and merged["approved_by"] == "Bo" and "Approved in XODUS by Bo" in put[2]["json"]["commit_message"]
    assert git_pull_requests.list_for(conn, "MIG-1")[0]["approved_by"] == "Bo"


class FakeDB:
    def __init__(self, answers):
        self.answers, self.sql, self.committed = answers, [], False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        self.last = next((a for k, a in self.answers if k in sql), [])

    def fetchall(self):
        return self.last

    def commit(self):
        self.committed = True

    def close(self):
        pass


def test_gate_records_the_mirror_counts_with_the_signer_and_reads_back(conn, monkeypatch):
    sql_target.ensure_table(conn)
    monkeypatch.setenv("SQL_TARGET_PASSWORD", "pw")
    sql_target.save(conn, "Ann", {"server": "s.database.windows.net", "database_name": "xodus", "username": "u", "password_env_var": "SQL_TARGET_PASSWORD", "schema_name": "simplicity"})
    db = FakeDB([("COUNT_BIG(*) FROM [xodus_mirror].[migx_ORION_MCL_ACCOUNT]", [(60,)])])
    gate = promotion_gate.record(conn, "MIG-1", "B1", "TVAL-1", "Val", "2026-11-10T10:00:00", MANIFEST, connect=lambda cfg, pw: db)
    assert gate["counts"] == {"migx_ORION_MCL_ACCOUNT": 60} and db.committed
    insert = next((s, p) for s, p in db.sql if s.startswith("INSERT INTO xodus_ops.xodus_promotion_gate"))
    assert insert[1][:4] == ("MIG-1", "B1", "TVAL-1", "Val") and json.loads(insert[1][5]) == {"migx_ORION_MCL_ACCOUNT": 60}
    empty = FakeDB([("COUNT_BIG", [(0,)])])
    with pytest.raises(ValueError):
        promotion_gate.record(conn, "MIG-1", "B1", "TVAL-1", "Val", "t", MANIFEST, connect=lambda cfg, pw: empty)


def test_stage_10_checks_values_fit_the_live_types_and_lengths():
    ent = {"Account": {"staging_table": "migx_Account", "expected_rows": None, "expected_sql": "SELECT COUNT(*) FROM [xodus_raw].[migx_corecap_accounts]",
                       "key": "ACCT_NUM", "unmapped_required": [],
                       "tables": {"migx_ORION_MCL_ACCOUNT": {"columns": {"ACCT_NUM": "ACCOUNT_NUMBER", "OPEN_DATE": "OPEN_DATE"}, "required": ["ACCOUNT_NUMBER"],
                                                             "types": {"OPEN_DATE": "date"}, "max_lengths": {"ACCOUNT_NUMBER": 20}}}}}
    plan = target_validation.build_plan(schema="xodus_mirror", staging_schema="xodus_canon", entities=ent)
    kinds = {p["check"] for p in plan}
    assert {"VALUES_FIT_TYPE", "VALUES_FIT_LENGTH"} <= kinds
    fit = next(p for p in plan if p["check"] == "VALUES_FIT_TYPE")
    assert "TRY_CONVERT(date," in fit["sql"] and "[xodus_mirror].[migx_ORION_MCL_ACCOUNT]" in fit["sql"]
    answers = {"[xodus_raw]": 89, "COUNT(*) FROM [xodus_canon]": 88}
    checks = target_validation.evaluate(plan, lambda q: next((v for k, v in answers.items() if k in q), 0), {"status": "SUCCEEDED"})
    rows = next(c for c in checks if c["check"] == "STAGING_ROW_COUNT")
    assert rows["status"] == "FAIL" and "raw file delivered 89" in rows["detail"]


def test_rows_loaded_compares_with_the_canonical_table_when_no_key_is_mapped():
    ent = {"Client": {"staging_table": "migx_Client", "expected_rows": None, "expected_sql": "SELECT COUNT(*) FROM [xodus_raw].[migx_contacts]", "key": "CLIENT_ID",
                      "unmapped_required": [], "tables": {"migx_ORION_MCL_CLIENT": {"columns": {"CITY": "CITY"}, "required": []}}}}
    plan = target_validation.build_plan(schema="xodus_mirror", staging_schema="xodus_canon", entities=ent)
    rows = next(p for p in plan if p["check"] == "ROWS_LOADED")
    assert rows["expected_sql"] == "SELECT COUNT(*) FROM [xodus_canon].[migx_Client]"
    checks = target_validation.evaluate([rows], lambda q: 40, {"status": "SUCCEEDED"})
    assert checks[0]["status"] == "PASS"


def test_failed_checks_name_their_rows_by_key_only():
    ent = {"Client": {"staging_table": "migx_Client", "expected_rows": None, "key": "CLIENT_ID", "unmapped_required": [],
                      "tables": {"migx_ORION_MCL_CLIENT": {"columns": {"DATE_OF_BIRTH": "REGISTRATION_DOB"}, "required": [], "types": {"REGISTRATION_DOB": "date"},
                                                           "row_key": "xodus_source_key"}}}}
    plan = target_validation.build_plan(schema="xodus_mirror", staging_schema="xodus_canon", entities=ent)
    fit = next(p for p in plan if p["check"] == "VALUES_FIT_TYPE")
    assert fit["keys_sql"].startswith("SELECT TOP (50) CAST([xodus_source_key] AS nvarchar(400)) FROM [xodus_mirror].[migx_ORION_MCL_CLIENT] WHERE ")
    checks = target_validation.evaluate([fit], lambda q: 2, None, lambda q: ["C-1", "C-2"])
    row = next(c for c in checks if c["check"] == "VALUES_FIT_TYPE")
    assert row["status"] == "FAIL" and row["row_keys"] == ["C-1", "C-2"]


def test_live_validation_checks_what_landed_and_names_duplicate_keys():
    from control_plane.services import live_validation as lv
    man = {**MANIFEST, "liveTables": [{**MANIFEST["liveTables"][0]}, {"entity": "Client", "live_table": "ORION_MCL_CLIENT", "mirror_table": "migx_ORION_MCL_CLIENT",
                                                                   "columns": ["CITY"], "key_columns": []}]}
    gate = {"counts": {"migx_ORION_MCL_ACCOUNT": 60, "migx_ORION_MCL_CLIENT": 40},
            "promotion": {"tables": [{"live_table": "ORION_MCL_ACCOUNT", "inserted": 55, "updated": 5}, {"live_table": "ORION_MCL_CLIENT", "inserted": 0, "updated": 0}]}}
    live = {"ORION_MCL_ACCOUNT": {"ACCOUNT_NUMBER": {"type": "nvarchar", "nullable": False}, "CUSTODIAN": {"type": "nvarchar", "nullable": True}}}
    plan = lv.build_plan(man, live, gate)
    assert {p["check"] for p in plan} == {"LIVE_ROWS_PRESENT", "LIVE_KEY_UNIQUE", "LIVE_NOT_BLANK"}
    answers = {"COUNT(*) - COUNT(DISTINCT": 1, "[ORION_MCL_ACCOUNT]": 60, "[ORION_MCL_CLIENT]": 40, "= N''": 0}
    checks = lv.evaluate(plan, lambda q: next(v for k, v in answers.items() if k in q), lambda q: ["A-9"])
    by = {(c["check"], c["table"]): c for c in checks}
    assert by[("LIVE_ROWS_PRESENT", "ORION_MCL_ACCOUNT")]["status"] == "PASS"
    assert by[("LIVE_ROWS_PRESENT", "ORION_MCL_CLIENT")]["status"] == "PASS" and "no key" in by[("LIVE_ROWS_PRESENT", "ORION_MCL_CLIENT")]["detail"]
    dup = by[("LIVE_KEY_UNIQUE", "ORION_MCL_ACCOUNT")]
    assert dup["status"] == "FAIL" and dup["row_keys"] == ["A-9"]
    short = lv.evaluate(lv.build_plan(man, live, {**gate, "promotion": {"tables": [{"live_table": "ORION_MCL_ACCOUNT", "inserted": 50, "updated": 0}]}}),
                        lambda q: next(v for k, v in answers.items() if k in q), lambda q: [])
    assert next(c for c in short if c["check"] == "LIVE_ROWS_PRESENT" and c["table"] == "ORION_MCL_ACCOUNT")["status"] == "FAIL"


def _request(conn, status, pipeline="PL_XODUS_Run", rid="RUN-T1"):
    from control_plane.services import adf_runs
    adf_runs.ensure_table(conn)
    conn.execute("INSERT INTO adf_run_requests (request_id, migration_id, batch_id, pipeline, adf_run_id, requested_by, requested_utc, status) "
                 "VALUES (?, 'MIG-T', 'B1', ?, 'adf-1', 'me', '2026-11-13T10:00:00+00:00', ?)", (rid, pipeline, status))


def test_a_pipeline_that_is_running_is_not_started_again(monkeypatch):
    import sqlite3

    import ui.app as app_module
    from control_plane.services import adf_runs
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _request(conn, "INPROGRESS")
    monkeypatch.setattr(adf_runs, "refresh", lambda c, req, **k: {**req, "status": "INPROGRESS"})
    busy = app_module._run_in_progress(conn, "MIG-T", "PL_XODUS_Run")
    assert busy and "already running" in app_module._already_running_message(busy)
    assert app_module._run_in_progress(conn, "MIG-T", "PL_XODUS_Promote") is None  # another pipeline is free


def test_a_stored_status_that_is_old_does_not_block_once_data_factory_says_it_finished(monkeypatch):
    import sqlite3

    import ui.app as app_module
    from control_plane.services import adf_runs
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _request(conn, "QUEUED")
    monkeypatch.setattr(adf_runs, "refresh", lambda c, req, **k: {**req, "status": "SUCCEEDED"})
    assert app_module._run_in_progress(conn, "MIG-T", "PL_XODUS_Run") is None
    _request(conn, "FAILED", "PL_XODUS_Setup_x", "RUN-T2")
    assert app_module._run_in_progress(conn, "MIG-T", "PL_XODUS_Setup_x") is None  # finished runs never block


def test_when_data_factory_cannot_be_asked_the_run_counts_as_still_going(monkeypatch):
    import sqlite3

    import ui.app as app_module
    from control_plane.services import adf_runs
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _request(conn, "INPROGRESS")

    def down(c, req, **k):
        raise ValueError("Data Factory is not reachable")
    monkeypatch.setattr(adf_runs, "refresh", down)
    assert app_module._run_in_progress(conn, "MIG-T", "PL_XODUS_Run")


def test_downstream_entities_are_every_dependant_directly_or_through_others():
    import ui.app as app_module
    depends = {"Account": ["Household"], "Position": ["Account"], "Household": [], "Advisor": []}
    assert app_module._with_downstream(["Household"], depends) == ["Account", "Household", "Position"]
    assert app_module._with_downstream(["Account"], depends) == ["Account", "Position"]
    assert app_module._with_downstream(["Advisor"], depends) == ["Advisor"]


class _Form(dict):
    def getlist(self, key):
        v = self.get(key, [])
        return v if isinstance(v, list) else [v]


class _Conn:
    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        pass


def _start(monkeypatch, form, runs=True, waves=True):
    """Call the stage 09 start endpoint with Data Factory faked; returns (redirect location, parameters sent to Data Factory or None)."""
    import asyncio
    import sqlite3

    import ui.app as app_module
    from control_plane.services import adf_runs, deployment_check
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if runs:
        _request(conn, "SUCCEEDED")  # batch B1
    else:
        adf_runs.ensure_table(conn)
    man = {"version": "v1", "migrationSlug": "mig1", "rawFolder": "raw/x", "container": "c", "loadOrder": ["Household", "Account", "Position"],
           "dependsOn": {"Account": ["Household"], "Position": ["Account"], "Household": []}, **({"waves": {"Household": 0, "Account": 1, "Position": 2}} if waves else {})}
    sent = {}

    class Req:
        async def form(self):
            return _Form(form)
    monkeypatch.setitem(app_module.state_store.questionnaire_data, "migration_id", "MIG-T")
    monkeypatch.setattr(app_module, "get_current_user", lambda r: {"name": "Ann"})
    monkeypatch.setattr(app_module, "get_db_connection", lambda: _Conn(conn))
    monkeypatch.setattr(app_module, "_xodus_package", lambda c, m: ({}, {"manifest": man}))
    monkeypatch.setattr(app_module, "_adf_status", lambda c, m: None)
    monkeypatch.setattr(app_module, "_merged_pr", lambda c, m, v: True)
    monkeypatch.setattr(deployment_check, "latest", lambda c, m: {"version": "v1", "status": "PASS"})
    monkeypatch.setattr(app_module, "workflow_progress", lambda: {"validation_status": "PASS"})
    monkeypatch.setattr(app_module, "_run_in_progress", lambda c, m, p: None)
    monkeypatch.setattr(app_module, "_target_setup_audit", lambda *a, **k: None)
    monkeypatch.setattr(adf_runs, "start", lambda c, mig, pl, params, actor, batch_id=None, **k: sent.update(params=params, batch=batch_id) or {"adf_run_id": "adf-2"})
    resp = asyncio.run(app_module.start_data_plane_run(Req()))
    return resp.headers["location"], sent.get("params")


def test_starting_without_entities_is_a_whole_new_batch(monkeypatch):
    loc, params = _start(monkeypatch, {})
    assert params["Entities"] == "" and params["BatchId"].startswith("B") and params["BatchId"] != "B1" and "notice=" in loc


def test_rerunning_entities_reuses_the_latest_batch_and_can_include_downstream(monkeypatch):
    loc, params = _start(monkeypatch, {"entities": ["Account"]})
    assert params["BatchId"] == "B1" and params["Entities"] == "Account"  # only the selected entity; the pipeline checks its parents
    loc, params = _start(monkeypatch, {"entities": ["Household"], "downstream": "1"})
    assert params["BatchId"] == "B1" and params["Entities"] == "Household,Account,Position"  # in load order


def test_rerunning_entities_is_refused_without_a_batch_or_for_unknown_entities(monkeypatch):
    loc, params = _start(monkeypatch, {"entities": ["Account"]}, runs=False)
    assert params is None and "error=" in loc and "start+a+whole+batch+first" in loc
    loc, params = _start(monkeypatch, {"entities": ["Nope"]})
    assert params is None and "error=" in loc and "Nope" in loc


def test_a_package_built_before_waves_neither_gets_the_entities_parameter_nor_an_entity_rerun(monkeypatch):
    loc, params = _start(monkeypatch, {}, waves=False)
    assert "Entities" not in params and params["BatchId"].startswith("B")  # the published pipeline of an older package has no such parameter
    loc, params = _start(monkeypatch, {"entities": ["Account"]}, waves=False)
    assert params is None and "error=" in loc and "built+before+entity+reruns" in loc
