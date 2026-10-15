import os

import pytest

from agents.orchestrator import AgenticOrchestrator
from control_plane.db import init_db
from control_plane.services.adf_client import AzureDataFactoryClient
from control_plane.services.wave_rules import CohortConditionEngine, WaveConditionEvaluator
from scripts.generate_mock_data import generate_mock_files


@pytest.fixture(scope="module")
def setup_mock_environment():
    generate_mock_files()
    init_db()
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data_plane", "storage")
    )


def test_pipeline_is_source_to_canonical_only(setup_mock_environment):
    orchestrator = AgenticOrchestrator(source_dir=setup_mock_environment)

    result = orchestrator.run_cohort_pipeline(
        rep_code="REP-9981",
        in_scope_entity_names=["Sleeve / Allocation", "Position (Snapshot)"],
        migration_id="TEST-MIGRATION",
    )

    assert result["rep_code"] == "REP-9981"
    assert result["wave_assignment"] == "DISABLED"
    assert result["execution_boundary"]["source_to_canonical_only"] is True
    assert result["execution_boundary"]["target_loading"] == "NOT_RUN"
    assert result["execution_boundary"]["cutover"] == "NOT_RUN"
    assert result["execution_boundary"]["reconciliation"] == "NOT_RUN"
    assert set(result["migration_readiness"]["in_scope_entities"]) <= {"Sleeve", "Position"}
    assert "load_summary" not in result
    assert "reconciliation_summary" not in result
    assert result["run_id"].startswith("RUN-")


def test_pipeline_blocks_when_no_entity_is_explicitly_in_scope(setup_mock_environment):
    orchestrator = AgenticOrchestrator(source_dir=setup_mock_environment)

    result = orchestrator.run_cohort_pipeline(
        rep_code="REP-9981",
        in_scope_entity_names=[],
        migration_id="TEST-MIGRATION",
    )

    assert result["status"] == "BLOCKED_NO_SCOPE"
    assert result["migration_readiness"]["in_scope_entities"] == []
    assert result["wave_assignment"] == "DISABLED"


def test_unknown_advisor_does_not_get_synthetic_records(setup_mock_environment):
    orchestrator = AgenticOrchestrator(source_dir=setup_mock_environment)

    result = orchestrator.run_cohort_pipeline(
        rep_code="REP-UNKNOWN",
        in_scope_entity_names=["Sleeve", "Position"],
        migration_id="TEST-MIGRATION",
    )

    assert result["status"] == "NEEDS_REMEDIATION"
    assert result["mapping_reports"]["Sleeve"]["status"] == "NO_SOURCE_DATA"
    assert result["mapping_reports"]["Position"]["status"] == "NO_SOURCE_DATA"
    assert result["mapping_reports"]["Sleeve"]["mapped_records"] == []
    assert result["mapping_reports"]["Position"]["mapped_records"] == []


def test_wave_conditions_are_catalogued_but_not_evaluated():
    conditions = CohortConditionEngine.get_condition_catalog()
    assert conditions
    assert all(item["status"] == "CONFIGURED_NOT_EVALUATED" for item in conditions)

    with pytest.raises(RuntimeError, match="Wave assignment is disabled"):
        WaveConditionEvaluator.assign_wave_for_record({"CUSTODIAN_CODE": "SCHWAB"})


def test_adf_mock_never_claims_real_success(monkeypatch):
    monkeypatch.setenv("ADF_MODE", "mock")
    client = AzureDataFactoryClient()

    result = client.trigger_entity_pipeline(
        migration_id="TEST-MIGRATION",
        wave_id="NOT_ASSIGNED",
        run_id="RUN-TEST",
        rep_code="REP-9981",
    )

    assert result["status"] == "MOCKED"
    assert result["mode"] == "MOCK"
    assert "no Azure pipeline was submitted" in result["message"]

    status = client.get_pipeline_run_status(result["adf_run_id"])
    assert status["status"] == "MOCKED"
    assert "no Azure execution occurred" in status["message"]
