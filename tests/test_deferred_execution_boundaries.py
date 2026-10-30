import os
from pathlib import Path

import pytest

from control_plane.services.adf_client import AzureDataFactoryClient
from control_plane.services.wave_rules import CohortConditionEngine, WaveConditionEvaluator


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_cohort_pipeline_is_retired_and_p0_workflow_remains_explicit():
    app = (ROOT / "ui" / "app.py").read_text()
    assert "from agents.orchestrator import AgenticOrchestrator" not in app
    assert "The legacy cohort pipeline is retired from P0." in app
    assert "No cohort execution, target loading, or wave assignment is available." in app
    assert '"/api/pipeline/run"' in app
    for route in (
        '"/api/entities/map-approved-files"',
        '"/api/mapping-suggestions/{suggestion_id}/review"',
    ):
        assert route in app


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
