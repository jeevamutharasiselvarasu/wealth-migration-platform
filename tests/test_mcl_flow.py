import json
import sqlite3

from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.services.mcl_validation import validate_mcl_relationships
from control_plane.services.profiler import SourceDataProfiler
import ui.state as state_module
from ui.state import WorkspaceStateStore


def test_client_source_maps_to_party_canonical_fields():
    mapper = SchemaMapperAgent()
    result = mapper.map_source_to_canonical(
        "Client",
        [{
            "CLIENT_ID": "00017",
            "FULL_LEGAL_NAME": "Sample Client",
            "TAX_IDENTIFIER": "MASKED-TAX-ID",
            "CLIENT_TYPE_RAW": "INDV",
        }],
    )

    assert result["status"] == "SUCCESS"
    assert result["mapped_records"][0]["PARTY_ID"] == "00017"
    assert result["mapped_records"][0]["LEGAL_NAME"] == "Sample Client"
    assert result["mapped_records"][0]["PARTY_TYPE"] == "INDIVIDUAL"


def test_household_maps_core_fields_and_keeps_unconfirmed_client_link_unresolved():
    mapper = SchemaMapperAgent()
    result = mapper.map_source_to_canonical(
        "Household",
        [{
            "SRC_HH_ID": "HH-01",
            "HOUSEHOLD_NAME": "Sample Household",
            "ADVISOR_REP_NUM": "0024",
            "PRIMARY_CLIENT_ID": "00017",
        }],
    )

    assert result["status"] == "PARTIAL_MAPPED"
    assert result["mapped_records"][0]["HH_ID"] == "HH-01"
    assert result["mapped_records"][0]["REP_CODE"] == "REP-0024"
    assert "PRIMARY_CLIENT_ID" in result["unmapped_fields"]
    assert any(item["source_field"] == "PRIMARY_CLIENT_ID" and item["canonical_field"] is None for item in result["field_lineages"])


def test_mcl_validation_passes_for_resolvable_keys_and_relationships():
    result = validate_mcl_relationships({
        "Account": {"mapped_records": [
            {"ACCT_NUM": "0001", "HH_ID": "HH-01", "PARTY_ID": "00017"}
        ]},
        "Household": {"mapped_records": [
            {"HH_ID": "HH-01", "PRIMARY_PARTY_ID": "00017"}
        ]},
        "Client": {"mapped_records": [
            {"PARTY_ID": "00017"}
        ]},
    })

    assert result["status"] == "PASS"
    assert result["summary"]["failed"] == 0
    assert all(item["status"] == "PASS" for item in result["key_checks"])
    assert all(item["status"] == "PASS" for item in result["relationship_checks"])


def test_mcl_validation_flags_orphan_household_reference():
    result = validate_mcl_relationships({
        "Account": {"mapped_records": [
            {"ACCT_NUM": "0001", "HH_ID": "HH-MISSING", "PARTY_ID": "00017"}
        ]},
        "Household": {"mapped_records": [{"HH_ID": "HH-01"}]},
        "Client": {"mapped_records": [{"PARTY_ID": "00017"}]},
    })

    assert result["status"] == "FAIL"
    household_link = result["relationship_checks"][0]
    assert household_link["status"] == "FAIL"
    assert household_link["orphan_values"] == ["HH-MISSING"]


def test_mcl_validation_does_not_infer_absent_relationship_columns():
    result = validate_mcl_relationships({
        "Account": {"mapped_records": [{"ACCT_NUM": "0001"}]},
        "Household": {"mapped_records": [{"HH_ID": "HH-01"}]},
        "Client": {"mapped_records": [{"PARTY_ID": "00017"}]},
    })

    assert result["status"] == "WARN"
    assert result["relationship_checks"][0]["status"] == "NOT_EVALUATED"

def test_mcl_validation_flags_duplicate_canonical_keys():
    result = validate_mcl_relationships({
        "Account": {"mapped_records": [
            {"ACCT_NUM": "0001"},
            {"ACCT_NUM": "0001"},
        ]},
        "Household": {"mapped_records": [{"HH_ID": "HH-01"}]},
        "Client": {"mapped_records": [{"PARTY_ID": "00017"}]},
    })

    account_key = result["key_checks"][0]
    assert result["status"] == "FAIL"
    assert account_key["status"] == "FAIL"
    assert account_key["duplicate_values"] == ["0001"]


