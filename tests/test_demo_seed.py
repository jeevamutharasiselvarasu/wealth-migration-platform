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


def test_storage_status_is_admin_only_and_reports_counts_without_values(tmp_path, monkeypatch):
    import asyncio
    import httpx
    app_module, session_id, state = _setup_client(tmp_path, monkeypatch, "storage-status")
    monkeypatch.setenv("XODUS_SEED_DEMO", "1")
    assert app_module.seed_demo_if_empty().startswith("seeded:")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "analyst-s", {"email": "a@example.invalid", "name": "A", "role": "Migration Analyst"})

    async def get(sid):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://testserver", cookies={"session_id": sid}) as c:
            return await c.get("/api/admin/storage-status")
    ok, denied = asyncio.run(get(session_id)), asyncio.run(get("analyst-s"))
    assert denied.status_code == 403
    body = ok.json()
    assert ok.status_code == 200 and body["database"] in {"sqlite", "postgresql"}
    assert body["tables"].get("workflow_events", 0) > 0 and body["workflow_events_by_type"].get("SOURCE_FILE_UPLOADED") == 9
    assert "ACCT" not in ok.text and "LEGACY" not in ok.text  # counts only, no source values


def test_seed_does_not_add_a_demo_when_a_new_migration_was_started_but_not_configured(tmp_path, monkeypatch):
    from tests.test_migration_workspaces import _call
    from ui.state import WorkspaceStateStore
    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "seed-new")
    assert _post_demo_load(app_module, sid).status_code == 303
    first_id = state.questionnaire_data["migration_id"]
    assert _call(app_module, sid, "POST", "/api/migration/new", [("template_id", first_id)]).status_code == 303
    started_id = state.questionnaire_data["migration_id"]
    assert started_id != first_id and not state.questionnaire_data["migration_setup"]
    # Restart: the active migration is the unconfigured one. The seed must not create a second demo migration.
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    monkeypatch.setenv("XODUS_SEED_DEMO", "1")
    assert app_module.seed_demo_if_empty() == "skipped: an earlier migration exists"
    assert app_module.state_store.questionnaire_data["migration_id"] == started_id
