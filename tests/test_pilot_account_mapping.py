import io
import pytest
import pandas as pd
from control_plane.services.profiler import SourceDataProfiler
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.db import init_db, get_db_connection

SAMPLE_CSV = b"""LEGACY_ACCT_NO,CUST_CODE_SRC,ADVISOR_REP_NUM,REGISTRATION_SRC_TYPE,ACCOUNT_OPEN_DT
ACC-99812,SCHW,9981,INDV_TAXABLE,03/15/2021
ACC-99813,SCHW,9981,IRA_ROLL,06/20/2022
ACC-10010,SCHW,1001,JT_TEN,11/01/2019"""


def test_profiler_inspects_source_file():
    profile = SourceDataProfiler.profile_csv_content("accounts.csv", SAMPLE_CSV, "Account")
    assert profile["status"] == "PROFILED"
    assert profile["row_count"] == 3
    assert profile["column_count"] == 5
    assert "LEGACY_ACCT_NO" in profile["candidate_keys"]


def test_schema_mapper_account_pilot():
    df = pd.read_csv(io.BytesIO(SAMPLE_CSV))
    raw_records = df.to_dict(orient="records")
    agent = SchemaMapperAgent()
    res = agent.map_source_to_canonical("Account", raw_records)

    assert res["status"] == "SUCCESS"
    assert res["confidence_score"] == 1.0
    assert len(res["missing_required"]) == 0
    assert len(res["unmapped_fields"]) == 0

    record0 = res["mapped_records"][0]
    assert record0["ACCT_NUM"] == "ACC-99812"
    assert record0["CUSTODIAN_CODE"] == "SCHWAB"
    assert record0["REP_CODE"] == "REP-9981"
    assert record0["REG_TYPE"] == "INDIVIDUAL"
    assert record0["OPEN_DATE"] == "2021-03-15"


def test_missing_mandatory_fields_penalty():
    incomplete_csv = b"""LEGACY_ACCT_NO,CUST_CODE_SRC,UNKNOWN_EXTRA_FIELD
ACC-101,SCHW,SOMETHING"""
    df = pd.read_csv(io.BytesIO(incomplete_csv))
    agent = SchemaMapperAgent()
    res = agent.map_source_to_canonical("Account", df.to_dict(orient="records"))

    assert res["status"] == "WARNING_INCOMPLETE"
    assert "UNKNOWN_EXTRA_FIELD" in res["unmapped_fields"]
    assert "REP_CODE" in res["missing_required"]
    assert "OPEN_DATE" in res["missing_required"]
    assert res["confidence_score"] < 0.70
    assert any("Missing required canonical fields" in w for w in res["warnings"])