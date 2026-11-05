import sqlite3

import pytest

from control_plane.services import canonical_derivations as cd
from control_plane.services import transformation_rules as tr
from control_plane.services.migration_validation import run_checks


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE canonical_mappings (migration_id TEXT, entity_name TEXT, source_field TEXT, canonical_field TEXT, is_confirmed INTEGER, transformation_expression TEXT)")
    tr.ensure_table(c)
    return c


def _approved(conn, rule_id, rule_type, params):
    r = tr.create_rule_version(conn, rule_id, rule_type, "test", params, "analyst")
    tr.decide_rule(conn, rule_id, r["version"], "APPROVED", "approver", "ok")
    return tr.make_expression(rule_id, r["version"])


def test_requires_approved_rule_reason_and_no_source_mapping(conn):
    draft = tr.create_rule_version(conn, "DRAFT_ONLY", "CONCAT", "t", {"sources": ["A", "B"], "separator": " "}, "analyst")
    with pytest.raises(ValueError):
        cd.set_derivation(conn, "M1", "Client", "LEGAL_NAME", "FIRST_NAME", tr.make_expression("DRAFT_ONLY", draft["version"]), "me", "why")
    expr = _approved(conn, "NAME_JOIN", "CONCAT", {"sources": ["FIRST_NAME", "LAST_NAME"], "separator": " "})
    with pytest.raises(ValueError):
        cd.set_derivation(conn, "M1", "Client", "LEGAL_NAME", "FIRST_NAME", expr, "me", " ")
    conn.execute("INSERT INTO canonical_mappings VALUES ('M1','Client','Name','LEGAL_NAME',1,'')")
    with pytest.raises(ValueError):
        cd.set_derivation(conn, "M1", "Client", "LEGAL_NAME", "FIRST_NAME", expr, "me", "why")


def test_concat_and_strict_value_map_are_applied_and_unmapped_stay_blank(conn):
    join = _approved(conn, "NAME_JOIN", "CONCAT", {"sources": ["FIRST_NAME", "MIDDLE_NAME", "LAST_NAME"], "separator": " "})
    cmap = _approved(conn, "CUSTODIAN_MAP", "VALUE_MAP", {"map": {"SCHWAB": "SCHWAB"}, "case_insensitive": True, "strict": True})
    cd.set_derivation(conn, "M1", "Client", "LEGAL_NAME", "FIRST_NAME", join, "me", "name parts")
    cd.set_derivation(conn, "M1", "Account", "CUSTODIAN_CODE", "CUSTODIAN_NAME", cmap, "me", "crosswalk")
    clients = cd.apply_to_rows([{"FIRST_NAME": "Ann", "MIDDLE_NAME": "", "LAST_NAME": "Lee"}], cd.list_derivations(conn, "M1", "Client"))
    assert clients[0]["LEGAL_NAME"] == "Ann Lee"
    accts = cd.apply_to_rows([{"CUSTODIAN_NAME": "Schwab"}, {"CUSTODIAN_NAME": "TD Ameritrade"}], cd.list_derivations(conn, "M1", "Account"))
    assert accts[0]["CUSTODIAN_CODE"] == "SCHWAB" and accts[1]["CUSTODIAN_CODE"] is None
    assert cd.list_derivations(conn, "OTHER") == []


def test_retired_rule_drops_derivation(conn):
    expr = _approved(conn, "NAME_JOIN", "CONCAT", {"sources": ["FIRST_NAME", "LAST_NAME"], "separator": " "})
    cd.set_derivation(conn, "M1", "Client", "LEGAL_NAME", "FIRST_NAME", expr, "me", "r")
    conn.execute("UPDATE transformation_rules SET status = 'RETIRED'")
    assert cd.list_derivations(conn, "M1") == []


