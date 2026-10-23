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
    from urllib.parse import urlencode

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
                content=urlencode([
                    ("migration_name", "CoreCap to Simplicity"),
                    ("source_firm", "CoreCap"),
                    ("target_system", "Simplicity"),
                    ("canonical_version", "Simplicity-MCL-v1"),
                    ("source_acquisition", "SAMPLE_FILES"),
                    ("entities", "Account"),
                    ("entities", "Client"),
                    ("entities", "Household"),
                ]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
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



def test_live_inference_confirmation_moves_file_and_survives_restart(tmp_path, monkeypatch):
    import asyncio
    from urllib.parse import urlencode

    import httpx

    import control_plane.db as db
    import ui.state as state_module
    import ui.app as app_module
    from control_plane.services.workflow_event_engine import publish_event
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "reclassification.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module.session_mgr, "demo_mode", True)
    session_id = "phase1-reclassification-session"
    monkeypatch.setitem(
        app_module.session_mgr.active_sessions,
        session_id,
        {"email": "test@example.invalid", "name": "Data Steward", "role": "Domain SME"},
    )

    state = WorkspaceStateStore()
    migration_id = "MIG-LIVE-RECLASSIFICATION"
    setup = {"entities": ["Account", "Client", "Household"], "target_system": "Simplicity"}
    state.questionnaire_data.update({
        "migration_id": migration_id,
        "migration_setup": setup,
        "target_system": "Simplicity",
    })
    publish_event(
        "MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id,
        {"migration_id": migration_id, "migration_setup": setup, "target_system": "Simplicity"},
        "Data Steward",
    )
    publish_event(
        "ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id,
        {"scopes": {"Account": "PENDING_DISCOVERY", "Client": "PENDING_DISCOVERY", "Household": "PENDING_DISCOVERY"}},
        "Data Steward",
    )
    record = state.attach_entity_sample_file(
        "Needs clarification", "ambiguous_source.csv", b"SOURCE_KEY,RECORD_CODE\nA-1,R-1\n"
    )
    record["inference"] = {"status": "NEEDS_CLARIFICATION", "entity": None, "evidence": {}}
    record["inference_approval_status"] = "PENDING"
    record["discovery_status"] = "PENDING_REVIEW"
    publish_event(
        "SOURCE_FILE_UPLOADED", migration_id, "source_file", record["file_id"],
        app_module._workflow_safe_source_payload(record, "Needs clarification", "ENTITY_MASTER"),
        "Data Steward",
    )
    monkeypatch.setattr(app_module, "state_store", state)

    async def confirm():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            cookies={"session_id": session_id},
        ) as client:
            return await client.post(
                "/api/entities/confirm-inference",
                content=urlencode([
                    ("entity_name", "Needs clarification"),
                    ("decision", "APPROVE"),
                    ("comments", "Confirmed by the data steward"),
                    ("target_entity", "Account"),
                    ("file_id", record["file_id"]),
                ]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )

    response = asyncio.run(confirm())
    assert response.status_code == 303
    assert not any(
        item.get("file_id") == record["file_id"]
        for item in state.entity_source_files.get("Needs clarification", {}).values()
    )
    moved = next(
        item for item in state.entity_source_files["Account"].values()
        if item.get("file_id") == record["file_id"]
    )
    assert moved["entity_name"] == "Account"
    assert moved["inference_approval_status"] == "APPROVED"
    assert state.entity_sample_files["Account"]["file_id"] == record["file_id"]
    assert state.entity_scopes["Account"] == "IN_SCOPE"

    restarted = WorkspaceStateStore()
    restored = next(
        item for item in restarted.entity_source_files["Account"].values()
        if item.get("file_id") == record["file_id"]
    )
    assert restored["entity_name"] == "Account"
    assert restored["inference_approval_status"] == "APPROVED"
    assert restored["inference_review"]["reviewer"] == "Data Steward"
    assert "Needs clarification" not in restarted.entity_sample_files



def test_http_source_to_mapping_review_and_reconciliation_journey(tmp_path, monkeypatch):
    import asyncio
    from urllib.parse import urlencode

    import httpx

    import control_plane.db as db
    import ui.state as state_module
    import ui.app as app_module
    from control_plane.services.workflow_event_engine import publish_event
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mapping-journey.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module.session_mgr, "demo_mode", True)
    session_id = "phase1-mapping-journey-session"
    monkeypatch.setitem(
        app_module.session_mgr.active_sessions,
        session_id,
        {"email": "test@example.invalid", "name": "Data Steward", "role": "Domain SME"},
    )

    state = WorkspaceStateStore()
    migration_id = "MIG-HTTP-MAPPING-JOURNEY"
    setup = {
        "migration_name": "CoreCap to Simplicity",
        "source_details": "CoreCap",
        "target_system": "Simplicity",
        "canonical_version": "Simplicity-MCL-v1",
        "entities": ["Account"],
    }
    state.questionnaire_data.update({
        "migration_id": migration_id,
        "migration_setup": setup,
        "target_system": "Simplicity",
        "firm_name": "CoreCap to Simplicity",
    })
    state.entity_scopes["Account"] = "PENDING_DISCOVERY"
    publish_event(
        "MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id,
        {"migration_id": migration_id, "migration_setup": setup, "target_system": "Simplicity",
         "firm_name": "CoreCap to Simplicity"},
        "Data Steward",
    )
    publish_event(
        "ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id,
        {"scopes": {"Account": "PENDING_DISCOVERY", "Client": "OUT_OF_SCOPE", "Household": "OUT_OF_SCOPE"}},
        "Data Steward",
    )
    monkeypatch.setattr(app_module, "state_store", state)

    sample = (
        b"ACCOUNT_ID,CUST_CODE_SRC,ADVISOR_REP_NUM,REGISTRATION_SRC_TYPE,ACCOUNT_OPEN_DT\n"
        b"ACC-1001,SCHW,REP-01,INDV_TAXABLE,2021-03-15\n"
        b"ACC-1002,FID,REP-02,IRA_ROLL,2022-06-20\n"
    )

    async def journey():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
            cookies={"session_id": session_id},
        ) as client:
            uploaded = await client.post(
                "/api/entities/upload-sample-file",
                data={"source_context": "Account master file", "data_dictionary_notes": "ACCOUNT_ID is the account key"},
                files=[("files", ("accounts.csv", sample, "text/csv"))],
                follow_redirects=False,
            )
            assert uploaded.status_code == 303
            file_id, current_bucket, record = next(
                (record["file_id"], entity, record)
                for entity, files in state.entity_source_files.items()
                for record in files.values()
                if record.get("filename") == "accounts.csv"
            )

            confirmed = await client.post(
                "/api/entities/confirm-inference",
                content=urlencode([
                    ("entity_name", current_bucket),
                    ("decision", "APPROVE"),
                    ("comments", "Confirmed against the source dictionary"),
                    ("target_entity", "Account"),
                    ("file_id", file_id),
                ]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert confirmed.status_code == 303
            assert state.entity_scopes["Account"] == "IN_SCOPE"

            generated = await client.post("/api/entities/map-approved-files", follow_redirects=False)
            assert generated.status_code == 303
            assert state.entity_source_files["Account"][record["source_role"]]["mapping_generated"] is True

            pending = await client.get("/api/mapping-suggestions?status_filter=PENDING")
            suggestions = pending.json()["suggestions"]
            account_suggestions = [item for item in suggestions if item["entity_name"] == "Account"]
            assert account_suggestions, "The approved source file should create mapping proposals"
            suggestion = account_suggestions[0]

            reviewed = await client.post(
                f"/api/mapping-suggestions/{suggestion['suggestion_id']}/review",
                content=urlencode([
                    ("decision", "APPROVED"),
                    ("reviewer", "Data Steward"),
                    ("comments", "Verified against the canonical Account contract"),
                ]),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert reviewed.status_code == 200
            assert reviewed.json()["suggestion"]["status"] == "APPROVED"

            reconciliation = await client.get("/reconciliation")
            assert reconciliation.status_code == 200
            assert "Approved source-to-canonical mappings" in reconciliation.text
            return file_id, suggestion["suggestion_id"]

    file_id, suggestion_id = asyncio.run(journey())
    restarted = WorkspaceStateStore()
    restored = next(
        record for record in restarted.entity_source_files["Account"].values()
        if record.get("file_id") == file_id
    )
    assert restored["mapping_generated"] is True
    assert restored["last_mapping_run_id"]
    conn = db.get_db_connection()
    try:
        suggestion = conn.execute(
            "SELECT status FROM mapping_suggestions WHERE suggestion_id = ?", (suggestion_id,)
        ).fetchone()
        assert suggestion["status"] == "APPROVED"
        assert conn.execute(
            "SELECT 1 FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1",
            (migration_id,),
        ).fetchone()
    finally:
        conn.close()
