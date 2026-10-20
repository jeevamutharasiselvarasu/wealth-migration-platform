from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.kernel.wealth_kernel import WealthManagementKnowledgeKernel


def test_pilot_canonical_catalog_contains_only_three_entities():
    catalog = WealthManagementKnowledgeKernel.get_canonical_entities()
    entities = [entity["entity_name"] for group in catalog.values() for entity in group]
    assert entities == ["Account", "Household", "Client"]
    assert all(entity["default_scope"] == "IN_SCOPE" for group in catalog.values() for entity in group)


def test_client_source_fields_map_to_client_canonical_contract():
    mapper = SchemaMapperAgent()
    result = mapper.map_source_to_canonical(
        "Client",
        [{
            "LEGACY_CLIENT_ID": "C-100",
            "FULL_LEGAL_NAME": "Sample Client",
            "CLIENT_TYPE_RAW": "INDV",
            "EMAIL_ADDR": "client@example.test",
        }],
    )

    assert result["mapped_records"] == [{
        "CLIENT_ID": "C-100",
        "LEGAL_NAME": "Sample Client",
        "CLIENT_TYPE": "INDIVIDUAL",
        "EMAIL": "client@example.test",
    }]
    assert result["missing_required"] == []
    assert result["status"] == "SUCCESS"


def test_client_mapping_flags_missing_required_fields():
    mapper = SchemaMapperAgent()
    result = mapper.map_source_to_canonical("Client", [{"FULL_LEGAL_NAME": "Sample Client"}])

    assert result["missing_required"] == ["CLIENT_ID", "CLIENT_TYPE"]
    assert result["status"] == "WARNING_INCOMPLETE"
