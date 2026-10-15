import json
from pathlib import Path

from control_plane.kernel.wealth_kernel import WealthManagementKnowledgeKernel


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_kernel_exposes_only_three_p0_entities():
    entities = [
        item["entity_name"]
        for group in WealthManagementKnowledgeKernel.get_canonical_entities().values()
        for item in group
    ]
    assert entities == ["Account", "Client", "Household"]


def test_source_dictionary_contains_only_p0_entities_and_no_target_mapping():
    dictionary = json.loads((ROOT / "knowledge" / "amplify_dictionary.json").read_text())
    entities = [item["canonical_entity"] for item in dictionary["entities"]]
    assert entities == ["Account", "Client", "Household"]
    assert dictionary["scope"]["direction"] == "source_to_canonical"
    assert dictionary["governance"]["joins"] == "never_invent_join_keys"


def test_discovery_questionnaire_has_only_p0_mapping_context():
    full_template = (ROOT / "ui" / "templates" / "firm_knowledge.html").read_text()
    template = full_template.split("<!-- Configurable Acquisition Discovery Questionnaire Form -->", 1)[1]
    assert 'name="firm_name"' in template
    assert 'name="source_systems"' in template
    assert 'name="business_data_owner"' in template
    assert 'name="mapping_approver"' in template
    assert 'name="identifier_relationship_notes"' in template
    for deferred_field in (
        'name="advisor_count"',
        'name="operating_model"',
        'name="primary_custodian"',
        'name="source_tamp"',
        'name="performance_source"',
        'name="aum_drift_gate"',
        'name="restricted_models"',
    ):
        assert deferred_field not in template


def test_entity_model_documents_relationships_without_assumed_joins():
    model = (ROOT / "knowledge" / "p0_wealth_entity_model.md").read_text()
    assert "Account" in model and "Client" in model and "Household" in model
    assert "Never infer joins" in model
    assert "Account-to-Client" in model
    assert "Client-to-Household" in model
