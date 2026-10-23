from control_plane.services import workflow_event_engine as engine
from control_plane.db import init_db
from ui.state import WorkspaceStateStore


def test_state_store_rehydrates_between_workflow_stages(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.state as state_module

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "restart.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    init_db()
    engine._ensure_event_table()

    first = WorkspaceStateStore()
    context = {
        "migration_id": "MIG-STATE-RESTART",
        "firm_name": "CoreCap to Simplicity",
        "source_systems": "CoreCap",
        "target_system": "Simplicity",
    }
    first.update_questionnaire_from_form(context)
    engine.publish_event(
        "MIGRATION_CONTEXT_UPDATED", "MIG-STATE-RESTART", "migration",
        "MIG-STATE-RESTART", context, "business-data-owner",
    )
    scopes = {"Account": "IN_SCOPE", "Client": "IN_SCOPE", "Household": "OUT_OF_SCOPE"}
    first.save_entity_scopes(scopes)
    engine.publish_event(
        "ENTITY_SCOPES_UPDATED", "MIG-STATE-RESTART", "migration",
        "MIG-STATE-RESTART", {"scopes": scopes}, "migration-analyst",
    )

    # Simulate process restart before source discovery begins.
    second = WorkspaceStateStore()
    assert second.questionnaire_data["migration_id"] == "MIG-STATE-RESTART"
    assert second.questionnaire_data["firm_name"] == "CoreCap to Simplicity"
    assert second.entity_scopes["Account"] == "IN_SCOPE"
    assert second.entity_scopes["Household"] == "OUT_OF_SCOPE"

    record = second.attach_entity_sample_file(
        "Account", "accounts.csv", b"ACCOUNT_ID,CLIENT_ID\nA-1,C-1\n"
    )
    engine.publish_event(
        "SOURCE_FILE_UPLOADED", "MIG-STATE-RESTART", "source_file",
        record["file_id"], {**record, "entity": "Account"}, "migration-analyst",
    )
    doc = second.add_firm_file(
        "account_dictionary.md", "data_dictionary",
        b"ACCOUNT_ID maps to the canonical Account identifier.",
    )
    engine.publish_event(
        "KNOWLEDGE_ASSET_REGISTERED", "MIG-STATE-RESTART", "knowledge_asset",
        doc["id"], doc, "knowledge-curator",
    )

    # Restart again after ingestion/knowledge registration and verify module-facing state.
    third = WorkspaceStateStore()
    assert third.questionnaire_data["migration_id"] == "MIG-STATE-RESTART"
    assert any(
        item.get("file_id") == record["file_id"]
        for item in third.entity_source_files["Account"].values()
    )
    assert third.entity_sample_files["Account"]["filename"] == "accounts.csv"
    assert any(item.get("id") == doc["id"] for item in third.firm_knowledge_docs)
    assert third.generated_dashboard["knowledge_asset_count"] == 1

def test_inference_review_decision_survives_workspace_restart(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.state as state_module

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review-restart.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    init_db()
    engine._ensure_event_table()

    first = WorkspaceStateStore()
    migration_id = "MIG-INFERENCE-RESTART"
    context = {
        "migration_id": migration_id,
        "firm_name": "CoreCap to Simplicity",
        "migration_setup": {"entities": ["Account", "Client", "Household"]},
    }
    first.questionnaire_data.update(context)
    engine.publish_event(
        "MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id,
        context, "analyst",
    )
    record = first.attach_entity_sample_file(
        "Needs clarification", "accounts.csv", b"ACCOUNT_ID,CLIENT_ID\nA-1,C-1\n"
    )
    record["inference"] = {
        "status": "NEEDS_CLARIFICATION",
        "entity": None,
        "evidence": {"Account": {"matched_fields": ["ACCOUNT_ID"], "signature_fields": ["ACCOUNT_ID"]}},
    }
    record["inference_approval_status"] = "PENDING"
    record["discovery_status"] = "PENDING_REVIEW"
    engine.publish_event(
        "SOURCE_FILE_UPLOADED", migration_id, "source_file", record["file_id"],
        {**record, "entity": "Needs clarification"}, "analyst",
    )
    review = {
        "decision": "APPROVE",
        "entity": "Account",
        "previous_entity": "Needs clarification",
        "inference": {**record["inference"], "status": "INFERRED", "entity": "Account"},
        "inference_approval_status": "APPROVED",
        "discovery_status": "APPROVED",
        "inference_review": {
            "decision": "APPROVE", "reviewer": "Data Steward",
            "comments": "Reviewed header evidence", "reviewed_utc": "2026-10-23T00:00:00+00:00",
        },
        "scope": "IN_SCOPE",
    }
    engine.publish_event(
        "ENTITY_INFERENCE_REVIEWED", migration_id, "source_file", record["file_id"],
        review, "Data Steward",
    )

    restarted = WorkspaceStateStore()
    restored = next(
        item for item in restarted.entity_source_files["Account"].values()
        if item.get("file_id") == record["file_id"]
    )
    assert not any(
        item.get("file_id") == record["file_id"]
        for item in restarted.entity_source_files.get("Needs clarification", {}).values()
    )
    assert restored["entity_name"] == "Account"
    assert restored["inference_approval_status"] == "APPROVED"
    assert restored["discovery_status"] == "APPROVED"
    assert restored["inference_review"]["reviewer"] == "Data Steward"
    assert restarted.entity_scopes["Account"] == "IN_SCOPE"

