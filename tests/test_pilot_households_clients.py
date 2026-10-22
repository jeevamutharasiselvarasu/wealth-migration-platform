import io
import pytest
import pandas as pd
from control_plane.services.profiler import SourceDataProfiler
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.services.mcl_validation import validate_mcl_relationships

HOUSEHOLD_CSV = b"""LEGACY_HH_ID,HOUSEHOLD_NAME,PRIMARY_REP_NUM,ESTABLISHED_DT
HH-99811,Miller Family Wealth,9981,2018-05-12
HH-99812,Johnson Living Trust,9981,2020-01-15"""

CLIENT_CSV = b"""LEGACY_CLIENT_ID,CLIENT_FIRST_NAME,CLIENT_LAST_NAME,FULL_LEGAL_NAME,TAX_IDENTIFIER,CLIENT_TYPE_RAW,EMAIL_ADDR,PHONE_NUM,HOUSEHOLD_LINK_ID,ADVISOR_REP_NUM
CL-99811A,Robert,Miller,Robert Miller,***-**-1122,INDV,robert.miller@example.com,555-0101,HH-99811,9981
CL-99812A,Arthur,Johnson,Arthur Johnson Revocable Trust,**-***4455,TRUST,arthur.j@example.com,555-0203,HH-99812,9981"""


def test_household_profiling_and_mapping():
    profile = SourceDataProfiler.profile_csv_content("households.csv", HOUSEHOLD_CSV, "Household")
    assert profile["status"] == "PROFILED"
    assert profile["row_count"] == 2
    assert "LEGACY_HH_ID" in profile["candidate_keys"]

    df = pd.read_csv(io.BytesIO(HOUSEHOLD_CSV))
    mapper = SchemaMapperAgent()
    res = mapper.map_source_to_canonical("Household", df.to_dict(orient="records"))

    assert res["status"] == "PARTIAL_MAPPED"
    assert res["confidence_score"] < 1.0
    assert res["mapped_records"][0]["HH_ID"] == "HH-99811"
    assert res["mapped_records"][0]["REP_CODE"] == "REP-9981"
    assert "ESTABLISHED_DT" in res["unmapped_fields"]


def test_client_profiling_and_mapping():
    profile = SourceDataProfiler.profile_csv_content("clients.csv", CLIENT_CSV, "Client")
    assert profile["status"] == "PROFILED"
    assert profile["row_count"] == 2
    assert "LEGACY_CLIENT_ID" in profile["candidate_keys"]

    df = pd.read_csv(io.BytesIO(CLIENT_CSV))
    mapper = SchemaMapperAgent()
    res = mapper.map_source_to_canonical("Client", df.to_dict(orient="records"))

    assert res["status"] == "SUCCESS"
    assert res["confidence_score"] == 1.0
    assert res["mapped_records"][0]["CLIENT_ID"] == "CL-99811A"
    assert res["mapped_records"][0]["CLIENT_TYPE"] == "INDIVIDUAL"
    assert res["mapped_records"][1]["CLIENT_TYPE"] == "TRUST"


def test_spine_referential_integrity():
    df_hh = pd.read_csv(io.BytesIO(HOUSEHOLD_CSV), dtype=str)
    df_cl = pd.read_csv(io.BytesIO(CLIENT_CSV), dtype=str)
    mapper = SchemaMapperAgent()

    household_mapping = mapper.map_source_to_canonical("Household", df_hh.to_dict(orient="records"))
    client_mapping = mapper.map_source_to_canonical("Client", df_cl.to_dict(orient="records"))
    valid_accounts = [{
        "ACCT_NUM": "ACC-99812",
        "HH_ID": "HH-99811",
        "CLIENT_ID": "CL-99811A",
    }]
    res = validate_mcl_relationships({
        "Account": {"mapped_records": valid_accounts},
        "Household": household_mapping,
        "Client": client_mapping,
    })

    assert res["status"] == "WARN"
    assert res["relationship_checks"][0]["status"] == "PASS"
    assert res["relationship_checks"][1]["status"] == "PASS"
    client_household = next(
        item for item in res["relationship_checks"]
        if item["check"] == "RELATIONSHIP_CANDIDATE"
    )
    assert client_household["status"] == "PROPOSED"
    assert client_household["orphan_count"] == 0


def test_approved_client_type_lookup_rule_family_executes_from_descriptive_proposal():
    mapper = SchemaMapperAgent(
        approved_transformations={
            "CLIENT_TYPE_RAW": "CLIENT_TYPE_LOOKUP (INDIVIDUAL -> INDIVIDUAL)"
        }
    )
    result = mapper.map_source_to_canonical(
        "Client",
        [{"LEGACY_CLIENT_ID": "CL-1", "FULL_LEGAL_NAME": "Jane Doe", "CLIENT_TYPE_RAW": "INDV"}],
    )
    assert result["mapped_records"][0]["CLIENT_TYPE"] == "INDIVIDUAL"
    lineage = next(item for item in result["field_lineages"] if item["source_field"] == "CLIENT_TYPE_RAW")
    assert lineage["rule_applied"] == "APPROVED_CLIENT_TYPE_LOOKUP"


def test_approved_lookup_and_format_rule_families_are_supported():
    mapper = SchemaMapperAgent(
        approved_transformations={
            "CUST_CODE_SRC": "CUSTODIAN_LOOKUP (SCHW -> SCHWAB)",
            "REGISTRATION_SRC_TYPE": "REG_TYPE_LOOKUP (INDV -> INDIVIDUAL)",
            "ACCOUNT_OPEN_DT": "DATE_FORMAT (MM/DD/YYYY -> YYYY-MM-DD)",
        }
    )
    result = mapper.map_source_to_canonical(
        "Account",
        [{
            "LEGACY_ACCT_NO": "A-1",
            "CUST_CODE_SRC": "SCHW",
            "ADVISOR_REP_NUM": "77",
            "REGISTRATION_SRC_TYPE": "INDV",
            "ACCOUNT_OPEN_DT": "3/7/2024",
        }],
    )
    record = result["mapped_records"][0]
    assert record["CUSTODIAN_CODE"] == "SCHWAB"
    assert record["REG_TYPE"] == "INDIVIDUAL"
    assert record["OPEN_DATE"] == "2024-03-07"
