import json

import pytest

from control_plane.services import transformation_rules as tr
from control_plane.services import xodus_pipelines as xp


def _rule(kind, params):
    return {"rule_type": kind, "params": tr.validate_params(kind, params), "status": "APPROVED"}


SPEC = {"spec_version": "v1", "target_system": "Simplicity", "entities": {
    "Household": {"fields": [{"name": f"ORION_MCL_HOUSEHOLD.{c}", "table": "ORION_MCL_HOUSEHOLD", "column": c, "type": t, "required": r}
                             for c, t, r in (("HH_GROUPING_CODE", "string", False), ("HOUSEHOLD_NAME", "string", False), ("HOUSEHOLD_LAST_NAME", "string", True))]},
    "Account": {"fields": [{"name": f"ORION_MCL_ACCOUNT.{c}", "table": "ORION_MCL_ACCOUNT", "column": c, "type": t, "required": r}
                           for c, t, r in (("ACCOUNT_NUMBER", "string", True), ("CUSTODIAN", "string", True), ("OPEN_DATE", "date", False))]}}}
TARGET = [{"entity_name": "Household", "target_field": "ORION_MCL_HOUSEHOLD.HH_GROUPING_CODE", "canonical_field": "HH_ID", "transformation_expression": "DIRECT_COPY", "status": "APPROVED"},
          {"entity_name": "Household", "target_field": "ORION_MCL_HOUSEHOLD.HOUSEHOLD_NAME", "canonical_field": "HH_NAME", "transformation_expression": "DIRECT_COPY", "status": "APPROVED"},
          {"entity_name": "Account", "target_field": "ORION_MCL_ACCOUNT.ACCOUNT_NUMBER", "canonical_field": "ACCT_NUM", "transformation_expression": "DIRECT_COPY", "status": "APPROVED"},
          {"entity_name": "Account", "target_field": "ORION_MCL_ACCOUNT.CUSTODIAN", "canonical_field": "CUSTODIAN_CODE", "transformation_expression": "DIRECT_COPY", "status": "APPROVED"},
          {"entity_name": "Account", "target_field": "ORION_MCL_ACCOUNT.OPEN_DATE", "canonical_field": "OPEN_DATE", "transformation_expression": "DIRECT_COPY", "status": "PROPOSED"}]
RULES = {"RULE:CUSTODIAN_CODE_MAP@2": _rule("VALUE_MAP", {"map": {"SCHWAB": "SCHWAB", "TD AMERITRADE": "TDA"}, "strict": True})}
ENTITIES = {
    "Household": {"files": ["corecap_households.csv"], "key": "HH_ID", "derivations": [], "canonical_fields": ["HH_ID", "HH_NAME", "ADVISOR_ID"],
                  "mappings": {"Id": {"canonical_field": "HH_ID", "expression": "DIRECT_COPY"}, "HouseholdName": {"canonical_field": "HH_NAME", "expression": "TRIM"},
                               "AdvisorId": {"canonical_field": "ADVISOR_ID", "expression": "DIRECT_COPY"}}},
    "Account": {"files": ["corecap_accounts.csv"], "key": "ACCT_NUM", "canonical_fields": ["ACCT_NUM", "CUSTODIAN_NAME", "CUSTODIAN_CODE", "REP_CODE", "HH_ID"],
                "mappings": {"AccountNumber": {"canonical_field": "ACCT_NUM", "expression": "DIRECT_COPY"},
                             "CustodianName": {"canonical_field": "CUSTODIAN_NAME", "expression": "DIRECT_COPY"},
                             "RepCode": {"canonical_field": "REP_CODE", "expression": "TRIM"},
                             "HouseholdId": {"canonical_field": "HH_ID", "expression": "DIRECT_COPY"}},
                "derivations": [{"canonical_field": "CUSTODIAN_CODE", "input_field": "CUSTODIAN_NAME", "rule_expression": "RULE:CUSTODIAN_CODE_MAP@2"}]}}
RAW = [{"file_name": "corecap_households.csv", "columns": ["AdvisorId", "Id", "HouseholdName"]},
       {"file_name": "corecap_accounts.csv", "columns": ["AccountNumber", "CustodianName", "RepCode", "HouseholdId"]}]
ENV = {"container": "xodus-lake", "rawFolder": "raw/corecap/", "liveSchema": "simplicity"}


