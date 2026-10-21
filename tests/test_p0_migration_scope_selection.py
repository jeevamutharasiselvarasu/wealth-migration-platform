from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_migration_setup_exposes_only_the_three_p0_entity_scope_options():
    template = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    assert 'name="entities"' in template
    assert "Account" in template and "Household" in template and "Client" in template
    assert "CoreCap" in template
    assert "Simplicity" in template


def test_setup_requires_at_least_one_selected_entity_and_marks_others_out_of_scope():
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'form.getlist("entities")' in app
    assert "Select at least one in-scope entity" in app
    assert 'else "OUT_OF_SCOPE"' in app


def test_upload_and_approval_routes_enforce_selected_entity_scope():
    app = (ROOT / "ui" / "app.py").read_text()
    assert "file was not registered for downstream processing" in app
    assert "cannot be approved or mapped" in app
    assert 'entity_name not in configured_scope' in app


def test_agent_assistance_is_integrated_into_source_discovery():
    nav = (ROOT / "ui" / "templates" / "base.html").read_text()
    discovery = (ROOT / "ui" / "templates" / "entity_sample_files.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'href="/agent-studio"' not in nav
    assert "Agent assistance runs inside Source Discovery automatically." in nav
    assert "Agent-assisted analysis · automatic" in discovery
    assert "Suggested source → canonical fields" in discovery
    assert 'MigrationAgentCoordinator().analyze(' in app
    assert "inference_approval_status" in discovery
    assert "wave assignment remains deferred" not in nav


def test_dashboard_renders_only_entities_selected_in_migration_setup():
    dashboard = (ROOT / "ui" / "templates" / "dashboard.html").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    assert 'for entity in in_scope_entities|default([])' in dashboard
    assert '"in_scope_entities": [entity for entity in ("Account", "Household", "Client") if entity in state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])]' in app
    assert 'Account, Household, Client</div>' not in dashboard
    assert 'ENTITY 02</span><span' not in dashboard


def test_platform_knowledge_is_not_stored_under_demo_data_or_registered_as_migration_input():
    manifest = (ROOT / "demo_data" / "demo_migration.json").read_text()
    app = (ROOT / "ui" / "app.py").read_text()
    setup = (ROOT / "ui" / "templates" / "migration_setup.html").read_text()
    assert (ROOT / "platform_knowledge" / "canonical_contract.md").exists()
    assert (ROOT / "platform_knowledge" / "source_mapping_notes.md").exists()
    assert not (ROOT / "demo_data" / "knowledge" / "canonical_contract.md").exists()
    assert not (ROOT / "demo_data" / "knowledge" / "source_mapping_notes.md").exists()
    assert '"knowledge_files"' not in manifest
    assert 'platform_knowledge/canonical_contract.md' in manifest
    assert 'platform_knowledge/source_mapping_notes.md' in manifest
    assert 'platform_knowledge/canonical_contract.md' in setup
    assert 'platform_knowledge/source_mapping_notes.md' in setup
    assert 'for item in manifest.get("knowledge_files", [])' not in app
