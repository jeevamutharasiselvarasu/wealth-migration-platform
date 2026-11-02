"""Canonical contract 1.1.0: aligned to the CoreCap (Amplify) dictionary and the Orion MCL and SPARC layouts."""
import json
from pathlib import Path

import pytest

from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.services.entity_inference import infer_entity_from_headers
from control_plane.services.firm_config import load_firm_config

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "demo_data" / "knowledge"
CONFIG = load_firm_config(ROOT / "config" / "firm_config.yaml")
TARGETS = {"ORION_MCL", "SPARC"}
ENTITY_OF_TABLE = {"Accounts": "Account", "Households": "Household", "Household_Contacts": "Client"}


def _fields(entity):
    return set(CONFIG.canonical_field_names(entity))


def test_contract_is_1_1_0_with_the_same_three_entities_and_stable_required_fields():
    assert CONFIG.config_version == "1.1.0" and CONFIG.canonical_contract_ref == "Simplicity-MCL-v1"
    assert sorted(CONFIG.entities) == ["Account", "Client", "Household"]
    assert CONFIG.required_fields("Account") == ["ACCT_NUM", "CUSTODIAN_CODE", "REG_TYPE", "OPEN_DATE"]  # REP_CODE became optional
    assert CONFIG.required_fields("Client") == ["CLIENT_ID", "LEGAL_NAME", "TAX_ID", "CLIENT_TYPE"]
    assert CONFIG.required_fields("Household") == ["HH_ID", "HH_NAME"]
    assert CONFIG.field_contract("Household", "REP_CODE").nullable and CONFIG.field_contract("Account", "REP_CODE").nullable


def test_keys_are_unchanged_and_the_system_id_is_kept_apart_from_the_custodian_number():
    keys = {e: [f.name for f in c.fields if f.is_key] for e, c in CONFIG.entities.items()}
    assert keys == {"Account": ["ACCT_NUM"], "Client": ["CLIENT_ID"], "Household": ["HH_ID"]}
    assert {"ACCOUNT_ID", "ACCT_NUM"} <= _fields("Account")


def test_personal_data_is_marked_sensitive_and_target_requirements_name_known_layouts():
    sensitive = {f.name for f in CONFIG.entities["Client"].fields if f.sensitive}
    assert {"TAX_ID", "DATE_OF_BIRTH", "EMAIL", "PHONE", "ADDRESS_LINE_1", "LAST_NAME", "LEGAL_NAME"} <= sensitive
    assert not any(f.sensitive for f in CONFIG.entities["Account"].fields + CONFIG.entities["Household"].fields)
    for entity in CONFIG.entities.values():
        for f in entity.fields:
            assert set(f.required_for_targets) <= TARGETS, f.name
    # target needs do not make a field required for source-to-canonical mapping
    assert CONFIG.field_contract("Client", "LAST_NAME").required_for_targets and CONFIG.field_contract("Client", "LAST_NAME").nullable


def test_no_party_and_no_deferred_entity_concepts_leak_into_the_contract():
    names = {n for e in CONFIG.entities for n in _fields(e)}
    assert not {"PARTY_ID", "SLEEVE_ID", "CUSIP", "MODEL_CODE", "RISK_SCORE"} & names


def test_source_dictionary_maps_only_to_real_canonical_fields_and_covers_every_column():
    doc = json.loads((KNOWLEDGE / "corecap_source_dictionary.json").read_text(encoding="utf-8"))
    assert doc["governance"]["contains_record_values"] is False
    assert {e["source_table"]: e["canonical_entity"] for e in doc["entities"]} == ENTITY_OF_TABLE
    kinds = {"DIRECT", "LOOKUP", "DERIVED", "EXCLUDED", "DEFERRED"}
    for e in doc["entities"]:
        for f in e["fields"]:
            assert f["mapping_kind"] in kinds, f["name"]
            if f["canonical_field"]:
                assert f["canonical_field"] in _fields(e["canonical_entity"]), f"{e['source_table']}.{f['name']}"
            else:
                assert f["mapping_kind"] in {"EXCLUDED", "DEFERRED"}  # a dropped column always has a disposition
            assert "sample" not in f and "values" not in f
            if f["canonical_field"] and CONFIG.field_contract(e["canonical_entity"], f["canonical_field"]).sensitive:
                assert f["sensitive"] is True
    assert {e["source_table"]: len(e["fields"]) for e in doc["entities"]} == {"Accounts": 16, "Households": 11, "Household_Contacts": 24}


