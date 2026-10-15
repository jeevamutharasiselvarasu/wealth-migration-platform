from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.services.mcl_validation import validate_mcl_relationships


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
