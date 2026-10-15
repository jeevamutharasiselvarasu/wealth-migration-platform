from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.services.mcl_validation import validate_mcl_relationships
from control_plane.services.profiler import SourceDataProfiler


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


def test_household_maps_primary_client_link_without_creating_client_entity():
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

    assert result["status"] == "SUCCESS"
    assert result["mapped_records"][0]["HH_ID"] == "HH-01"
    assert result["mapped_records"][0]["PRIMARY_PARTY_ID"] == "00017"
    assert result["mapped_records"][0]["REP_CODE"] == "REP-0024"


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
