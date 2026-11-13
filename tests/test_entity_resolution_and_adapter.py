"""Canonical phase 2 (entity resolution, Advisor, AccountRole) and phase 3 (target adapter)."""
import sqlite3

import pytest

from control_plane.services import entity_resolution as er
from control_plane.services import target_adapter as ta
from control_plane.services import target_mapping as tm
from control_plane.services import target_validation as tv
from control_plane.services.firm_config import P0_ENTITIES, load_firm_config
from tests.test_xodus_pipelines import ENTITIES, RAW, build
from ui.role_policy import admin_may_view, allowed_roles_for


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


CLIENTS = [
    {"CLIENT_ID": "C1", "TAX_ID": "123-45-6789", "FIRST_NAME": "A", "LAST_NAME": "B", "DATE_OF_BIRTH": "1970-01-01", "EMAIL": "", "HH_ID": "H1", "HH_ORDER": "1"},
    {"CLIENT_ID": "C2", "TAX_ID": "123456789", "FIRST_NAME": "A", "LAST_NAME": "B", "DATE_OF_BIRTH": "", "EMAIL": "x", "HH_ID": "H1", "HH_ORDER": "2"},
    {"CLIENT_ID": "C3", "TAX_ID": "", "FIRST_NAME": "a", "LAST_NAME": "b", "DATE_OF_BIRTH": "1970-01-01", "EMAIL": "", "HH_ID": "H2", "HH_ORDER": "1"},
    {"CLIENT_ID": "C4", "TAX_ID": "[REDACTED]", "FIRST_NAME": "[REDACTED]", "LAST_NAME": "[REDACTED]", "DATE_OF_BIRTH": "[REDACTED]", "HH_ID": "H3", "HH_ORDER": "1"},
    {"CLIENT_ID": "C5", "TAX_ID": "[REDACTED]", "FIRST_NAME": "[REDACTED]", "LAST_NAME": "[REDACTED]", "DATE_OF_BIRTH": "[REDACTED]", "HH_ID": "H4", "HH_ORDER": "1"},
]


def test_the_five_entities_are_in_the_canonical_config():
    cfg = load_firm_config()
    assert P0_ENTITIES[:5] == ("Account", "Client", "Household", "Advisor", "AccountRole") and set(cfg.entities) == set(P0_ENTITIES)
    assert set(P0_ENTITIES[5:]) == {"Model", "ModelWeight", "AccountAllocation", "AccountGainLoss"}  # SPARC entities, added with the CoreCap to Simplicity spec
    assert [f.name for f in cfg.entities["Advisor"].fields if f.is_key] == ["ADVISOR_ID"]
    assert not any(f.is_key for f in cfg.entities["AccountRole"].fields)  # a client may hold several roles on one account: no single key


def test_resolution_is_transitive_most_complete_survives_and_placeholders_never_match():
    rows, xref, stats = er.resolve(CLIENTS, "CLIENT_ID", er.suggested_rules()["Client"])
    # C1~C2 by tax id, C1~C3 by name + birth date; C4 and C5 share only masked values, so they stay apart
    assert stats == {"rows": 5, "records": 3, "merged": 2, "clusters": 1, "examples": [["C1", "C2", "C3"]]}
    assert xref == {"C1": "C1", "C2": "C1", "C3": "C1", "C4": "C4", "C5": "C5"}
    survivor = next(r for r in rows if r["CLIENT_ID"] == "C1")
    assert survivor["EMAIL"] == "x"  # a blank field of the survivor is filled from the others


def test_references_point_at_the_survivor_and_account_roles_are_derived_from_household_heads():
    _, xref, _ = er.resolve(CLIENTS, "CLIENT_ID", er.suggested_rules()["Client"])
    roles = [{"ACCT_NUM": "A1", "CLIENT_ID": "C2"}]
    assert er.repoint(roles, "CLIENT_ID", xref) == 1 and roles[0]["CLIENT_ID"] == "C1"
    derived = er.derive_rows("AccountRole", {"Account": [{"ACCT_NUM": "A1", "HH_ID": "H1"}, {"ACCT_NUM": "A2", "HH_ID": "H9"}], "Client": CLIENTS})
    assert derived == [{"ACCT_NUM": "A1", "CLIENT_ID": "C1", "ROLE": "OWNER", "ROLE_ORDER": "1"}]  # no head known for H9: no role invented


def test_rules_are_proposed_only_when_their_fields_are_filled_and_decided_by_a_person(conn):
    assert er.suggest(conn, "M", "Client", ["CLIENT_ID", "TAX_ID", "FIRST_NAME", "LAST_NAME"]) == 1  # no birth date mapped: one rule
    assert er.suggest(conn, "M", "Client", ["CLIENT_ID", "TAX_ID"]) == 0
    assert er.pending(conn, "M") == 1 and er.approved_rules(conn, "M") == {}
    er.decide(conn, "M", "Client", "MATCH_TAX_ID", "APPROVE", "Ann")
    assert er.approved_rules(conn, "M") == {"Client": [["TAX_ID"]]}
    with pytest.raises(ValueError):
        er.decide(conn, "M", "Client", "MATCH_NOPE", "APPROVE", "Ann")
    assert er.decide_derivation(conn, "M", "APPROVE", "Ann") == "APPROVED" and er.derivation(conn, "M")["method"] == er.method("AccountRole")