def test_validation_treats_derived_field_as_mapped_and_reports_blank_values(conn):
    cmap = _approved(conn, "CUSTODIAN_MAP", "VALUE_MAP", {"map": {"SCHWAB": "SCHWAB"}, "case_insensitive": True, "strict": True})
    cd.set_derivation(conn, "M1", "Account", "CUSTODIAN_CODE", "CUSTODIAN_NAME", cmap, "me", "r")
    derivs = {"Account": cd.list_derivations(conn, "M1", "Account")}
    contract = {"Account": {"required": ["ACCT_NUM", "CUSTODIAN_CODE"], "keys": ["ACCT_NUM"]}, "Household": {"required": [], "keys": []}, "Client": {"required": [], "keys": []}}
    mappings = {"Account": {"AcctNo": "ACCT_NUM", "Cust": "CUSTODIAN_NAME"}}
    files = {"Account": [{"filename": "a.csv", "records": [{"AcctNo": "1", "Cust": "Schwab"}, {"AcctNo": "2", "Cust": "Unknown Co"}]}]}
    without = run_checks(contract, mappings, files)
    assert [c for c in without["checks"] if c["check"] == "REQUIRED_FIELDS_MAPPED" and c["entity"] == "Account"][0]["status"] == "FAIL"
    result = run_checks(contract, mappings, files, None, derivs)
    c = {x["check"]: x for x in result["checks"] if x["entity"] == "Account"}
    assert c["REQUIRED_FIELDS_MAPPED"]["status"] == "PASS"
    assert c["REQUIRED_VALUES_PRESENT"]["status"] == "FAIL" and "CUSTODIAN_CODE (1 row(s))" in c["REQUIRED_VALUES_PRESENT"]["detail"]


def test_package_records_derivations_and_their_rules(tmp_path):
    from control_plane.services import package_contract as pc
    spec = {"entities": {"Account": {"fields": [{"name": "T.A", "table": "T", "column": "A"}]}}}
    derivation = {"entity_name": "Account", "canonical_field": "CUSTODIAN_CODE", "input_field": "CUSTODIAN_NAME",
                  "rule_expression": "RULE:CUSTODIAN_CODE_MAP@1", "decided_by": "Ann"}
    rule = {"status": "APPROVED", "rule_type": "VALUE_MAP", "params": {"map": {"SCHWAB": "SCHWAB"}, "case_insensitive": True, "strict": True},
            "description": "d", "reviewed_by": "Ann"}
    base = dict(migration_id="M", source_system="CoreCap", target_system="Simplicity", firm={"firm_id": "F", "config_version": "1", "canonical_contract_ref": "c"},
                target={"name": "S", "model_version": "v", "confirmed_by": "x", "confirmed_utc": "u"}, source_files=[], spec=spec, scenarios=[],
                source_mappings=[{"entity": "Account", "source_field": "ACCT", "canonical_field": "ACCT_NUM", "transformation": "DIRECT_COPY"}],
                environment={"simulated": True, "banner": "b", "factoryName": "f"})
    m = {"entity_name": "Account", "target_field": "T.A", "canonical_field": "CUSTODIAN_CODE", "status": "APPROVED", "reviewer": "Ann", "transformation_expression": "DIRECT_COPY"}
    body = pc.build_body(approved_target=[m], rules={"RULE:CUSTODIAN_CODE_MAP@1": rule}, derivations=[derivation], **base)
    assert body["canonical_derivations"][0]["rule"] == "RULE:CUSTODIAN_CODE_MAP@1"
    assert [r["expression"] for r in body["transformation_rules"]] == ["RULE:CUSTODIAN_CODE_MAP@1"]
    with pytest.raises(ValueError, match="not an approved rule"):
        pc.build_body(approved_target=[m], rules={}, derivations=[derivation], **base)
    conn = sqlite3.connect(tmp_path / "p.db")
    conn.row_factory = sqlite3.Row
    assert pc.generate(conn, "M", "Ann", body)["new"]
    assert "canonical_derivations" not in pc.build_body(approved_target=[m], rules={}, **base)
