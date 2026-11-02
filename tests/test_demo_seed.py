"""First-start demo seed: loads the demo template once, never over an existing migration, and only when asked."""
from tests.test_firm_config import _post_demo_load, _setup_client


def test_seed_is_off_unless_enabled(tmp_path, monkeypatch):
    app_module, _, state = _setup_client(tmp_path, monkeypatch, "seed-off")
    monkeypatch.delenv("XODUS_SEED_DEMO", raising=False)
    assert app_module.seed_demo_if_empty() == "disabled"
    assert "migration_setup" not in state.questionnaire_data


def test_seed_loads_the_demo_once_and_does_not_repeat(tmp_path, monkeypatch):
    app_module, _, state = _setup_client(tmp_path, monkeypatch, "seed-on")
    monkeypatch.setenv("XODUS_SEED_DEMO", "1")
    first = app_module.seed_demo_if_empty()
    assert first.startswith("seeded:"), first
    migration_id = state.questionnaire_data["migration_id"]
    assert state.entity_sample_files and state.firm_knowledge_docs
    assert app_module.seed_demo_if_empty() == "skipped: a migration already exists"
    assert state.questionnaire_data["migration_id"] == migration_id
    audit = [e for e in state.audit_log if e.get("event") == "DEMO_MIGRATION_LOADED"]
    assert audit and audit[-1]["actor"] == "System seed"


def test_seed_never_replaces_a_users_migration(tmp_path, monkeypatch):
    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "seed-keep")
    assert _post_demo_load(app_module, session_id).status_code == 303
    migration_id = state.questionnaire_data["migration_id"]
    monkeypatch.setenv("XODUS_SEED_DEMO", "1")
    assert app_module.seed_demo_if_empty().startswith("skipped")
    assert state.questionnaire_data["migration_id"] == migration_id
