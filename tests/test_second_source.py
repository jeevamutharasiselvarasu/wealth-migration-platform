"""Canonical phasing step 5: a second source with its own layout and coding goes through the same canonical model.

"Ledgerline" (tests/fixtures/ledgerline_source_files) shares nothing with CoreCap: other file split (parties, portfolios, owners, families,
managers), other column names, other date and money formats, and its own spellings of codes. Nothing in the canonical model, the code
sets, entity resolution or the pipelines changes for it: only the mappings and the approved code values are new.
"""
import csv
import sqlite3
from pathlib import Path

import pytest

from control_plane.services import canonical_model as cm
from control_plane.services import code_mappings as cmap
from control_plane.services import entity_resolution as er
from control_plane.services import transformation_rules as tr
from control_plane.services import xodus_pipelines as xp
from tests.test_xodus_pipelines import ENV, SPEC, TARGET, build

DATA = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ledgerline_source_files"
DATE = {"input_formats": ["MM/DD/YYYY"]}
MONEY = {"strip_chars": "$,", "decimals": 2, "parentheses_negative": True}


def read(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def model():
    return cm.load_definition()


def canonical_rows():
    """Source -> canonical for each entity: column renames, typed values and (for the derived status) one rule; codes stay as the source writes them."""
    def date(v):
        return tr.apply_rule("DATE_FORMAT", tr.validate_params("DATE_FORMAT", DATE), v)[0]

    managers = {m["MGR_CD"]: m for m in read("ll_managers.csv")}
    accounts = [{"ACCT_NUM": p["PORT_NO"], "ACCT_NAME": p["PORT_TITLE"], "CUSTODIAN_CODE": p["BRK_CD"], "REG_TYPE": p["ACCT_TYPE"], "TAX_STATUS": p["TAX_CLASS"],
                 "OPEN_DATE": date(p["OPEN_DT"]), "CLOSED_DATE": date(p["CLOSE_DT"]) or "",
                 "ACCT_STATUS": tr.apply_rule("IF_BLANK", {"when_blank": "Open", "when_filled": "Closed"}, p["CLOSE_DT"])[0],
                 "BALANCE": tr.apply_rule("NUMBER_CLEAN", tr.validate_params("NUMBER_CLEAN", MONEY), p["MKT_VAL"])[0], "HH_ID": p["FAMILY_CD"], "REP_CODE": p["MGR_CD"]}
                for p in read("ll_portfolios.csv")]
    clients = [{"CLIENT_ID": p["PARTY_NO"], "FIRST_NAME": p["FNAME"], "LAST_NAME": p["LNAME"], "TAX_ID": p["SSN"], "DATE_OF_BIRTH": date(p["DOB"]) or "",
                "CLIENT_TYPE": "ORG" if p["PARTY_KIND"] == "ORG" else "INDIVIDUAL", "HH_ID": p["FAMILY_CD"], "HH_ORDER": p["SEQ"], "HH_RELATIONSHIP": p["REL_CD"],
                "EMAIL": p["EMAIL_ADDR"], "PHONE": p["CELL"]} for p in read("ll_parties.csv")]
    households = [{"HH_ID": f["FAMILY_CD"], "HH_NAME": f["FAMILY_NM"], "ADVISOR_ID": f["MGR_CD"], "CREATED_DATE": date(f["SINCE"])} for f in read("ll_families.csv")]
    advisors = [{"ADVISOR_ID": m["MGR_CD"], "ADVISOR_NAME": m["MGR_NAME"], "EMAIL": m["MGR_EMAIL"], "IS_ACTIVE": "true" if m["ACTIVE_FLG"] == "Y" else "false"}
                for m in managers.values()]
    roles = [{"ACCT_NUM": o["PORT_NO"], "CLIENT_ID": o["PARTY_NO"], "ROLE": o["OWNER_TYPE"]} for o in read("ll_owners.csv")]
    return {"Household": households, "Client": clients, "Account": accounts, "AccountRole": roles, "Advisor": advisors}


def test_the_second_source_really_has_another_layout():
    corecap = {"AccountNumber", "CustodianName", "RegistrationType", "TaxStatus", "HouseholdId", "RepCode"}
    assert not corecap & set(read("ll_portfolios.csv")[0])
    assert {p["BRK_CD"] for p in read("ll_portfolios.csv")} == {"SCHW", "FID", "PERSH"}  # CoreCap writes "Schwab", "Fidelity", ...


def test_its_rows_are_canonical_typed_and_fit_the_same_entities(model):
    rows = canonical_rows()
    for entity, entity_rows in rows.items():
        names = {f["name"] for f in model["entities"][entity]["fields"]}
        assert entity_rows and all(set(r) <= names for r in entity_rows), entity
    acct = {a["ACCT_NUM"]: a for a in rows["Account"]}
    assert acct["LL-0001"]["OPEN_DATE"] == "2019-03-15" and acct["LL-0001"]["BALANCE"] == "412300.55" and acct["LL-0001"]["ACCT_STATUS"] == "Open"
    assert acct["LL-0004"]["CLOSED_DATE"] == "2025-02-28" and acct["LL-0004"]["ACCT_STATUS"] == "Closed"
    assert acct["LL-0006"]["BALANCE"] == "-250.75"  # parentheses are a negative amount


def test_source_codes_reach_canonical_codes_through_the_same_code_sets(conn, model):
    rows = canonical_rows()
    res = cmap.propose(conn, "MIG-LL", model, rows)
    by = {(r["code_set"], r["source_value"]): r for r in cmap.list_mappings(conn, "MIG-LL")}
    # what the code sets already know is suggested
    assert by[("custodian", "SCHW")]["canonical_code"] == "SCHWAB" and by[("custodian", "FID")]["canonical_code"] == "FIDELITY"
    assert by[("custodian", "PERSH")]["canonical_code"] == "PERSHING"
    assert by[("registration_type", "JTWROS")]["canonical_code"] == "JOINT_WROS" and by[("registration_type", "IRA_ROLL")]["canonical_code"] == "IRA_ROLLOVER"
    assert by[("tax_status", "DEFERRED")]["canonical_code"] == "TAX_DEFERRED" and by[("tax_status", "TAXFREE")]["canonical_code"] == "TAX_EXEMPT"
    assert by[("account_status", "CLOSED")]["canonical_code"] == "CLOSED" and by[("household_role", "HOH")]["canonical_code"] == "HEAD"
    # what it does not know is left for a person: no guess is made
    open_values = {(cs, v) for (cs, v), r in by.items() if not r["canonical_code"]}
    assert open_values == {("household_role", "SPS"), ("household_role", "DEP"), ("account_role", "CUSTODIAN-ADULT")}
    assert res["suggested"] == res["values"] - len(open_values)
    assert cmap.approve_suggested(conn, "MIG-LL", model, "Ann") == res["suggested"]
    cmap.decide(conn, "MIG-LL", model, "household_role", "SPS", "APPROVE", "SPOUSE", "Ann")
    cmap.decide(conn, "MIG-LL", model, "household_role", "DEP", "APPROVE", "DEPENDENT", "Ann")
    cmap.decide(conn, "MIG-LL", model, "account_role", "CUSTODIAN-ADULT", "APPROVE", "AUTHORIZED", "Ann")
    # after approval every coded value of the source conforms to its set
    approved = {(m["code_set"], m["source_value"]): m["canonical_code"] for m in cmap.approved_source_map(conn, "MIG-LL")}
    for entity, fields in cm.coded_fields(model).items():
        for row in rows.get(entity, []):
            for field, code_set in fields.items():
                if row.get(field):
                    assert approved[(code_set, cm.normalise(row[field]))] in cm.codes(model, code_set), (entity, field, row[field])


def test_the_same_person_in_two_party_records_is_resolved_by_the_same_rules():
    clients = canonical_rows()["Client"]
    out, xref, stats = er.resolve(clients, "CLIENT_ID", er.SUGGESTED_RULES["Client"])
    assert stats["merged"] == 1 and xref["P102"] == xref["P101"] == "P101"
    survivor = next(r for r in out if r["CLIENT_ID"] == "P101")
    assert survivor["EMAIL"] == "louis@example.test" and survivor["DATE_OF_BIRTH"] == "1959-09-30"  # the most complete record, blanks filled from the other
    roles = [{"ACCT_NUM": "LL-0003", "CLIENT_ID": "P102"}]
    er.repoint(roles, "CLIENT_ID", xref)
    assert roles[0]["CLIENT_ID"] == "P101"


def test_the_pipelines_are_the_same_for_the_second_source():
    """A new source changes the generated SQL and its data rows, never the pipeline definitions."""
    entities = {
        "Household": {"files": ["ll_families.csv"], "key": "HH_ID", "derivations": [], "canonical_fields": ["HH_ID", "HH_NAME", "ADVISOR_ID"],
                      "mappings": {"FAMILY_CD": {"canonical_field": "HH_ID", "expression": "DIRECT_COPY"}, "FAMILY_NM": {"canonical_field": "HH_NAME", "expression": "TRIM"},
                                   "MGR_CD": {"canonical_field": "ADVISOR_ID", "expression": "DIRECT_COPY"}}},
        "Account": {"files": ["ll_portfolios.csv"], "key": "ACCT_NUM", "derivations": [], "canonical_fields": ["ACCT_NUM", "CUSTODIAN_CODE", "HH_ID"],
                    "mappings": {"PORT_NO": {"canonical_field": "ACCT_NUM", "expression": "DIRECT_COPY"}, "BRK_CD": {"canonical_field": "CUSTODIAN_CODE", "expression": "DIRECT_COPY"},
                                 "FAMILY_CD": {"canonical_field": "HH_ID", "expression": "DIRECT_COPY"}}}}
    raw = [{"file_name": "ll_families.csv", "columns": ["FAMILY_CD", "FAMILY_NM", "MGR_CD"]},
           {"file_name": "ll_portfolios.csv", "columns": ["PORT_NO", "BRK_CD", "FAMILY_CD"]}]
    codes = {"coded": {"Account": {"CUSTODIAN_CODE": "custodian"}}, "target_system": "Simplicity", "model_version": "2.0.0", "target_map": [],
             "source_map": [{"code_set": "custodian", "source_value": v, "canonical_code": c} for v, c in (("SCHW", "SCHWAB"), ("FID", "FIDELITY"), ("PERSH", "PERSHING"))]}
    first, second = build(), build(entities=entities, raw_files=raw, rules={}, codes=codes)
    stable = [f"pipeline/{p}.json" for p in xp.STABLE_PIPELINES] + ["dataset/DS_XODUS_RawFile.json", "dataset/DS_XODUS_Sql.json", "dataset/DS_XODUS_SqlSimplicity.json"]
    assert all(first["files"][p] == second["files"][p] for p in stable)
    sql = second["files"]["sql/xodus_mig00a1a6577b.sql"]
    assert "[xodus_raw].[mig00a1a6577b_ll_portfolios]" in sql and "N'SCHW', N'SCHWAB'" in sql and "N'PERSH', N'PERSHING'" in sql