def build(**over):
    args = dict(migration_id="MIG-00A1A6577B", spec=SPEC, approved_target=TARGET, rules=RULES, entities=ENTITIES, raw_files=RAW,
                load_order=["Household", "Account"], env=ENV, generated_by="Ann")
    args.update(over)
    return xp.build(**args)


def test_pipelines_are_stable_and_only_the_sql_changes_with_the_source():
    a = build()
    changed = dict(ENTITIES, Household={**ENTITIES["Household"], "mappings": {**ENTITIES["Household"]["mappings"],
                                                                                "HouseholdName": {"canonical_field": "HH_NAME", "expression": "UPPERCASE"}}})
    b = build(entities=changed)
    stable = [f"pipeline/{p}.json" for p in xp.STABLE_PIPELINES] + ["dataset/DS_XODUS_RawFile.json", "dataset/DS_XODUS_Sql.json", "dataset/DS_XODUS_SqlSimplicity.json"]
    assert all(a["files"][p] == b["files"][p] for p in stable)  # a source change never touches the pipeline definitions
    assert a["version"] != b["version"] and a["files"]["sql/xodus_mig00a1a6577b.sql"] != b["files"]["sql/xodus_mig00a1a6577b.sql"]
    assert a["manifest"]["setupPipeline"] == "PL_XODUS_Setup_mig00a1a6577b" and "pipeline/PL_XODUS_Setup_mig00a1a6577b.json" in a["files"]


def test_sql_layers_procedures_and_registry():
    pkg = build()
    sql = pkg["files"]["sql/xodus_mig00a1a6577b.sql"]
    assert "CREATE TABLE [xodus_raw].[mig00a1a6577b_corecap_accounts]" in sql and "[xodus_source_file] nvarchar(1024)" in sql
    assert "CREATE OR ALTER PROCEDURE [xodus_canon].[usp_canon_mig00a1a6577b_Account]" in sql
    assert "LTRIM(RTRIM(s.[RepCode]))" in sql and "LTRIM(RTRIM(s.[HouseholdName]))" in sql  # the agent's generic families, in SQL
    assert "UPDATE c SET c.[CUSTODIAN_CODE] = " in sql and "N'TD AMERITRADE' THEN N'TDA'" in sql  # derivation after mapping
    assert "PARTITION BY s.[AccountNumber]" in sql  # one row per key, as the in-memory join does
    assert "CREATE OR ALTER PROCEDURE [xodus_mirror].[usp_mirror_mig00a1a6577b]" in sql and "OPEN_DATE" not in sql.split("usp_mirror_")[1].split("END")[0]
    assert "INSERT INTO [xodus_ops].[xodus_load_order] (migration_slug, seq, entity) VALUES (N'mig00a1a6577b', 1, N'Household'), (N'mig00a1a6577b', 2, N'Account');" in sql
    assert f"VALUES (N'mig00a1a6577b', N'xodus', N'{pkg['version']}')" in sql
    assert "SET XACT_ABORT ON" in sql and "ROLLBACK TRANSACTION" in sql
    live = pkg["files"]["sql/simplicity_mig00a1a6577b.sql"]
    assert "MERGE [simplicity].[ORION_MCL_HOUSEHOLD] AS tgt" in live and "ON tgt.[HH_GROUPING_CODE] = src.[HH_GROUPING_CODE]" in live
    assert "CREATE TABLE [simplicity]" not in live and "DROP TABLE [simplicity]" not in live  # live tables are never created or dropped
    assert pkg["manifest"]["liveTables"][0]["key_columns"] == ["ACCOUNT_NUMBER"]


