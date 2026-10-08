import os
import pytest
from agents.orchestrator import AgenticOrchestrator
from scripts.generate_mock_data import generate_mock_files

@pytest.fixture(scope="module")
def setup_mock_environment():
    generate_mock_files()
    storage_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data_plane", "storage")
    )
    return storage_dir

def test_agentic_orchestrator_pipeline_execution(setup_mock_environment):
    storage_dir = setup_mock_environment
    orchestrator = AgenticOrchestrator(source_dir=storage_dir)
    
    result = orchestrator.run_cohort_pipeline(rep_code="REP-9981", wave_id="WAVE-01")
    
    assert result["rep_code"] == "REP-9981"
    assert result["wave_id"] == "WAVE-01"
    assert result["extracted_sleeve_count"] == 2
    assert result["extracted_position_count"] == 2
    assert result["compliance_status"] == "APPROVED"
    assert result["data_quality_status"] == "PASS"
    assert result["load_summary"]["load_status"] == "COMPLETED"
    assert result["reconciliation_summary"]["status"] == "PASS"
    assert result["hitl_status"] == "PENDING_APPROVAL"

def test_agentic_orchestrator_unknown_advisor(setup_mock_environment):
    storage_dir = setup_mock_environment
    orchestrator = AgenticOrchestrator(source_dir=storage_dir)
    
    result = orchestrator.run_cohort_pipeline(rep_code="REP-UNKNOWN", wave_id="WAVE-01")
    
    assert result["extracted_sleeve_count"] == 0
    assert result["extracted_position_count"] == 0