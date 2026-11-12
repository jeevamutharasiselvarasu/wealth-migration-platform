"""Test-only loader: builds a sample migration from tests/fixtures (the platform itself ships no demo data).

It does what a person does in the UI, through the platform's own state and event functions: save a setup, classify and
attach the sample source files, register the sample knowledge. Tests call it where they used to press "Load demo data set".
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException


def load_sample_migration(app, user: Dict[str, str]) -> Dict[str, Any]:
    HTTPException = app.HTTPException
    root_dir = app.root_dir
    load_firm_config = app.load_firm_config
    FirmConfigError = app.FirmConfigError
    P0_ENTITIES = app.P0_ENTITIES
    state_store = app.state_store
    publish_event = app.publish_event
    read_source_records = app.read_source_records
    _workflow_safe_source_payload = app._workflow_safe_source_payload
    target_connector = app.target_connector
    sql_target = app.sql_target
    _canonical_types = app._canonical_types
    get_db_connection = app.get_db_connection
    from typing import Any, Dict  # noqa: F401
    """Create the sample migration from sample_data/ (shared by the one-click route and the first-start seed). Authorisation is the caller's job."""
    manifest_path = Path(root_dir) / "tests" / "fixtures" / "sample_migration.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Sample migration configuration could not be loaded.") from exc

    setup = dict(manifest.get("migration_setup", {}))
    if not setup.get("target_system") or not setup.get("canonical_version"):
        raise HTTPException(status_code=500, detail="Sample migration configuration is missing target contract details.")
    # Same gate as /api/migration/setup: no migration without a firm configuration.
    try:
        firm_config = load_firm_config()
    except FirmConfigError as exc:
        raise HTTPException(status_code=409, detail=f"Firm configuration is required before migration setup. {exc}")
    if setup["canonical_version"] != firm_config.canonical_contract_ref:
        raise HTTPException(status_code=400, detail=f"Canonical contract reference must match the configured firm contract ({firm_config.canonical_contract_ref}).")
    setup["firm_id"] = firm_config.firm_id
    setup["firm_config_version"] = firm_config.version_id

    selected_entities = [entity for entity in setup.get("entities", []) if entity in set(P0_ENTITIES)]
    if not selected_entities:
        raise HTTPException(status_code=500, detail="Sample migration must configure at least one in-scope entity.")
    setup["entities"] = selected_entities
    migration_id = f"MIG-{uuid.uuid4().hex[:10].upper()}"
    state_store.questionnaire_data["migration_setup"] = setup
    state_store.questionnaire_data["migration_id"] = migration_id
    state_store.update_questionnaire_from_form({
        "firm_name": setup.get("migration_name", ""),
        "migration_name": setup.get("migration_name", ""),
        "target_system": setup.get("target_system", ""),
        "source_systems": setup.get("source_details", ""),
    })
    state_store.entity_sample_files.clear()
    state_store.entity_source_files.clear()
    state_store.firm_knowledge_docs.clear()
    for entity in set(P0_ENTITIES):
        state_store.entity_scopes[entity] = "PENDING_DISCOVERY" if entity in selected_entities else "OUT_OF_SCOPE"

    context_payload = {
        "migration_id": migration_id,
        "firm_name": setup.get("migration_name", ""),
        "migration_name": setup.get("migration_name", ""),
        "source_systems": setup.get("source_details", ""),
        "target_system": setup.get("target_system", ""),
        "migration_setup": setup,
    }
    state_store.questionnaire_data.update(context_payload)
    publish_event("MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id, context_payload, user["name"])
    publish_event(
        "ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id,
        {"scopes": {entity: state_store.entity_scopes[entity] for entity in P0_ENTITIES}},
        user["name"],
    )

    loaded_files = []
    for item in manifest.get("source_files", []):
        source_path = (Path(root_dir) / item["path"]).resolve()
        if not source_path.is_relative_to(Path(root_dir).resolve()):
            raise HTTPException(status_code=400, detail="Sample source path is outside the repository.")
        try:
            content = source_path.read_bytes()
            records = read_source_records(source_path.name, content)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"Sample source file could not be read: {source_path.name}") from exc
        headers = list(records[0].keys()) if records else []
        source_context = str(item.get("context", ""))
        dictionary_notes = str(item.get("dictionary_notes", ""))
        from control_plane.services.entity_classification_agent import classify_source_file
        inference = classify_source_file(
            headers, records=records,
            context=" ".join(part for part in (source_context, dictionary_notes) if part).strip(),
        )
        entity_name = inference.get("entity")
        if inference.get("status") == "NEEDS_CLARIFICATION" or entity_name not in set(P0_ENTITIES):
            entity_name = "Needs clarification"
        elif entity_name not in selected_entities:
            loaded_files.append({"filename": source_path.name, "inferred_entity": entity_name, "status": "EXCLUDED_OUT_OF_SCOPE"})
            continue
        inferred_role = (
            "RELATIONSHIP_EXTRACT" if inference.get("classification_type") == "RELATIONSHIP"
            else "ACCOUNT_MASTER" if entity_name == "Account"
            else "ENTITY_MASTER"
        )
        source_role = str(item.get("source_role") or inferred_role)
        record = state_store.attach_entity_sample_file(entity_name, source_path.name, content, source_role=source_role)
        record["inference"] = inference
        record["inferred_entity"] = entity_name
        record["inference_approval_status"] = "PENDING"
        record["discovery_status"] = "PENDING_REVIEW"
        record["source_context"] = source_context
        record["data_dictionary_notes"] = dictionary_notes
        publish_event(
            "SOURCE_FILE_UPLOADED", migration_id, "source_file", str(record["file_id"]),
            _workflow_safe_source_payload(record, entity_name, source_role), user["name"],
        )
        loaded_files.append({"filename": source_path.name, "inferred_entity": entity_name, "status": record["inference_approval_status"]})

    knowledge_items = manifest.get("knowledge_files") or [
        {"path": setup.get("knowledge_schema_ref", ""), "category": "data_dictionary"},
        {"path": setup.get("knowledge_rules_ref", ""), "category": "transformation_rule"},
        {"path": "tests/fixtures/knowledge/corecap_business_rules.md", "category": "business_rules"},
        {"path": "tests/fixtures/knowledge/corecap_source_dictionary.json", "category": "data_dictionary"},
        {"path": "tests/fixtures/knowledge/simplicity_target_layouts.json", "category": "target_schema"},
        {"path": "tests/fixtures/knowledge/corecap_migration_decisions.md", "category": "source_analysis"},
    ]
    registered_knowledge = []
    for item in knowledge_items:
        relative_path = str(item.get("path", "")).strip()
        if not relative_path:
            continue
        knowledge_path = (Path(root_dir) / relative_path).resolve()
        if not knowledge_path.is_relative_to(Path(root_dir).resolve()):
            raise HTTPException(status_code=400, detail="Sample knowledge path is outside the repository.")
        try:
            knowledge_content = knowledge_path.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Sample knowledge asset could not be read: {knowledge_path.name}") from exc
        try:
            doc = state_store.add_firm_file(knowledge_path.name, str(item.get("category", "mapping_reference")), knowledge_content)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"Sample knowledge asset could not be registered: {knowledge_path.name}") from exc
        doc["source_path"] = relative_path
        publish_event("KNOWLEDGE_ASSET_REGISTERED", migration_id, "knowledge_asset", doc["id"], doc, user["name"])
        registered_knowledge.append(doc)

    target_note = ""
    target_audit = None
    canonical_types = _canonical_types()  # read before the write transaction below: it opens its own connection
    conn = get_db_connection()
    try:
        if target_connector.get_config(conn) is None:
            if sql_target.get_config(conn) is None:
                target_note = " Target system: save the target database on the Azure connection page, then choose Use the Azure SQL database as the target on Target mapping."
            else:
                try:
                    cfg = target_connector.configure_azure_sql(conn, user["name"], "")
                    if cfg["status"] == "CONNECTED":
                        target_connector.infer_model(conn, canonical_types)
                        target_note = " Target system connected and inferred; an admin confirms it once on Target mapping."
                    else:
                        target_note = f" Target database is not ready ({cfg['connection_detail']}) Fix it, then finish the one-time target setup."
                    target_audit = cfg["status"]
                except ValueError as exc:
                    target_note = f" Target setup needs attention: {exc}"
        conn.commit()
    finally:
        conn.close()
    if target_audit:  # after the transaction is closed: the audit log writes through its own connection
        state_store.audit_log.append({"event": "TARGET_SYSTEM_CONFIGURED", "actor": user["name"],
                                      "timestamp_utc": datetime.now(timezone.utc).isoformat(), "details": {"via": "DEMO_LOAD", "status": target_audit}})
    state_store.audit_log.append({
        "event": "DEMO_MIGRATION_LOADED",
        "actor": user["name"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": {"migration_name": setup.get("migration_name"), "source_files": loaded_files},
    })
    return {"loaded_files": loaded_files, "registered_knowledge": registered_knowledge, "target_note": target_note}


def seed_confirmed_target(app) -> None:
    """Tests only: store a confirmed target system (an Orion-shaped schema from tests/fixtures) as a Platform Admin would after inferring it."""
    from control_plane.services import target_connector as tc
    from control_plane.services import target_inference as ti
    from control_plane.services.firm_config import load_firm_config
    cfg = load_firm_config()
    canonical = {e: {f.name: f.type for f in d.fields} for e, d in cfg.entities.items()}
    schema = json.loads((Path(__file__).resolve().parent / "fixtures" / "target_schema_orion.json").read_text(encoding="utf-8"))
    model = ti.infer(schema, canonical)
    now = datetime.now(timezone.utc).isoformat()
    conn = app.get_db_connection()
    try:
        tc.ensure_table(conn)
        conn.execute("DELETE FROM target_system_config")
        conn.execute("INSERT INTO target_system_config (config_id, target_name, connector_type, endpoint, scope_tables, include_code_lists, owner, status, "
                     "connection_checked_utc, connection_detail, schema_hash, inferred_model, configured_by, configured_utc, inferred_utc, confirmed_by, confirmed_utc) "
                     "VALUES (1, 'Simplicity', 'AZURE_SQL', 'azure-sql://test/Simplicity/simplicity', '[]', 1, '', 'CONFIRMED', ?, 'test', ?, ?, 'test', ?, ?, 'test', ?)",
                     (now, model["schema_hash"], json.dumps(model), now, now, now))
        conn.commit()
    finally:
        conn.close()
