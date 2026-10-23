import json
from pathlib import Path

from control_plane.services.entity_classification_agent import classify_source_file
from control_plane.services.source_file_adapter import read_source_records


ROOT = Path(__file__).resolve().parents[1]


def test_demo_manifest_files_are_all_parseable_and_classifiable():
    manifest = json.loads((ROOT / "demo_data" / "demo_migration.json").read_text(encoding="utf-8"))
    source_files = manifest["source_files"]

    assert len(source_files) == 9
    assert set(manifest["migration_setup"]["entities"]) == {"Account", "Client", "Household"}

    for item in source_files:
        path = (ROOT / item["path"]).resolve()
        assert path.is_relative_to(ROOT.resolve())
        records = read_source_records(path.name, path.read_bytes())
        assert records, f"{path.name} should include sample rows"
        result = classify_source_file(
            list(records[0].keys()),
            records=records,
            context=" ".join(
                part for part in (item.get("context", ""), item.get("dictionary_notes", ""))
                if part
            ),
        )
        assert result["status"] in {
            "INFERRED", "NEEDS_REVIEW", "NEEDS_CLARIFICATION", "RELATIONSHIP_REVIEW"
        }, path.name
        assert result["approval_status"] == "PENDING"
        assert result["classification_type"] in {"MASTER_ENTITY", "RELATIONSHIP", "UNKNOWN"}


def test_demo_knowledge_references_resolve_to_registered_inputs():
    manifest = json.loads((ROOT / "demo_data" / "demo_migration.json").read_text(encoding="utf-8"))
    setup = manifest["migration_setup"]
    knowledge_items = manifest.get("knowledge_files") or [
        {"path": setup.get("knowledge_schema_ref", "")},
        {"path": setup.get("knowledge_rules_ref", "")},
    ]

    assert len(knowledge_items) == 2
    for item in knowledge_items:
        path = (ROOT / item["path"]).resolve()
        assert path.is_relative_to(ROOT.resolve())
        assert path.is_file(), f"Knowledge reference is missing: {item['path']}"
        assert path.read_bytes().strip()



def test_demo_load_route_registers_sources_and_knowledge_across_restart(tmp_path, monkeypatch):
    import asyncio
    import httpx

    import control_plane.db as db
    import ui.state as state_module
    import ui.app as app_module
    from control_plane.services.workflow_event_engine import read_events
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "demo-flow.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module.session_mgr, "demo_mode", True)
    session_id = "phase1-demo-test-session"
    monkeypatch.setitem(
        app_module.session_mgr.active_sessions,
        session_id,
        {"email": "test@example.invalid", "name": "Phase 1 Test", "role": "Platform Admin"},
    )

    state = WorkspaceStateStore()
    monkeypatch.setattr(app_module, "state_store", state)

    async def post_demo_load():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/api/demo/load",
                cookies={"session_id": session_id},
                follow_redirects=False,
            )

    response = asyncio.run(post_demo_load())

    assert response.status_code == 303
    assert response.headers["location"].startswith("/entity-sample-files?notice=")
    migration_id = state.questionnaire_data["migration_id"]
    assert state.questionnaire_data["migration_setup"]["target_system"] == "Simplicity"
    assert sum(len(files) for files in state.entity_source_files.values()) == 9
    assert len(state.firm_knowledge_docs) == 2

    events = read_events(migration_id, limit=5000)
    source_events = [event for event in events if event["event_type"] == "SOURCE_FILE_UPLOADED"]
    assert len(source_events) == 9
    assert sum(event["event_type"] == "KNOWLEDGE_ASSET_REGISTERED" for event in events) == 2

    def contains_sample_values(value):
        if isinstance(value, dict):
            return any(
                key in {"sample_value", "sample_values"} or contains_sample_values(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_sample_values(item) for item in value)
        return False

    assert all(not contains_sample_values(event["payload"]) for event in source_events)

    restarted = WorkspaceStateStore()
    assert restarted.questionnaire_data["migration_id"] == migration_id
    assert restarted.questionnaire_data["migration_setup"]["target_system"] == "Simplicity"
    assert sum(len(files) for files in restarted.entity_source_files.values()) == 9
    assert len(restarted.firm_knowledge_docs) == 2
    assert restarted.generated_dashboard["knowledge_asset_count"] == 2



def test_manual_migration_setup_survives_restart(tmp_path, monkeypatch):
    import asyncio
    import httpx

    import control_plane.db as db
    import ui.state as state_module
    import ui.app as app_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "setup-restart.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module.session_mgr, "demo_mode", True)
    session_id = "phase1-setup-test-session"
    monkeypatch.setitem(
        app_module.session_mgr.active_sessions,
        session_id,
        {"email": "test@example.invalid", "name": "Phase 1 Test", "role": "Platform Admin"},
    )
    state = WorkspaceStateStore()
    monkeypatch.setattr(app_module, "state_store", state)

    async def save_setup():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            cookies={"session_id": session_id},
        ) as client:
            return await client.post(
                "/api/migration/setup",
                data=[
                    ("migration_name", "CoreCap to Simplicity"),
                    ("source_firm", "CoreCap"),
                    ("target_system", "Simplicity"),
                    ("canonical_version", "Simplicity-MCL-v1"),
                    ("source_acquisition", "SAMPLE_FILES"),
                    ("entities", "Account"),
                    ("entities", "Client"),
                    ("entities", "Household"),
                ],
                follow_redirects=False,
            )

    response = asyncio.run(save_setup())
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    migration_id = state.questionnaire_data["migration_id"]

    restarted = WorkspaceStateStore()
    assert restarted.questionnaire_data["migration_id"] == migration_id
    assert restarted.questionnaire_data["migration_setup"]["target_system"] == "Simplicity"
    assert restarted.questionnaire_data["migration_setup"]["canonical_version"] == "Simplicity-MCL-v1"
    assert restarted.entity_scopes["Account"] == "PENDING_DISCOVERY"
    assert restarted.entity_scopes["Client"] == "PENDING_DISCOVERY"
    assert restarted.entity_scopes["Household"] == "PENDING_DISCOVERY"