def test_target_layouts_only_reference_canonical_fields_that_exist():
    doc = json.loads((KNOWLEDGE / "simplicity_target_layouts.json").read_text(encoding="utf-8"))
    layouts = {l["layout_id"]: l for l in doc["layouts"]}
    assert {k: len(v["fields"]) for k, v in layouts.items()} == {"ORION_MCL": 27, "SPARC": 34}
    assert sum(f["in_p0_scope"] for f in layouts["ORION_MCL"]["fields"]) == 24 and sum(f["in_p0_scope"] for f in layouts["SPARC"]["fields"]) == 29
    everything = {n for e in CONFIG.entities for n in _fields(e)}
    for layout in layouts.values():
        for f in layout["fields"]:
            for name in f["canonical_fields"]:
                assert name in everything, f"{layout['layout_id']}: {f['target_header']} -> {name}"
            if f["in_p0_scope"]:
                assert f["canonical_fields"], f["target_header"]


def test_every_field_a_target_needs_is_flagged_on_the_canonical_contract():
    doc = json.loads((KNOWLEDGE / "simplicity_target_layouts.json").read_text(encoding="utf-8"))
    flagged = {(layout_id, n) for e in CONFIG.entities for n in _fields(e)
               for layout_id in CONFIG.field_contract(e, n).required_for_targets}
    needed = {l["layout_id"] for l in doc["layouts"]}
    assert {layout_id for layout_id, _ in flagged} == needed


CORECAP_ACCOUNTS = {"AccountId": "x1", "AccountNumber": "00123456", "AccountName": "Test Person - IRA", "Balance": "100.50", "CustodianName": "Schwab",
                    "TaxStatus": "Taxable", "RegistrationType": "IRA", "DefaultLiquidationMethod": "FIFO", "RepCode": "R1", "HouseholdId": "h1",
                    "LastDirectReconDate": "2026-01-01", "ReportedAsOfDate": "2026-01-02", "CustodialStartDate": "2020-05-06", "ClosedDate": "",
                    "RiskScore": "10", "RiskScoreDate": "2026-01-01"}
CORECAP_HOUSEHOLDS = {"AdvisorId": "a1", "Id": "h1", "HouseholdName": "Test, Person", "HouseholdProperName": "Mx. Test Person", "CreatedDate": "2020-01-01",
                      "RiskScore": "5", "RiskScoreDate": "2026-01-01", "RiskToleranceScore": "9", "RiskToleranceScoreDate": "2026-01-01", "DeletedDate": "", "DeletedByUserName": ""}
CORECAP_CONTACTS = {"HouseholdId": "h1", "ContactId": "c1", "Salutation": "", "FirstName": "Test", "Nickname": "", "MiddleName": "Q", "LastName": "Person", "Suffix": "",
                    "Designations": "", "JobTitle": "", "EmployerName": "Example Co", "EntityType": "individual", "DateOfBirth": "1980-01-01", "TaxId": "000000000",
                    "HouseholdOrder": "1", "HouseholdRelationshipType": "Head", "Address1": "1 Test St", "Address2": "", "City": "Testville", "State": "MI",
                    "Zip": "01234", "Country": "US", "Phone": "000", "Email": "t@example.test"}


@pytest.mark.parametrize("entity,row,expected", [
    ("Account", CORECAP_ACCOUNTS, {"AccountId": "ACCOUNT_ID", "AccountNumber": "ACCT_NUM", "AccountName": "ACCT_NAME", "CustodianName": "CUSTODIAN_NAME",
                                   "RegistrationType": "REG_TYPE", "CustodialStartDate": "OPEN_DATE", "Balance": "BALANCE", "HouseholdId": "HH_ID", "ClosedDate": "CLOSED_DATE"}),
    ("Household", CORECAP_HOUSEHOLDS, {"Id": "HH_ID", "HouseholdName": "HH_NAME", "HouseholdProperName": "HH_DISPLAY_NAME", "AdvisorId": "ADVISOR_ID", "DeletedDate": "DELETED_DATE"}),
    ("Client", CORECAP_CONTACTS, {"ContactId": "CLIENT_ID", "FirstName": "FIRST_NAME", "LastName": "LAST_NAME", "EntityType": "CLIENT_TYPE", "TaxId": "TAX_ID",
                                  "HouseholdOrder": "HH_ORDER", "Zip": "POSTAL_CODE", "Address1": "ADDRESS_LINE_1", "Email": "EMAIL"}),
])
def test_corecap_headers_map_to_the_v1_1_canonical_fields(entity, row, expected):
    result = SchemaMapperAgent().map_source_to_canonical(entity, [row])
    lineage = {l["source_field"]: l["canonical_field"] for l in result["field_lineages"]}
    for source, target in expected.items():
        assert lineage.get(source) == target, f"{entity}.{source}"
        assert target in _fields(entity)
    assert "Id" not in lineage or entity == "Household"  # the bare key is only a household id inside the Households file


@pytest.mark.parametrize("row,expected", [(CORECAP_ACCOUNTS, "Account"), (CORECAP_HOUSEHOLDS, "Household"), (CORECAP_CONTACTS, "Client")])
def test_the_three_corecap_files_are_classified_from_their_headers(row, expected):
    result = infer_entity_from_headers(list(row))
    assert result["status"] == "INFERRED" and result["entity"] == expected, result