def test_pipeline_shape_logging_and_gate():
    files = build()["files"]
    run = json.loads(files["pipeline/PL_XODUS_Run.json"])["properties"]
    names = [a["name"] for a in run["activities"]]
    assert names[:2] == ["Log_Start", "Check_Scope"] and "Run_Load_Raw" in names and "Run_Waves" in names and "Fail_Run_Waves" in names and names[-1] == "Log_Done"
    load = json.loads(files["pipeline/PL_XODUS_Load_Raw_File.json"])["properties"]["activities"]
    assert "additionalColumns" not in load[0]["typeProperties"]["source"]  # Data Factory refuses them on these sink datasets
    stamp = next(a for a in load if a["name"] == "Stamp_Trace")
    assert stamp["dependsOn"] == [{"activity": "Copy_Raw", "dependencyConditions": ["Succeeded"]}] and "usp_xodus_stamp_raw" in stamp["typeProperties"]["scripts"][0]["text"]
    assert [a["name"] for a in load if a["name"].startswith("Log_OK")] == ["Log_OK_Stamp_Trace"]
    assert "CREATE OR ALTER PROCEDURE [xodus_ops].[usp_xodus_stamp_raw]" in files["sql/xodus_mig00a1a6577b.sql"]
    promote = json.loads(files["pipeline/PL_XODUS_Promote.json"])["properties"]["activities"]
    order = [a["name"] for a in promote]
    assert order.index("Gate_Check") < order.index("Stop_If_Gate_Fails") < order.index("Copy_Each_Table") < order.index("Merge_Into_Live")
    for a in promote:  # control activities are never nested in each other (Data Factory does not allow it)
        if a["type"] == "IfCondition":
            assert all(x["type"] not in ("ForEach", "IfCondition") for x in a["typeProperties"]["ifTrueActivities"])
    logs = [a for p in files if p.startswith("pipeline/") for a in json.loads(files[p])["properties"]["activities"] if a["name"].startswith("Log_")]
    assert logs and all("@mig" in a["typeProperties"]["scripts"][0]["text"] and "'" not in a["typeProperties"]["scripts"][0]["text"] for a in logs)  # values are parameters


def test_refuses_what_it_cannot_translate():
    bad = dict(ENTITIES, Household={**ENTITIES["Household"], "mappings": {"Id": {"canonical_field": "HH_ID", "expression": "FREE TEXT RULE"}}})
    with pytest.raises(ValueError):
        build(entities=bad)
    with pytest.raises(ValueError):
        build(raw_files=[{"file_name": "corecap_households.csv", "columns": ["Id]; DROP TABLE x;--"]}, RAW[1]])
    with pytest.raises(ValueError):
        build(approved_target=[])


def test_an_entity_without_a_key_orders_its_dedupe_by_a_non_constant():
    """SQL Server rejects a constant in a window's ORDER BY: a keyless entity (account allocation lines) has no source key to order by."""
    spec = {"spec_version": "v1", "target_system": "Simplicity", "entities": {
        "AccountAllocation": {"fields": [{"name": f"account_model.{c}", "table": "account_model", "column": c, "type": "string", "required": False}
                                         for c in ("ACCOUNT_NUMBER", "CURRENT_MODEL")]}}}
    target = [{"entity_name": "AccountAllocation", "target_field": "account_model.ACCOUNT_NUMBER", "canonical_field": "ACCT_NUM", "transformation_expression": "DIRECT_COPY", "status": "APPROVED"},
              {"entity_name": "AccountAllocation", "target_field": "account_model.CURRENT_MODEL", "canonical_field": "MODEL_NAME", "transformation_expression": "DIRECT_COPY", "status": "APPROVED"}]
    entities = {"AccountAllocation": {"files": ["corecap_account_targets.csv"], "key": None, "derivations": [], "canonical_fields": ["ACCT_NUM", "MODEL_NAME"],
                                      "mappings": {"AccountNumber": {"canonical_field": "ACCT_NUM", "expression": "DIRECT_COPY"},
                                                   "ModelName": {"canonical_field": "MODEL_NAME", "expression": "DIRECT_COPY"}}}}
    raw = [{"file_name": "corecap_account_targets.csv", "columns": ["AccountNumber", "ModelName"]}]
    sql = build(spec=spec, approved_target=target, entities=entities, raw_files=raw, load_order=["AccountAllocation"])["files"]["sql/xodus_mig00a1a6577b.sql"]
    assert "ORDER BY CAST(NULL" not in sql and "ORDER BY (SELECT NULL)) AS [xodus_rn]" in sql


def _sql(pkg):
    return pkg["files"]["sql/xodus_mig00a1a6577b.sql"]