def test_generated_sql_resolves_clients_repoints_roles_and_derives_account_roles():
    clients = {"files": ["corecap_household_contacts.csv"], "key": "CLIENT_ID", "derivations": [], "resolution": [["TAX_ID"]],
               "canonical_fields": ["CLIENT_ID", "TAX_ID", "HH_ID", "HH_ORDER"],
               "mappings": {"ContactId": {"canonical_field": "CLIENT_ID", "expression": "DIRECT_COPY"}, "TaxId": {"canonical_field": "TAX_ID", "expression": "DIRECT_COPY"},
                            "HouseholdId": {"canonical_field": "HH_ID", "expression": "DIRECT_COPY"}, "HouseholdOrder": {"canonical_field": "HH_ORDER", "expression": "DIRECT_COPY"}}}
    roles = {"files": [], "mappings": {}, "derivations": [], "key": None, "derived": er.method("AccountRole"), "canonical_fields": ["ACCT_NUM", "CLIENT_ID", "ROLE", "ROLE_ORDER"]}
    raw = RAW + [{"file_name": "corecap_household_contacts.csv", "columns": ["ContactId", "TaxId", "HouseholdId", "HouseholdOrder"]}]
    pkg = build(entities={**ENTITIES, "Client": clients, "AccountRole": roles}, raw_files=raw, load_order=["Household", "Account", "Client", "AccountRole"])
    sql = pkg["files"]["sql/xodus_mig00a1a6577b.sql"]
    assert "CREATE TABLE [xodus_canon].[mig00a1a6577b_Client_xref]" in sql
    assert "INSERT INTO [xodus_canon].[mig00a1a6577b_Client_xref] ([source_key], [survivor_key], [xodus_batch_id], [xodus_run_id])" in sql
    assert "DELETE c FROM [xodus_canon].[mig00a1a6577b_Client] AS c JOIN #er AS e ON e.[k] = c.[CLIENT_ID] WHERE e.[g] <> e.[k];" in sql
    assert "IN (N'', N'REDACTED', N'NA', N'NULL', N'NONE', N'UNKNOWN') THEN NULL" in sql  # masked values never match
    assert ("SELECT a.[ACCT_NUM], h.[CLIENT_ID], N'OWNER', N'1', @BatchId, @RunId FROM [xodus_canon].[mig00a1a6577b_Account] AS a JOIN "
            "(SELECT [HH_ID], MIN([CLIENT_ID]) AS [CLIENT_ID] FROM [xodus_canon].[mig00a1a6577b_Client]") in sql
    assert ("UPDATE c SET c.[CLIENT_ID] = x.[survivor_key] FROM [xodus_canon].[mig00a1a6577b_AccountRole] AS c JOIN [xodus_canon].[mig00a1a6577b_Client_xref] AS x") in sql
    assert pkg["manifest"]["canonical"]["Client"]["xref"] == "mig00a1a6577b_Client_xref"
    assert pkg["manifest"]["canonical"]["AccountRole"]["derived"] == er.method("AccountRole")
    with pytest.raises(ValueError, match="loaded before AccountRole"):
        build(entities={**ENTITIES, "Client": clients, "AccountRole": roles}, raw_files=raw, load_order=["Household", "AccountRole", "Account", "Client"])


def test_stage_10_checks_that_no_match_value_is_still_shared():
    plan = tv.build_plan(schema="xodus_mirror", staging_schema="xodus_canon", entities={"Client": {
        "staging_table": "s_Client", "expected_rows": None, "key": "CLIENT_ID", "tables": {}, "resolution": [["TAX_ID"]]}})
    p = next(x for x in plan if x["check"] == "DUPLICATES_RESOLVED")
    assert p["field"] == "TAX_ID" and "HAVING COUNT(*) > 1" in p["sql"] and "CAST(c.[CLIENT_ID] AS nvarchar(400))" in p["keys_sql"]
    c = next(x for x in tv.evaluate(plan, lambda sql: 2 if "HAVING" in sql else 0, None) if x["check"] == "DUPLICATES_RESOLVED")
    assert c["status"] == "FAIL" and "2 match value(s)" in c["detail"]


SPEC = {"target_system": "Simplicity", "spec_version": "v3", "entities": {"Account": {"fields": [
    {"name": "T.ACCOUNT_NUMBER", "table": "T", "column": "ACCOUNT_NUMBER", "source": "ACCT_NUM"},
    {"name": "T.CUSTODIAN", "table": "T", "column": "CUSTODIAN"},
    {"name": "T.NOTES", "table": "T", "column": "NOTES"}]}}}


