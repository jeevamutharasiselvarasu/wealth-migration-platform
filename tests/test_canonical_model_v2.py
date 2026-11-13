"""Canonical model v2: definition and releases, code-set mapping, the target code values, the generated SQL and the conformance check."""
import sqlite3

import pytest
import yaml

from control_plane.services import canonical_model as cm
from control_plane.services import code_mappings as cmap
from control_plane.services import target_validation as tv
from tests.test_xodus_pipelines import build
from ui.role_policy import allowed_roles_for, admin_may_view


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def model():
    return cm.load_definition()


def test_definition_validates_with_the_agreed_entities_and_first_code_sets(model):
    assert set(model["entities"]) == {"Household", "Client", "Account", "AccountRole", "Advisor", "Model", "ModelWeight", "AccountModel", "AccountGainLoss"}  # no Party layer; the representative is the Advisor
    assert {"registration_type", "tax_status", "custodian", "account_status", "household_role"} <= set(model["code_sets"])
    assert cm.coded_fields(model)["Account"] == {"CUSTODIAN_CODE": "custodian", "REG_TYPE": "registration_type", "TAX_STATUS": "tax_status",
                                                 "ACCT_STATUS": "account_status"}
    assert cm.coded_fields(model)["Client"] == {"HH_RELATIONSHIP": "household_role"}


def test_validation_names_the_problem(model):
    bad = yaml.safe_load(yaml.safe_dump(model))
    bad["entities"]["Account"]["fields"][3]["code_set"] = "nope"
    with pytest.raises(ValueError, match="unknown code set nope"):
        cm.validate(bad)
    bad = yaml.safe_load(yaml.safe_dump(model))
    bad["entities"]["Advisor"]["fields"] = [f for f in bad["entities"]["Advisor"]["fields"] if not f.get("key")]
    with pytest.raises(ValueError, match="no key field"):
        cm.validate(bad)


def test_release_is_immutable_and_current_reads_the_release(conn, tmp_path, model):
    assert cm.current(conn)[1] is None  # nothing released: the definition is used
    rec = cm.release(conn, "Ada")
    assert rec["version"] == model["version"] and cm.current(conn)[1]["released_by"] == "Ada"
    with pytest.raises(ValueError, match="already released"):
        cm.release(conn, "Ada")
    changed = dict(model, description="changed")
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="new version"):
        cm.release(conn, "Ada", p)


@pytest.mark.parametrize("value,code", [("Rollover IRA", "IRA_ROLLOVER"), ("  ira_rollover ", "IRA_ROLLOVER"), ("Joint WROS", "JOINT_WROS"),
                                        ("Traditional IRA", "IRA_TRADITIONAL"), ("Schwab", "SCHWAB"), ("NonTaxable", "TAX_EXEMPT"), ("Child", "DEPENDENT")])
def test_suggestions_come_from_codes_synonyms_and_descriptions(model, value, code):
    cs = next(s for e, fs in cm.coded_fields(model).items() for f, s in fs.items()
              if s in ("registration_type", "custodian", "tax_status", "household_role") and code in cm.codes(model, s))
    assert cm.suggest(model, cs, value)[0] == code


def test_unknown_values_get_no_guess(model):
    assert cm.suggest(model, "registration_type", "Something Odd") == (None, "No known spelling matches; choose the code.")


ROWS = {"Account": [{"ACCT_NUM": "1", "REG_TYPE": "Rollover IRA", "CUSTODIAN_CODE": "Schwab"},
                    {"ACCT_NUM": "2", "REG_TYPE": "rollover ira", "CUSTODIAN_CODE": "Odd Bank"},
                    {"ACCT_NUM": "3", "REG_TYPE": "", "CUSTODIAN_CODE": None}],
        "Client": [{"CLIENT_ID": "C1", "HH_RELATIONSHIP": "Head"}]}


def test_profile_keeps_distinct_values_only_and_decisions_survive_a_new_profile(conn, model):
    res = cmap.propose(conn, "MIG-1", model, ROWS)
    assert res == {"values": 4, "new": 4, "suggested": 3}
    rows = {(r["code_set"], r["source_value"]): r for r in cmap.list_mappings(conn, "MIG-1")}
    assert rows[("registration_type", "ROLLOVER IRA")]["row_count"] == 2 and rows[("registration_type", "ROLLOVER IRA")]["canonical_code"] == "IRA_ROLLOVER"
    assert rows[("custodian", "ODD BANK")]["canonical_code"] is None
    assert cmap.approve_suggested(conn, "MIG-1", model, "Ann") == 3
    cmap.decide(conn, "MIG-1", model, "custodian", "ODD BANK", "APPROVE", "pershing", "Ann")
    assert cmap.propose(conn, "MIG-1", model, ROWS)["new"] == 0
    assert cmap.counts(conn, "MIG-1")["APPROVED"] == 4
    assert {"code_set": "custodian", "source_value": "ODD BANK", "canonical_code": "PERSHING"} in cmap.approved_source_map(conn, "MIG-1")


def test_a_code_must_belong_to_its_set(conn, model):
    cmap.propose(conn, "MIG-1", model, ROWS)
    with pytest.raises(ValueError, match="Choose a code of custodian"):
        cmap.decide(conn, "MIG-1", model, "custodian", "ODD BANK", "APPROVE", "", "Ann")
    with pytest.raises(ValueError, match="Choose a code"):
        cmap.decide(conn, "MIG-1", model, "custodian", "ODD BANK", "APPROVE", "IRA_ROTH", "Ann")
    assert cmap.decide(conn, "MIG-1", model, "custodian", "ODD BANK", "REJECT", "", "Ann")["status"] == "REJECTED"
    assert cmap.decide(conn, "MIG-1", model, "custodian", "ODD BANK", "REOPEN", "", "Ann")["status"] == "PROPOSED"