def test_row_lineage_columns_in_every_layer():
    pkg = build()
    sql = _sql(pkg)
    assert "[corecap_accounts]" not in sql  # raw table names carry the slug
    raw = sql.split("CREATE TABLE [xodus_raw].[mig00a1a6577b_corecap_accounts]")[1].split(");")[0]
    assert "[xodus_row_id] bigint IDENTITY(1,1) NOT NULL" in raw
    canon = sql.split("CREATE TABLE [xodus_canon].[mig00a1a6577b_Account]")[1].split(");")[0]
    assert "[xodus_source_file] nvarchar(1024) NULL" in canon and "[xodus_source_row] bigint NULL" in canon and "[xodus_row_hash] char(64) NULL" in canon
    proc = sql.split("CREATE OR ALTER PROCEDURE [xodus_canon].[usp_canon_mig00a1a6577b_Account]")[1].split("COMMIT TRANSACTION")[0]
    assert "s.[xodus_row_id] AS [xodus_src_row]" in proc and "[xodus_source_file], [xodus_source_row], [xodus_batch_id]" in proc
    assert "SET c.[xodus_row_hash] = CONVERT(char(64), HASHBYTES('SHA2_256', CONCAT_WS(" in proc
    assert proc.index("SET c.[CUSTODIAN_CODE]") < proc.index("SET c.[xodus_row_hash]")  # the hash covers the derived values
    mirror = sql.split("CREATE TABLE [xodus_mirror].[mig00a1a6577b_ORION_MCL_ACCOUNT]")[1].split(");")[0]
    assert "[xodus_source_row] bigint NULL" in mirror
    assert "s.[xodus_source_row] AS [xodus_source_row]" in sql
    landing = pkg["files"]["sql/simplicity_mig00a1a6577b.sql"]
    assert "[xodus_source_row] bigint NULL" in landing  # the copy to landing keeps the row lineage column


def test_column_map_records_every_mapping_derivation_and_mirror_column():
    pkg = build()
    sql = _sql(pkg)
    assert "CREATE TABLE [xodus_ref].[column_map]" in sql and "CREATE OR ALTER VIEW [xodus_ref].[v_column_lineage]" in sql
    assert "DELETE FROM [xodus_ref].[column_map] WHERE migration_slug = N'mig00a1a6577b';" in sql
    assert ("N'canonical', N'Account', N'mig00a1a6577b_Account', N'REP_CODE', N'corecap_accounts.csv', N'RepCode', N'MAPPING', N'TRIM', NULL") in sql
    assert ("N'canonical', N'Account', N'mig00a1a6577b_Account', N'CUSTODIAN_CODE', NULL, N'CUSTODIAN_NAME', N'DERIVATION', N'RULE:CUSTODIAN_CODE_MAP@2', NULL") in sql
    assert ("N'mirror', N'Account', N'mig00a1a6577b_ORION_MCL_ACCOUNT', N'ACCOUNT_NUMBER', NULL, N'ACCT_NUM', N'MAPPING', N'DIRECT_COPY', NULL") in sql
    assert "OPEN_DATE" not in sql.split("INSERT INTO [xodus_ref].[column_map]")[1].split(";")[0]  # a PROPOSED mapping is not lineage
    assert pkg["manifest"]["columnMapRows"] >= 9


def test_column_map_changes_with_the_mapping_so_the_version_does_too():
    changed = dict(ENTITIES, Account={**ENTITIES["Account"], "mappings": {**ENTITIES["Account"]["mappings"], "RepCode": {"canonical_field": "REP_CODE", "expression": "UPPERCASE"}}})
    assert build()["version"] != build(entities=changed)["version"]
    assert "N'REP_CODE', N'corecap_accounts.csv', N'RepCode', N'MAPPING', N'UPPERCASE', NULL" in _sql(build(entities=changed))


def test_entity_registry_gives_parents_wave_zero_and_children_one_more():
    flat = build()
    assert flat["manifest"]["waves"] == {"Household": 0, "Account": 0}  # no dependencies given: every entity is wave 0
    nested = build(depends={"Account": ["Household"], "Household": []})
    assert nested["manifest"]["waves"] == {"Household": 0, "Account": 1} and nested["manifest"]["dependsOn"]["Account"] == ["Household"]
    sql = _sql(nested)
    assert "CREATE TABLE [xodus_ops].[xodus_entity_registry]" in sql and "CREATE TABLE [xodus_ops].[xodus_entity_state]" in sql
    assert "(N'mig00a1a6577b', N'Account', 1, N'Household', N'mig00a1a6577b_corecap_accounts', N'mig00a1a6577b_Account', N'mig00a1a6577b_ORION_MCL_ACCOUNT')" in sql
    assert "(N'mig00a1a6577b', N'Household', 0, N'', " in sql
    assert nested["version"] != flat["version"]  # waves are deployed data, so they belong to the package version