def test_adapter_draft_decide_confirm_is_versioned_and_immutable(conn, monkeypatch):
    monkeypatch.setattr(tm, "_source", lambda f: f.get("source"))
    assert ta.draft(conn, SPEC, {"Account": ["ACCT_NUM", "CUSTODIAN_CODE"]}) == {"added": 3, "with_source": 1}
    with pytest.raises(ValueError, match="waiting for a decision"):
        ta.confirm(conn, SPEC, "Admin")
    ta.decide(conn, "Simplicity", "Account", "T.ACCOUNT_NUMBER", "APPROVE", "Admin")
    ta.decide(conn, "Simplicity", "Account", "T.CUSTODIAN", "APPROVE", "Admin", "CUSTODIAN_CODE", ["ACCT_NUM", "CUSTODIAN_CODE"])
    ta.decide(conn, "Simplicity", "Account", "T.NOTES", "REJECT", "Admin")  # left to each migration
    v1 = ta.confirm(conn, SPEC, "Admin", "first")
    assert v1["version"] == 1 and [f["target_field"] for f in v1["fields"]] == ["T.ACCOUNT_NUMBER", "T.CUSTODIAN"]
    with pytest.raises(ValueError, match="Nothing changed"):
        ta.confirm(conn, SPEC, "Admin")
    with pytest.raises(ValueError, match="not a canonical Account field"):
        ta.decide(conn, "Simplicity", "Account", "T.NOTES", "APPROVE", "Admin", "NOPE", ["ACCT_NUM"])


def test_a_migration_adopts_the_adapter_and_reviews_only_what_it_leaves_open(conn, monkeypatch):
    monkeypatch.setattr(tm, "_source", lambda f: f.get("source"))
    with pytest.raises(ValueError, match="No adapter is confirmed"):
        ta.adopt(conn, "MIG-1", SPEC, "Ann")
    ta.draft(conn, SPEC, {"Account": ["ACCT_NUM", "CUSTODIAN_CODE"]})
    ta.decide(conn, "Simplicity", "Account", "T.ACCOUNT_NUMBER", "APPROVE", "Admin")
    ta.decide(conn, "Simplicity", "Account", "T.CUSTODIAN", "APPROVE", "Admin", "CUSTODIAN_CODE")
    ta.decide(conn, "Simplicity", "Account", "T.NOTES", "REJECT", "Admin")
    ta.confirm(conn, SPEC, "Admin")
    res = ta.adopt(conn, "MIG-1", SPEC, "Ann")
    assert res == {"version": 1, "applied": 2, "unchanged": 0, "open": 1}
    rows = {m["target_field"]: m for m in tm.list_mappings(conn, "MIG-1", SPEC)}
    assert rows["T.CUSTODIAN"]["status"] == "APPROVED" and rows["T.CUSTODIAN"]["canonical_field"] == "CUSTODIAN_CODE"
    assert "target adapter version 1" in rows["T.CUSTODIAN"]["review_comments"] and rows["T.NOTES"]["status"] == "PROPOSED"
    assert ta.adopt(conn, "MIG-1", SPEC, "Ann")["unchanged"] == 2 and ta.adopted(conn, "MIG-1", "Simplicity")["version"] == 1
    with pytest.raises(ValueError, match="target model"):
        ta.adopt(conn, "MIG-1", {**SPEC, "spec_version": "v4"}, "Ann")


def test_roles_for_the_new_routes():
    assert allowed_roles_for("POST", "/api/entity-resolution/suggest") == {"Migration Analyst", "IMO Lead"}
    assert allowed_roles_for("POST", "/api/entity-resolution/decide") == {"Migration Analyst", "IMO Lead"}
    assert allowed_roles_for("POST", "/api/entity-resolution/account-roles") == {"Migration Analyst", "IMO Lead"}
    assert allowed_roles_for("POST", "/api/target-system/adapter/confirm") == {"Platform Admin"}
    assert allowed_roles_for("POST", "/api/target-mapping/adopt-adapter") == {"Migration Analyst", "IMO Lead"}
    assert admin_may_view("/target-setup/adapter") and not admin_may_view("/entity-resolution")


def test_a_field_rejected_in_the_seed_migration_is_never_proposed_from_the_hint(conn, monkeypatch):
    monkeypatch.setattr(tm, "_source", lambda f: "ACCT_NUM")  # a misleading hint on every field
    seed = [{"entity_name": "Account", "target_field": "T.NOTES", "canonical_field": "ACCT_NUM", "transformation_expression": "DIRECT_COPY",
             "status": "REJECTED", "migration_id": "MIG-1"}]
    ta.draft(conn, SPEC, {"Account": ["ACCT_NUM"]}, seed)
    notes = next(r for r in ta.fields(conn, "Simplicity") if r["target_field"] == "T.NOTES")
    assert notes["canonical_field"] is None and notes["origin"] == "rejected in migration MIG-1"