def test_target_code_values_per_target_system(conn, model):
    assert cmap.save_crosswalk(conn, "Simplicity", model, "registration_type", {"IRA_ROLLOVER": "Rollover IRA", "IRA_ROTH": ""}, "Admin") == 1
    assert cmap.target_map(conn, "Simplicity") == [{"code_set": "registration_type", "canonical_code": "IRA_ROLLOVER", "target_value": "Rollover IRA"}]
    assert cmap.target_map(conn, "Other") == []
    cmap.save_crosswalk(conn, "Simplicity", model, "registration_type", {"IRA_ROLLOVER": ""}, "Admin")  # blank removes: the code passes through
    assert cmap.target_map(conn, "Simplicity") == []
    with pytest.raises(ValueError, match="not a code of"):
        cmap.save_crosswalk(conn, "Simplicity", model, "registration_type", {"SCHWAB": "x"}, "Admin")


CODES = {"coded": {"Account": {"CUSTODIAN_CODE": "custodian", "REG_TYPE": "registration_type"}},
         "source_map": [{"code_set": "custodian", "source_value": "CHARLES SCHWAB", "canonical_code": "SCHWAB"}],
         "target_map": [{"code_set": "custodian", "canonical_code": "SCHWAB", "target_value": "Schwab"}], "target_system": "Simplicity", "model_version": "2.0.0"}


def test_generated_sql_reads_the_code_maps_as_data():
    pkg = build(codes=CODES)
    sql = pkg["files"]["sql/xodus_mig00a1a6577b.sql"]
    assert "CREATE TABLE [xodus_ref].[code_map]" in sql and "CREATE TABLE [xodus_ref].[target_code_map]" in sql
    # the derived CUSTODIAN_CODE is translated after it is built; REG_TYPE is not filled in this migration, so it is left alone
    assert ("UPDATE c SET c.[CUSTODIAN_CODE] = m.[canonical_code] FROM [xodus_canon].[mig00a1a6577b_Account] AS c JOIN [xodus_ref].[code_map] AS m "
            "ON m.[migration_slug] = N'mig00a1a6577b' AND m.[code_set] = N'custodian' AND m.[source_value] = UPPER(LTRIM(RTRIM(c.[CUSTODIAN_CODE])));") in sql
    assert "c.[REG_TYPE] = m." not in sql
    assert "t.[target_system] = N'Simplicity' AND t.[code_set] = N'custodian' AND t.[canonical_code] = s.[CUSTODIAN_CODE]), s.[CUSTODIAN_CODE]) AS [CUSTODIAN]" in sql
    assert "INSERT INTO [xodus_ref].[code_map] (migration_slug, code_set, source_value, canonical_code) VALUES (N'mig00a1a6577b', N'custodian', N'CHARLES SCHWAB', N'SCHWAB');" in sql
    assert "DELETE FROM [xodus_ref].[target_code_map] WHERE target_system = N'Simplicity';" in sql
    assert pkg["manifest"]["codeSets"]["coded"] == {"Account": {"CUSTODIAN_CODE": "custodian"}}
    # a new approved value changes the data rows and the version, never the pipelines
    more = dict(CODES, source_map=CODES["source_map"] + [{"code_set": "custodian", "source_value": "SCHW", "canonical_code": "SCHWAB"}])
    other = build(codes=more)
    assert other["version"] != pkg["version"] and other["files"]["pipeline/PL_XODUS_Run.json"] == pkg["files"]["pipeline/PL_XODUS_Run.json"]


def test_without_code_maps_the_package_is_unchanged():
    assert "xodus_ref].[code_map] WHERE" not in build()["files"]["sql/xodus_mig00a1a6577b.sql"] and build()["manifest"]["codeSets"] is None


def test_conformance_check_names_rows_by_their_source_key():
    plan = tv.build_plan(schema="xodus_mirror", staging_schema="xodus_canon", entities={"Account": {
        "staging_table": "s_Account", "expected_rows": None, "key": "ACCT_NUM", "tables": {}, "code_sets": {"REG_TYPE": ["IRA_ROTH", "INDIVIDUAL"]}}})
    p = next(x for x in plan if x["check"] == "VALUES_IN_CODE_SET")
    assert p["field"] == "REG_TYPE" and "NOT IN (N'INDIVIDUAL', N'IRA_ROTH')" in p["sql"] and "CAST([ACCT_NUM] AS nvarchar(400))" in p["keys_sql"]
    checks = tv.evaluate(plan, lambda sql: 3 if "NOT IN" in sql else 0, None, fetch_keys=lambda sql: ["A1", "A2", "A3"])
    c = next(x for x in checks if x["check"] == "VALUES_IN_CODE_SET")
    assert c["status"] == "FAIL" and c["row_keys"] == ["A1", "A2", "A3"] and "Code sets" in c["detail"]


def test_roles():
    assert allowed_roles_for("POST", "/api/canonical-model/release") == {"Platform Admin", "Domain SME"}
    assert allowed_roles_for("POST", "/api/code-sets/profile") == {"Migration Analyst", "IMO Lead"}
    assert allowed_roles_for("POST", "/api/code-sets/decide") == {"Migration Analyst", "IMO Lead"}
    assert allowed_roles_for("POST", "/api/target-system/code-values") == {"Platform Admin"}
    assert admin_may_view("/canonical-model") and admin_may_view("/target-setup/code-values") and not admin_may_view("/code-sets")