def test_the_stable_pipelines_do_not_change_with_the_lineage_work():
    a, b = build(), build(depends={"Account": ["Household"]})
    stable = [f"pipeline/{p}.json" for p in xp.STABLE_PIPELINES]
    assert all(a["files"][p] == b["files"][p] for p in stable)


def test_a_scoped_run_rebuilds_only_the_selected_entities():
    files = build(depends={"Account": ["Household"]})["files"]
    run = json.loads(files["pipeline/PL_XODUS_Run.json"])["properties"]
    assert run["parameters"]["Entities"] == {"type": "string", "defaultValue": ""}  # empty = the whole batch
    acts = {a["name"]: a for a in run["activities"]}
    assert "usp_xodus_check_scope" in acts["Check_Scope"]["typeProperties"]["scripts"][0]["text"]
    assert acts["Run_Load_Raw"]["dependsOn"][0]["activity"] == "Log_OK_Check_Scope"  # nothing is loaded before the scope is checked
    assert acts["Run_Load_Raw"]["typeProperties"]["parameters"]["Entities"] == {"value": "@pipeline().parameters.Entities", "type": "Expression"}
    load = json.loads(files["pipeline/PL_XODUS_Load_Raw.json"])["properties"]
    assert load["parameters"]["Entities"]["defaultValue"] == ""
    assert next(a for a in load["activities"] if a["name"] == "Get_Raw_Files")["typeProperties"]["source"]["storedProcedureParameters"]["Entities"]["value"]["value"] == "@pipeline().parameters.Entities"
    c2m = json.loads(files["pipeline/PL_XODUS_Canonical_To_Mirror.json"])["properties"]
    assert c2m["parameters"]["Entity"]["defaultValue"] == ""
    assert "@Entity = @entity" in next(a for a in c2m["activities"] if a["name"] == "Build_Mirror")["typeProperties"]["scripts"][0]["text"]


def test_the_run_goes_wave_by_wave_and_a_failed_gate_stops_it():
    files = build(depends={"Account": ["Household"]})["files"]
    run = {a["name"]: a for a in json.loads(files["pipeline/PL_XODUS_Run.json"])["properties"]["activities"]}
    waves = run["Run_Waves"]
    assert waves["type"] == "ForEach" and waves["typeProperties"]["isSequential"] is True  # one wave after another
    assert run["Get_Waves"]["typeProperties"]["source"]["sqlReaderStoredProcedureName"] == "[xodus_ops].[usp_xodus_waves]"
    inner = waves["typeProperties"]["activities"][0]
    assert inner["typeProperties"]["pipeline"]["referenceName"] == "PL_XODUS_Wave" and inner["typeProperties"]["parameters"]["Wave"]["value"] == "@string(item().wave)"
    assert run["Log_Done"]["dependsOn"][0]["activity"] == "Log_OK_Run_Waves"
    wave = json.loads(files["pipeline/PL_XODUS_Wave.json"])["properties"]
    by = {a["name"]: a for a in wave["activities"]}
    order = [a["name"] for a in wave["activities"]]
    assert order.index("Get_Wave_Entities") < order.index("Build_Each_Entity") < order.index("Build_Each_Mirror") < order.index("Validate_Wave") < order.index("Gate_Wave")
    assert by["Build_Each_Entity"]["typeProperties"]["isSequential"] is False  # siblings build in parallel
    assert by["Get_Wave_Entities"]["typeProperties"]["source"]["storedProcedureParameters"]["Wave"]["type"] == "Int32"
    assert by["Validate_Wave"]["typeProperties"]["firstRowOnly"] is True
    gate = by["Gate_Wave"]
    assert "firstRow.ok" in gate["typeProperties"]["expression"]["value"] and gate["typeProperties"]["ifTrueActivities"][-1]["type"] == "Fail"
    assert by["Log_Wave_OK"]["dependsOn"][0]["activity"] == "Gate_Wave"  # the next wave only starts after the gate passed
    for a in wave["activities"]:  # control activities are never nested in each other
        if a["type"] == "IfCondition":
            assert all(x["type"] not in ("ForEach", "IfCondition") for x in a["typeProperties"]["ifTrueActivities"])
    assert json.loads(files["pipeline/PL_XODUS_Wave.json"])["properties"]["parameters"]["Wave"] == {"type": "string"}


