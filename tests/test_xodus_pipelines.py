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
                             "RepCode": {"canonical_field": "REP_CODE", "expression": "PREFIX_FORMAT (REP-8000000)"},
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
    assert "N'REP-' + s.[RepCode]" in sql and "LTRIM(RTRIM(s.[HouseholdName]))" in sql  # the agent's families, in SQL
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
    assert names[:2] == ["Log_Start", "Run_Source_To_Canonical"] and "Fail_Run_Canonical_To_Mirror" in names and names[-1] == "Log_Done"
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