def test_mapper_emits_lineage_for_unmapped_source_columns():
    mapper = SchemaMapperAgent()
    result = mapper.map_source_to_canonical(
        "Client",
        [{
            "CLIENT_ID": "00017",
            "FULL_LEGAL_NAME": "Sample Client",
            "TAX_IDENTIFIER": "MASKED-TAX-ID",
            "CLIENT_TYPE_RAW": "INDV",
            "SOURCE_NOTE_UNKNOWN": "not silently dropped",
        }],
    )

    lineage = {item["source_field"]: item for item in result["field_lineages"]}
    assert lineage["CLIENT_ID"]["canonical_field"] == "PARTY_ID"
    assert lineage["SOURCE_NOTE_UNKNOWN"]["canonical_field"] is None
    assert lineage["SOURCE_NOTE_UNKNOWN"]["rule_applied"] == "UNRESOLVED_NO_CANONICAL_FIELD"


def test_mapper_flags_two_source_columns_mapped_to_one_canonical_field():
    mapper = SchemaMapperAgent()
    result = mapper.map_source_to_canonical(
        "Client",
        [{
            "CLIENT_ID": "00017",
            "PARTY_ID": "00018",
            "FULL_LEGAL_NAME": "Sample Client",
            "TAX_IDENTIFIER": "MASKED-TAX-ID",
            "CLIENT_TYPE_RAW": "INDV",
        }],
    )

    assert result["status"] == "WARNING_INCOMPLETE"
    assert result["field_collisions"]
    assert any("Multiple source columns map" in warning for warning in result["warnings"])

def test_profiler_preserves_leading_zero_identifier_values():
    profile = SourceDataProfiler.profile_csv_content(
        "clients.csv",
        b"CLIENT_ID,CLIENT_TYPE_RAW\n00017,INDV\n00018,TRUST\n",
        entity_name="Client",
    )

    client_id_profile = next(item for item in profile["column_profiles"] if item["column_name"] == "CLIENT_ID")
    assert client_id_profile["sample_values"] == ["00017", "00018"]
    assert client_id_profile["inferred_type"] == "string"

def test_mcl_batch_persistence_stores_metadata_not_source_values(tmp_path, monkeypatch):
    db_path = tmp_path / "mcl-test.db"

    def connect_test_db():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    connection = connect_test_db()
    connection.execute("""
        CREATE TABLE mcl_execution_runs (
            mcl_run_id TEXT PRIMARY KEY,
            migration_id TEXT NOT NULL,
            status TEXT NOT NULL,
            entity_summary_json TEXT NOT NULL,
            relationship_summary_json TEXT NOT NULL,
            execution_boundary_json TEXT NOT NULL,
            executed_utc TEXT NOT NULL
        )
    """)
    connection.commit()
    connection.close()
    monkeypatch.setattr(state_module, "get_db_connection", connect_test_db)
    store = WorkspaceStateStore()

    store.persist_mcl_execution_summary(
        mcl_run_id="MCL-TEST",
        status="NEEDS_REMEDIATION",
        entity_results={
            "Client": {
                "run_id": "RUN-CLIENT",
                "canonical_entity": "Party",
                "source_file": "clients.csv",
                "profile": {"row_count": 1, "column_count": 2},
                "mapping": {
                    "status": "SUCCESS",
                    "mapped_records": [{"PARTY_ID": "PRIVATE-CLIENT-ID"}],
                    "mapped_fields": ["PARTY_ID"],
                    "unmapped_fields": [],
                    "missing_required": [],
                    "confidence_score": 1.0,
                },
            }
        },
        relationship_validation={
            "status": "FAIL",
            "summary": {"failed": 1},
            "key_checks": [{"status": "FAIL", "duplicate_values": ["PRIVATE-CLIENT-ID"]}],
            "relationship_checks": [{"status": "FAIL", "orphan_values": ["PRIVATE-CLIENT-ID"]}],
        },
        execution_boundary={"target_loading": "NOT_RUN", "wave_assignment": "DISABLED"},
    )

    connection = connect_test_db()
    row = connection.execute("SELECT * FROM mcl_execution_runs WHERE mcl_run_id = 'MCL-TEST'").fetchone()
    connection.close()
    persisted_text = json.dumps(dict(row))
    assert "PRIVATE-CLIENT-ID" not in persisted_text
    assert "clients.csv" in persisted_text
    assert "RUN-CLIENT" in persisted_text