def test_scoped_procedures_and_entity_state_in_the_sql():
    sql = _sql(build(depends={"Account": ["Household"]}))
    # one mirror procedure per entity; the whole mirror calls them in order inside one transaction
    assert "CREATE OR ALTER PROCEDURE [xodus_mirror].[usp_mirror_mig00a1a6577b_Account]" in sql
    assert "CREATE OR ALTER PROCEDURE [xodus_mirror].[usp_mirror_mig00a1a6577b_Household]" in sql
    whole = sql.split("CREATE OR ALTER PROCEDURE [xodus_mirror].[usp_mirror_mig00a1a6577b]")[1].split("\nEND")[0]
    assert whole.index("usp_mirror_mig00a1a6577b_Account") < whole.index("usp_mirror_mig00a1a6577b_Household") and "BEGIN TRANSACTION" in whole
    # the dispatcher takes an optional entity (empty = the whole mirror)
    assert "@Entity sysname = N''" in sql and "CASE WHEN @Entity = N'' THEN N'' ELSE N'_' + @Entity END" in sql
    # entity state: written at the end of each canonical and mirror procedure, with the package version, and dependants go stale
    canon = sql.split("CREATE OR ALTER PROCEDURE [xodus_canon].[usp_canon_mig00a1a6577b_Account]")[1].split("COMMIT TRANSACTION")[0]
    assert "@Entity = N'Account', @Stage = N'canonical'" in canon and canon.index("xodus_row_hash") < canon.index("usp_xodus_entity_state")
    assert "@Stage = N'mirror'" in sql.split("usp_mirror_mig00a1a6577b_Account]")[1]
    state = sql.split("CREATE OR ALTER PROCEDURE [xodus_ops].[usp_xodus_entity_state]")[1].split("\nEND")[0]
    assert "SET status = N'STALE'" in state and "package_version = @version" in state
    # the scope check refuses what would leave the batch inconsistent
    check = sql.split("CREATE OR ALTER PROCEDURE [xodus_ops].[usp_xodus_check_scope]")[1].split("\nEND")[0]
    for text in ("is not an entity of this migration", "has not been built yet", "built by another package version", "built in this batch first (it is stale, failed validation or not built)"):
        assert text in check
    assert "IF @Entities = N'' RETURN;" in check  # a whole-batch run has nothing to check


def test_registry_source_files_include_lookup_files_and_only_earlier_parents():
    lookup_rule = _rule("LOOKUP", {"parent_file": "corecap_households.csv", "parent_key": "Id", "return_column": "AdvisorId"})
    rules = {**RULES, "RULE:ADV_LOOKUP@1": lookup_rule}
    entities = dict(ENTITIES, Account={**ENTITIES["Account"], "derivations": ENTITIES["Account"]["derivations"] + [
        {"canonical_field": "REP_CODE", "input_field": "HH_ID", "rule_expression": "RULE:ADV_LOOKUP@1"}]})
    sql = _sql(build(rules=rules, entities=entities, depends={"Account": ["Household"], "Household": ["Account"]}))  # a cycle: only the earlier one counts
    row = sql.split("INSERT INTO [xodus_ops].[xodus_entity_registry]")[1].split(";")[0]
    assert "N'Account', 1, N'Household', N'mig00a1a6577b_corecap_accounts,mig00a1a6577b_corecap_households'" in row  # the lookup file is loaded for Account too
    assert "N'Household', 0, N''" in row


def _validate_sql(pkg):
    sql = _sql(pkg)
    return sql.split("CREATE OR ALTER PROCEDURE [xodus_ops].[usp_validate_mig00a1a6577b]")[1].split("COMMIT TRANSACTION")[0]


def test_blocking_checks_run_as_sql_per_entity_and_write_findings_and_failing_keys():
    pkg = build(depends={"Account": ["Household"]})
    sql = _sql(pkg)
    for table in ("xodus_validation_result", "xodus_validation_row_key", "xodus_wave_waiver"):
        assert f"CREATE TABLE [xodus_ops].[{table}]" in sql
    proc = _validate_sql(pkg)
    assert "r.[wave] = @Wave" in proc and "@Entities = N''" in proc  # only the wave's entities (and the selected ones)
    assert "DELETE FROM [xodus_ops].[xodus_validation_result]" in proc  # a rerun replaces the entity's findings
    assert "N'ENTITY_BUILT'" in proc and "N'STALE' THEN N'FAIL'" in proc  # a stale or unbuilt entity is a finding
    acct = proc.split("IF EXISTS (SELECT 1 FROM @scope WHERE [entity] = N'Account')")[1].split("IF EXISTS (SELECT 1 FROM @scope WHERE [entity] = N'Household')")[0]
    for check in ("STAGING_ROW_COUNT", "KEYS_LOADED", "TARGET_KEY_UNIQUE", "REQUIRED_NOT_BLANK"):
        assert f"N'{check}'" in acct
    assert "SET @n = (SELECT COUNT(*) FROM [xodus_canon].[mig00a1a6577b_Account]);" in acct
    assert "SET @x = (SELECT COUNT(*) FROM [xodus_raw].[mig00a1a6577b_corecap_accounts]);" in acct  # compared with what the raw file delivered
    assert "INSERT INTO @k ([k]) SELECT TOP (50)" in acct and "INSERT INTO [xodus_ops].[xodus_validation_row_key]" in acct  # keys of failing rows, never values
    assert "N'WAIVED'" in proc and "xodus_wave_waiver" in proc  # an approved exception turns a failure into WAIVED
    assert "THEN N'FAILED' ELSE N'VALIDATED'" in proc  # the entity's state follows its findings
    assert pkg["manifest"]["waveChecks"]["Account"] == ["KEYS_LOADED", "REQUIRED_NOT_BLANK", "STAGING_ROW_COUNT", "TARGET_KEY_UNIQUE"]


def test_the_gate_reads_failures_and_the_dispatcher_refuses_an_undeployed_migration():
    sql = _sql(build())
    gate = sql.split("CREATE OR ALTER PROCEDURE [xodus_ops].[usp_xodus_validate_wave]")[1].split(chr(10) + "END")[0]
    assert "THROW 50002" in gate and "usp_validate_" in gate and "r.status = N'FAIL'" in gate  # WAIVED and WARN do not stop a wave
    assert "AS ok" in gate and "STRING_AGG" in gate
    for proc in ("usp_xodus_waves", "usp_xodus_wave_entities", "usp_xodus_wave_mirror_entities"):
        assert f"CREATE OR ALTER PROCEDURE [xodus_ops].[{proc}]" in sql
    assert "r.mirror_tables <> N''" in sql.split("usp_xodus_wave_mirror_entities")[1]  # an entity with no mirror table is not built twice


def test_an_orphan_check_runs_in_the_childs_wave_and_only_when_the_parent_is_not_later():
    plan = [{"check": "NO_ORPHANS", "entity": "Account", "table": "t", "field": "HH → Household.HH", "sql": "SELECT 1", "kind": "zero", "expected": None, "keys_sql": None},
            {"check": "NO_ORPHANS", "entity": "Household", "table": "t", "field": "ADV → Advisor.ADV", "sql": "SELECT 1", "kind": "zero", "expected": None, "keys_sql": None}]
    proc = xp._validate_procedure("m1", plan, {"Household": 0, "Account": 1, "Advisor": 1})
    assert "N'Account'" in proc  # Household (wave 0) is an earlier parent
    assert "N'Household'" not in proc.split("N'ENTITY_BUILT'")[1].split("N'WAIVED'")[0]  # its parent Advisor is in a later wave: left to stage 10


def test_code_lists_from_the_model_become_blocking_code_set_checks():
    codes = {"coded": {"Account": {"CUSTODIAN_CODE": "custodian"}}, "source_map": [], "target_map": [], "target_system": "Simplicity",
             "code_lists": {"custodian": ["SCHWAB", "TDA"]}}
    sql = _sql(build(codes=codes, depends={"Account": ["Household"]}))
    assert "N'VALUES_IN_CODE_SET'" in sql and "NOT IN (N'SCHWAB', N'TDA')" in sql
