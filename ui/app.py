import os
import uuid
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ui.auth import session_mgr
from ui.state import state_store
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from agents.operational.cutover_agent import CutoverAgent
from agents.operational.audit_agent import AuditAgent
from data_plane.storage_manager import HybridStorageManager
from control_plane.services.profiler import SourceDataProfiler
from control_plane.services.mcl_validation import validate_mcl_relationships
from control_plane.services.source_file_adapter import read_source_records
from control_plane.services.entity_inference import infer_entity_from_headers
from control_plane.services.wave_rules import CohortConditionEngine, WaveConditionEvaluator
from control_plane.services.adf_client import AzureDataFactoryClient
from control_plane.services.adf_exporter import ADFPackageExporter
from control_plane.db import get_db_connection
from control_plane.services.workflow_event_engine import publish_event, read_events, get_projection, replay_events
from agents.p0_agent_suite import MigrationAgentCoordinator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataMigrationWorkbench")

app = FastAPI(title="Data Migration Workbench", version="3.5.0")

ui_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(ui_dir, ".."))

static_dir = os.path.join(ui_dir, "static")
templates_dir = os.path.join(ui_dir, "templates")

os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

schema_mapper = SchemaMapperAgent()
cutover_agent = CutoverAgent()
audit_agent = AuditAgent()
adf_client = AzureDataFactoryClient()


def get_current_user(request: Request) -> Dict[str, str]:
    session = session_mgr.get_session(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"})
    return session


def render_template_with_context(request: Request, template_name: str, extra_context: dict):
    base_context = {
        "request": request,
        "migration_id": state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"),
        "firm_name": state_store.questionnaire_data.get("firm_name", "New Migration"),
        "adls_connected": state_store.adls_connected,
        "adls_container": state_store.adls_container,
        "kernel_specs": getattr(state_store, "kernel_specs", {})
    }
    base_context.update(extra_context)
    return templates.TemplateResponse(request=request, name=template_name, context=base_context)


# --- AUTHENTICATION ROUTES ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    demo_user = next(iter(session_mgr.authorized_users.values()), None) if session_mgr.demo_mode else None
    demo_email = next(iter(session_mgr.authorized_users.keys()), "") if session_mgr.demo_mode else ""
    return templates.TemplateResponse(request=request, name="login.html", context={
        "error": error,
        "demo_mode": bool(demo_user),
        "demo_email": demo_email,
    })


@app.post("/login")
async def process_login(email: str = Form(...), password: str = Form(...)):
    # Authentication and persona selection are intentionally separate steps.
    default_role = session_mgr.authorized_users.get(email.lower(), {}).get("default_role", "Migration Analyst")
    session_id = session_mgr.authenticate(email, password, default_role)
    if not session_id:
        return RedirectResponse(url="/login?error=Invalid+credentials+provided", status_code=status.HTTP_303_SEE_OTHER)
    response = RedirectResponse(url="/persona-selection", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="session_id", value=session_id, httponly=True, samesite="lax")
    return response


@app.get("/persona-selection", response_class=HTMLResponse)
async def persona_selection_page(request: Request):
    user = get_current_user(request)
    personas = [
        {"id":"Platform Admin","label":"Platform Admin","initials":"PA","plane":"Platform governance",
         "description":"Configure the migration workspace, identity boundaries, environment references and access controls.",
         "stages":["Migration setup","Access & governance","Audit"]},
        {"id":"Migration Analyst","label":"Migration Analyst","initials":"MA","plane":"Control plane",
         "description":"Own source discovery, profiling, entity inference, mapping recommendations, information requests and rule preparation.",
         "stages":["Discovery","Mapping","Rule design"]},
        {"id":"Domain SME","label":"Domain SME / Data Steward","initials":"SME","plane":"Business review",
         "description":"Resolve business meaning, provide missing definitions, and approve or reject mappings and proposed transformations.",
         "stages":["Knowledge","Review","Approve / reject"]},
        {"id":"Validation Specialist","label":"Validation Specialist","initials":"VS","plane":"Validation",
         "description":"Review field completeness, keys, relationships, data-quality exceptions and reconciliation evidence.",
         "stages":["Validation","Exceptions","Evidence"]},
    ]
    assigned_role = session_mgr.authorized_users.get(user["email"].lower(), {}).get("default_role", user["role"])
    allowed_roles = {"Platform Admin", "Migration Analyst", "Domain SME", "Validation Specialist"} if assigned_role == "Platform Admin" else {assigned_role}
    personas = [persona for persona in personas if persona["id"] in allowed_roles]
    response = render_template_with_context(request, "persona_selection.html", {
        "page_title":"Choose Persona", "user_name":user["name"], "user_role":user["role"],
        "selected_role":user["role"], "personas":personas
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.post("/persona-selection")
async def select_persona(request: Request, role: str = Form(...)):
    user = get_current_user(request)
    assigned_role = session_mgr.authorized_users.get(user["email"].lower(), {}).get("default_role", user["role"])
    if assigned_role != "Platform Admin" and role != assigned_role:
        raise HTTPException(status_code=403, detail="The selected persona is not assigned to this user.")
    session_id = request.cookies.get("session_id")
    if not session_id or not session_mgr.select_role(session_id, role):
        raise HTTPException(status_code=403, detail="The selected persona is not available for this session.")
    return RedirectResponse(url="/migration-setup", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/migration-setup", response_class=HTMLResponse)
async def migration_setup_page(request: Request, demo_loaded: str = ""):
    user = get_current_user(request)
    setup = state_store.questionnaire_data.get("migration_setup", {})
    return render_template_with_context(request, "migration_setup.html", {
        "page_title":"Migration Setup", "user_name":user["name"], "user_role":user["role"], "setup":setup,
        "demo_loaded": demo_loaded == "1",
        "demo_available": session_mgr.demo_mode,
        "knowledge_docs": state_store.firm_knowledge_docs,
    })


@app.post("/api/migration/setup")
async def save_migration_setup(request: Request):
    user = get_current_user(request)
    form = await request.form()
    # Explicit scope selection controls downstream P0 workflows.
    allowed_entities = {"Account", "Household", "Client"}
    selected_entities = [entity for entity in form.getlist("entities") if entity in allowed_entities]
    if not selected_entities:
        raise HTTPException(status_code=400, detail="Select at least one in-scope entity: Account, Household, or Client.")
    setup = {
        "migration_name": str(form.get("migration_name", "")).strip(),
        "source_firm": str(form.get("source_firm", "CoreCap")).strip() or "CoreCap",
        "target_system": str(form.get("target_system", "")).strip(),
        "canonical_version": str(form.get("canonical_version", "")).strip(),
        "target_owner": str(form.get("target_owner", "")).strip(),
        "azure_tenant_id": str(form.get("azure_tenant_id", "")).strip(),
        "azure_subscription_id": str(form.get("azure_subscription_id", "")).strip(),
        "azure_resource_group": str(form.get("azure_resource_group", "")).strip(),
        "azure_storage_account": str(form.get("azure_storage_account", "")).strip(),
        "azure_container": str(form.get("azure_container", "")).strip(),
        "azure_access_method": str(form.get("azure_access_method", "PENDING")).strip(),
        "knowledge_schema_ref": str(form.get("knowledge_schema_ref", "")).strip(),
        "knowledge_rules_ref": str(form.get("knowledge_rules_ref", "")).strip(),
        "knowledge_sme": str(form.get("knowledge_sme", "")).strip(),
        "relationship_notes": str(form.get("relationship_notes", "")).strip(),
        "source_acquisition": str(form.get("source_acquisition", "SAMPLE_FILES")).strip(),
        "source_details": str(form.get("source_details", "")).strip(),
        "entities": selected_entities,
        "configured_by": user["name"],
        "configuration_status": "CONFIGURED",
    }
    if not setup["target_system"] or not setup["canonical_version"]:
        raise HTTPException(status_code=400, detail="Target system and canonical schema / contract reference are required before source mapping setup.")
    if setup["source_acquisition"] == "AZURE_CONNECTION":
        required_azure = ["azure_tenant_id", "azure_subscription_id", "azure_resource_group", "azure_storage_account", "azure_container"]
        missing_azure = [field for field in required_azure if not setup.get(field)]
        if missing_azure:
            raise HTTPException(status_code=400, detail="Azure connection mode requires: " + ", ".join(missing_azure) + ".")
    # Configuration metadata only: never store secrets or claim a live Azure connection.
    current_migration_id = str(state_store.questionnaire_data.get("migration_id", ""))
    if not current_migration_id or current_migration_id == "MIG-UNCONFIGURED":
        state_store.questionnaire_data["migration_id"] = f"MIG-{uuid.uuid4().hex[:10].upper()}"
    state_store.update_questionnaire_from_form({
        "firm_name": setup["migration_name"],
        "migration_name": setup["migration_name"],
        "source_systems": setup["source_details"],
        "target_system": setup["target_system"],
        "source_systems": setup["source_details"],
    })
    state_store.questionnaire_data["migration_setup"] = setup
    for entity in {"Account", "Household", "Client"}:
        state_store.entity_scopes[entity] = "PENDING_DISCOVERY" if entity in selected_entities else "OUT_OF_SCOPE"
    state_store.audit_log.append({"event":"MIGRATION_SETUP_SAVED","actor":user["name"],"timestamp_utc":datetime.now(timezone.utc).isoformat(),"details":{"target_system":setup["target_system"],"source_acquisition":setup["source_acquisition"],"entities":selected_entities}})
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/demo/load")
async def load_demo_migration(request: Request):
    """Load the sample migration from editable JSON/CSV/Markdown inputs, not code constants."""
    user = get_current_user(request)
    if not session_mgr.demo_mode:
        raise HTTPException(status_code=404, detail="Demo mode is disabled.")
    manifest_path = Path(root_dir) / "demo_data" / "demo_migration.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Demo migration configuration could not be loaded.") from exc

    setup = dict(manifest.get("migration_setup", {}))
    if not setup.get("target_system") or not setup.get("canonical_version"):
        raise HTTPException(status_code=500, detail="Demo migration configuration is missing target contract details.")

    selected_entities = [entity for entity in setup.get("entities", ["Account", "Household", "Client"]) if entity in {"Account", "Household", "Client"}]
    if not selected_entities:
        raise HTTPException(status_code=500, detail="Demo migration must configure at least one in-scope entity.")
    setup["entities"] = selected_entities
    state_store.questionnaire_data["migration_setup"] = setup
    state_store.questionnaire_data["migration_id"] = f"MIG-{uuid.uuid4().hex[:10].upper()}"
    state_store.update_questionnaire_from_form({
        "firm_name": setup.get("migration_name", ""),
        "migration_name": setup.get("migration_name", ""),
        "target_system": setup.get("target_system", ""),
        "source_systems": setup.get("source_details", ""),
    })
    state_store.entity_sample_files.clear()
    state_store.entity_source_files.clear()
    for entity in {"Account", "Household", "Client"}:
        state_store.entity_scopes[entity] = "PENDING_DISCOVERY" if entity in selected_entities else "OUT_OF_SCOPE"

    loaded_files = []
    for item in manifest.get("source_files", []):
        source_path = (Path(root_dir) / item["path"]).resolve()
        if not source_path.is_relative_to(Path(root_dir).resolve()):
            raise HTTPException(status_code=400, detail="Demo source path is outside the repository.")
        try:
            content = source_path.read_bytes()
            records = read_source_records(source_path.name, content)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"Demo source file could not be read: {source_path.name}") from exc
        headers = list(records[0].keys()) if records else []
        # Use the file's stated purpose for entity classification. Dictionary notes may
        # mention linked entities and should remain mapping evidence, not make a
        # relationship extract ambiguous during entity inference.
        inference = infer_entity_from_headers(
            headers,
            context=str(item.get("context", "")),
        )
        if not inference.get("entity") or inference.get("status") == "NEEDS_CLARIFICATION":
            raise HTTPException(status_code=500, detail=f"Demo source file needs better inference evidence: {source_path.name}")
        entity_name = inference["entity"]
        if entity_name not in selected_entities:
            loaded_files.append({"filename": source_path.name, "inferred_entity": entity_name, "status": "EXCLUDED_OUT_OF_SCOPE"})
            continue
        source_role = "ACCOUNT_MASTER" if entity_name == "Account" else "ENTITY_MASTER"
        record = state_store.attach_entity_sample_file(entity_name, source_path.name, content, source_role=source_role)
        record["inference"] = inference
        record["inference_approval_status"] = "PENDING"
        record["source_context"] = str(item.get("context", ""))
        record["data_dictionary_notes"] = str(item.get("dictionary_notes", ""))
        loaded_files.append({"filename": source_path.name, "inferred_entity": entity_name, "status": record["inference_approval_status"]})


    state_store.audit_log.append({
        "event": "DEMO_MIGRATION_LOADED",
        "actor": user["name"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": {"migration_name": setup.get("migration_name"), "source_files": loaded_files},
    })
    return RedirectResponse(url="/entity-sample-files?notice=" + quote_plus(f"Demo loaded: {len(loaded_files)} source files and {len(manifest.get('knowledge_files', []))} knowledge assets registered. Review each inferred file below."), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/agent-studio", response_class=HTMLResponse)
async def agent_studio(request: Request):
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/agents/analyze/{file_id}")
async def analyze_source_file_with_agents(request: Request, file_id: str):
    user = get_current_user(request)
    setup = state_store.questionnaire_data.get("migration_setup", {})
    selected = [e for e in setup.get("entities", []) if e in {"Account", "Household", "Client"}]
    if not selected:
        raise HTTPException(status_code=400, detail="Select at least one in-scope entity in Migration Setup first.")
    found = None
    for entity, entity_files in state_store.entity_source_files.items():
        for record in entity_files.values():
            if record.get("file_id") == file_id:
                found = (entity, record)
                break
        if found:
            break
    if not found:
        raise HTTPException(status_code=404, detail="Source file was not found.")
    entity, record = found
    try:
        content = Path(record["stored_path"]).read_bytes()
        records = read_source_records(record["filename"], content)
        analysis = MigrationAgentCoordinator().analyze(
            records, context=record.get("source_context", ""),
            in_scope=selected,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="The source file could not be analyzed.") from exc
    analysis["file_id"] = file_id
    analysis["filename"] = record["filename"]
    analysis["analyzed_by"] = user["name"]
    analysis["analyzed_utc"] = datetime.now(timezone.utc).isoformat()
    record["agent_analysis"] = analysis
    state_store.audit_log.append({
        "event": "P0_AGENT_ANALYSIS_COMPLETED", "actor": user["name"],
        "timestamp_utc": analysis["analyzed_utc"],
        "details": {"file_id": file_id, "filename": record["filename"],
                    "scope": selected, "classification_status": analysis["classification"].get("status")},
    })
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        session_mgr.logout(session_id)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("session_id")
    return response


# --- WORKBENCH MODULE ROUTES ---

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def dashboard_overview(request: Request):
    user = get_current_user(request)
    return render_template_with_context(
        request=request,
        template_name="dashboard.html",
        extra_context={
            "page_title": "Migration Dashboard",
            "user_name": user["name"],
            "user_role": user["role"],
            "dashboard": state_store.generated_dashboard,
            "migration_setup": state_store.questionnaire_data.get("migration_setup", {}),
            "knowledge_docs_count": len(state_store.firm_knowledge_docs),
            "in_scope_entities": [entity for entity in ("Account", "Household", "Client") if entity in state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])],
            "wave_conditions": CohortConditionEngine.get_condition_catalog(),
            "entity_approval_summary": {
                entity: {
                    "total": len(state_store.entity_source_files.get(entity, {})),
                    "approved": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") == "APPROVED"),
                    "pending": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") not in {"APPROVED", "REJECTED"}),
                    "rejected": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") == "REJECTED"),
                } for entity in state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])
            }
        }
    )


@app.get("/firm-knowledge", response_class=HTMLResponse)
async def firm_knowledge_studio(request: Request):
    user = get_current_user(request)
    return render_template_with_context(
        request=request,
        template_name="firm_knowledge.html",
        extra_context={
            "page_title": "Knowledge Assets",
            "user_name": user["name"],
            "user_role": user["role"],
            "form": state_store.questionnaire_data,
            "docs": state_store.firm_knowledge_docs
        }
    )


@app.get("/entity-selection", response_class=HTMLResponse)
async def entity_selection_view(request: Request):
    # Entity scope is now part of source discovery and human confirmation, not a separate manual pre-step.
    get_current_user(request)
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/entity-sample-files", response_class=HTMLResponse)
async def entity_sample_files_view(request: Request, uploaded: str = "", notice: str = ""):
    user = get_current_user(request)
    source_uploads = []
    migration_setup = state_store.questionnaire_data.get("migration_setup", {})
    in_scope_entities = set(migration_setup.get("entities", []))
    for entity, files in getattr(state_store, "entity_source_files", {}).items():
        if entity not in in_scope_entities or entity not in {"Account", "Household", "Client"}:
            continue
        for file_id, record in files.items():
            source_uploads.append({**record, "file_id": record.get("file_id", file_id), "inferred_entity": entity})
    source_uploads.sort(key=lambda item: item.get("uploaded_at", ""), reverse=True)
    return render_template_with_context(
        request=request,
        template_name="entity_sample_files.html",
        extra_context={
            "page_title": "Source Discovery & Entity Inference",
            "user_name": user["name"],
            "user_role": user["role"],
            "selected_entities": state_store.get_source_upload_entities(),
            "source_uploads": source_uploads,
            "uploaded_entity": uploaded,
            "notice": notice,
            "acquisition_method": state_store.questionnaire_data.get("migration_setup", {}).get("source_acquisition", "SAMPLE_FILES"),
            "knowledge_docs": state_store.firm_knowledge_docs,
            "entity_approval_summary": {
                entity: {
                    "total": len(state_store.entity_source_files.get(entity, {})),
                    "approved": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") == "APPROVED"),
                    "pending": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") not in {"APPROVED", "REJECTED"}),
                    "rejected": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") == "REJECTED"),
                } for entity in state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])
            },
        }
    )


@app.get("/agent-pipeline", response_class=HTMLResponse)
async def agent_pipeline_view(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    latest_run = state_store.pipeline_runs[-1] if state_store.pipeline_runs else None
    active_migration_id = str(state_store.questionnaire_data.get("migration_id", "UNCONFIGURED"))
    _sync_mapping_information_requests(active_migration_id)
    conn = get_db_connection()
    try:
        selected_scope = [e for e in state_store.questionnaire_data.get("migration_setup", {}).get("entities", []) if e in {"Account", "Household", "Client"}]
        scope_placeholders = ", ".join("?" for _ in selected_scope) or "''"
        rows = conn.execute(
            f"""
            SELECT s.suggestion_id, s.entity_name, s.source_field, s.canonical_field,
                   s.proposed_rule, s.confidence_score
            FROM mapping_suggestions AS s
            WHERE s.migration_id = ? AND s.status = 'PENDING'
              AND s.entity_name IN ({scope_placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM canonical_mappings AS c
                  WHERE c.migration_id = s.migration_id
                    AND c.entity_name = s.entity_name
                    AND c.source_field = s.source_field
                    AND c.canonical_field = s.canonical_field
                    AND c.is_confirmed = 1
              )
            ORDER BY s.entity_name, s.created_utc DESC, s.source_field
            """,
            (active_migration_id, *selected_scope),
        ).fetchall()
        mapping_suggestions = [dict(row) for row in rows]
    finally:
        conn.close()

    source_files = {
        entity: dict(files)
        for entity, files in getattr(state_store, "entity_source_files", {}).items()
        if entity in selected_scope and entity in {"Account", "Household", "Client"}
    }
    for entity, record in getattr(state_store, "entity_sample_files", {}).items():
        if entity in selected_scope and entity in {"Account", "Household", "Client"}:
            source_files.setdefault(entity, {}).setdefault(record.get("source_role", "ENTITY_MASTER"), record)

    return render_template_with_context(
        request=request,
        template_name="agent_pipeline.html",
        extra_context={
            "page_title": "Mapping Studio & Transformation Rules",
            "user_name": user["name"],
            "user_role": user["role"],
            "firm_context": state_store.questionnaire_data,
            "pipeline_run": latest_run,
            "source_files": source_files,
            "mapping_suggestions": mapping_suggestions,
            "information_requests": [item for item in state_store.questionnaire_data.get("mapping_information_requests", []) if item.get("entity") in selected_scope],
            "notice": notice,
            "error": error,
        }
    )


@app.get("/hitl", response_class=HTMLResponse)
async def hitl_approval_queue(request: Request):
    """P0 human approval lives in the source-to-canonical mapping review."""
    get_current_user(request)
    return RedirectResponse(url="/agent-pipeline#pending-mappings", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/reconciliation", response_class=HTMLResponse)
async def reconciliation_dashboard(request: Request):
    user = get_current_user(request)
    migration_id = state_store.questionnaire_data.get("migration_id", "UNCONFIGURED")
    conn = get_db_connection()
    try:
        approved_mappings = [
            dict(row) for row in conn.execute(
                """
                SELECT c.entity_name, c.source_field, c.canonical_field,
                       c.mapping_type, c.transformation_expression, c.confidence_score,
                       c.created_by, c.updated_utc, c.migration_id,
                       s.status AS review_status, s.reviewer, s.review_comments,
                       s.reviewed_utc, s.run_id
                FROM canonical_mappings AS c
                LEFT JOIN mapping_suggestions AS s
                  ON s.migration_id = c.migration_id
                 AND s.entity_name = c.entity_name
                 AND s.source_field = c.source_field
                 AND s.canonical_field = c.canonical_field
                 AND s.status = 'APPROVED'
                WHERE c.migration_id = ? AND c.is_confirmed = 1
                ORDER BY c.entity_name, c.source_field
                """,
                (migration_id,),
            ).fetchall()
        ]
        pending_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM mapping_suggestions AS s
            WHERE s.migration_id = ? AND s.status = 'PENDING'
              AND NOT EXISTS (
                SELECT 1 FROM canonical_mappings AS c
                WHERE c.migration_id = s.migration_id
                  AND c.entity_name = s.entity_name
                  AND c.source_field = s.source_field
                  AND c.canonical_field = s.canonical_field
                  AND c.is_confirmed = 1
              )
            """,
            (migration_id,),
        ).fetchone()["count"]
        mapping_runs = [
            dict(row) for row in conn.execute(
                """
                SELECT run_id, entity_name, source_record_count, confidence_score, status, executed_by, executed_utc
                FROM migration_runs WHERE migration_id = ?
                ORDER BY executed_utc DESC LIMIT 25
                """,
                (migration_id,),
            ).fetchall()
        ]
        mapping_lineage = [
            dict(row) for row in conn.execute(
                """
                SELECT l.entity_name, l.source_column, l.canonical_column, l.applied_rule,
                       l.validation_status, l.actor, l.timestamp_utc, l.run_id
                FROM field_lineage_audit AS l
                JOIN migration_runs AS r ON r.run_id = l.run_id
                WHERE r.migration_id = ?
                ORDER BY l.timestamp_utc DESC LIMIT 100
                """,
                (migration_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    return render_template_with_context(
        request=request,
        template_name="reconciliation.html",
        extra_context={
            "page_title": "Validation & Exception Management",
            "user_name": user["name"],
            "user_role": user["role"],
            "pipeline_runs": state_store.pipeline_runs,
            "drift_gate": state_store.questionnaire_data.get("aum_drift_gate", "0.50%"),
            "approved_mappings": approved_mappings,
            "pending_mapping_count": pending_count,
            "mapping_runs": mapping_runs,
            "mapping_lineage": mapping_lineage,
            "migration_id": migration_id,
        }
    )


# --- PHASE 1 PILOT ENDPOINTS: SOURCE-TO-CANONICAL ACCOUNT ---

@app.post("/api/pilot/account/profile")
async def profile_pilot_account_file(file: UploadFile = File(...)):
    """Inspects uploaded accounts.csv, infers types, null rates, and candidate keys."""
    content = await file.read()
    profile = SourceDataProfiler.profile_csv_content(file.filename, content, entity_name="Account")
    if profile.get("status") != "PROFILED":
        raise HTTPException(status_code=400, detail=profile.get("error_message", "CSV profiling failed."))
    run_id = f"RUN-{uuid.uuid4().hex}"
    profile_id = state_store.persist_source_profile(profile, run_id=run_id)
    profile["run_id"] = run_id
    profile["profile_id"] = profile_id
    return JSONResponse({"status": "SUCCESS", "profile": profile})


@app.post("/api/pilot/account/map")
async def map_pilot_account(records: Optional[List[Dict[str, Any]]] = None):
    """Executes source-to-canonical mapping with confidence scoring and persists metadata lineage."""
    if records is None:
        records = []

    mapping_res = schema_mapper.map_source_to_canonical("Account", records)
    run_id = f"RUN-{uuid.uuid4().hex}"
    state_store.persist_mapping_lineage(mapping_res, run_id=run_id, actor="Platform Admin")
    safe_mapping = _preview_only_mapping(mapping_res)
    return JSONResponse({"status": mapping_res.get("status", "UNKNOWN"), "run_id": run_id, "mapping": safe_mapping})


@app.get("/api/pilot/account/lineage")
async def get_pilot_account_lineage():
    """Retrieves SQLite metadata lineage audit records for the Account pilot."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM field_lineage_audit WHERE entity_name = 'Account' ORDER BY timestamp_utc DESC LIMIT 20;")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return JSONResponse({"status": "SUCCESS", "lineage_records": rows})


# --- PHASE 2 & 3: MIGRATION TELEMETRY & LINEAGE ENDPOINTS ---

@app.get("/api/migration/telemetry")
async def get_migration_telemetry_report():
    """Returns migration-level and entity-level mapping confidence, gap counts, and readiness."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT entity_name, source_record_count, mapped_record_count, confidence_score, status, executed_utc
        FROM migration_runs ORDER BY executed_utc DESC;
    """)
    run_rows = [dict(r) for r in cursor.fetchall()]

    cursor.execute("""
        SELECT entity_name, source_column, canonical_column, applied_rule, validation_status
        FROM field_lineage_audit ORDER BY timestamp_utc DESC;
    """)
    lineage_rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    total_entities = len(run_rows)
    avg_confidence = round(sum(r["confidence_score"] for r in run_rows) / total_entities, 2) if total_entities > 0 else 1.0

    return JSONResponse({
        "status": "SUCCESS",
        "migration_id": state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"),
        "overall_confidence_score": avg_confidence,
        "entity_runs": run_rows,
        "lineage_audit": lineage_rows[:20]
    })


@app.get("/api/waves/conditions")
async def get_wave_cohort_conditions():
    """Returns declarative conditions catalog used for condition-driven cohort actuation."""
    return JSONResponse({
        "status": "SUCCESS",
        "conditions": CohortConditionEngine.get_condition_catalog()
    })

@app.post("/api/waves/evaluate-record")
async def evaluate_record_cohort():
    """Wave evaluation is intentionally deferred until approved ADLS telemetry is available."""
    return JSONResponse(
        {
            "status": "DEFERRED_SCOPE",
            "wave_assignment": "DISABLED",
            "message": "Conditions can be configured in this phase, but records are not evaluated and no waves are assigned.",
        },
        status_code=409,
    )


# --- ADLS & AZURE DATA FACTORY (ADF) ENDPOINTS ---

@app.get("/api/adls/status")
async def get_adls_status():
    """Returns Azure Data Lake Storage Gen2 connection status."""
    return {
        "status": "CONNECTED" if state_store.adls_connected else "DISCONNECTED",
        "account_name": os.getenv("AZURE_STORAGE_ACCOUNT", "stgcorecapdata"),
        "container": state_store.adls_container,
        "hybrid_mode": "ACTIVE"
    }


@app.post("/api/adls/upload")
async def upload_to_adls_container(run_id: str = Form("RUN-001"), subfolder: str = Form("RAW"), file: UploadFile = File(...)):
    """Uploads source extracts directly to ADLS Gen2 container and local fallback storage."""
    content = await file.read()
    rel_path, sha256_hash, size_bytes = HybridStorageManager.save_file(
        run_id=run_id,
        filename=file.filename,
        content=content,
        subfolder=subfolder,
        adls_container=state_store.adls_container
    )
    return JSONResponse({
        "status": "SUCCESS",
        "file_name": file.filename,
        "relative_path": rel_path,
        "sha256": sha256_hash,
        "size_bytes": size_bytes,
        "adls_container": state_store.adls_container
    })


@app.post("/api/adf/trigger")
async def trigger_adf_pipeline():
    """Actual target-side ADF execution is deferred; P0 is source-to-canonical mapping only."""
    return JSONResponse(
        {
            "status": "DEFERRED_SCOPE",
            "mode": "NOT_SUBMITTED",
            "message": "Target-side ADF execution is disabled in P0. Generate and review deployment artifacts separately.",
        },
        status_code=409,
    )


@app.get("/api/adf/status/{adf_run_id}")
async def check_adf_pipeline_status(adf_run_id: str):
    """Queries Azure Data Factory pipeline execution status."""
    status_res = adf_client.get_pipeline_run_status(adf_run_id)
    return JSONResponse(status_res)


@app.get("/api/runs/{run_id}/export-package")
async def export_package(run_id: str, mode: str = "local"):
    """Generates SQL views and exports ADF deployment package to ADLS or local storage."""
    in_scope_list = [e["entity_name"] for e in state_store.get_in_scope_entities()]
    manifest = ADFPackageExporter.export_package(
        run_id=run_id,
        migration_id=state_store.questionnaire_data.get("migration_id", "MIG-CORECAP-2026"),
        in_scope_entities=in_scope_list,
        adls_container=state_store.adls_container
    )
    return JSONResponse(manifest)


@app.get("/api/adf/package/download/{run_id}")
async def download_adf_package_file(run_id: str):
    """Downloads the exported ADF package as a deployable zip bundle."""
    zip_path = Path(f"App_Data/Storage/{run_id}/TRANSFORMED/adf_package_{run_id}.zip")
    if not zip_path.exists():
        in_scope_list = [e["entity_name"] for e in state_store.get_in_scope_entities()]
        ADFPackageExporter.export_package(
            run_id=run_id,
            migration_id=state_store.questionnaire_data.get("migration_id", "MIG-CORECAP-2026"),
            in_scope_entities=in_scope_list,
            adls_container=state_store.adls_container
        )
    if zip_path.exists():
        return FileResponse(
            path=str(zip_path),
            filename=f"adf_package_{run_id}.zip",
            media_type="application/zip"
        )
    raise HTTPException(status_code=404, detail="ADF package bundle not found.")


# --- FORM SUBMISSIONS & API ENDPOINTS ---

@app.post("/api/kernel/update")
async def update_kernel_config(
    request: Request,
    sleeve_invariant: str = Form(...),
    sleeve_tolerance: str = Form(...),
    aum_drift_gate: str = Form(...)
):
    user = get_current_user(request)
    if user["role"] not in ["Platform Admin", "Domain SME"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kernel Configuration edits are strictly restricted to Platform Admin or Domain SME."
        )

    state_store.update_kernel_specs(user["name"], sleeve_invariant, sleeve_tolerance, aum_drift_gate)
    logger.info(f"Wealth Kernel specifications updated by {user['name']} ({user['role']})")
    return RedirectResponse(url="/firm-knowledge", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/knowledge/submit-questionnaire")
async def submit_questionnaire(
    firm_name: str = Form(...),
    source_systems: str = Form(""),
    business_data_owner: str = Form(""),
    mapping_approver: str = Form(""),
    identifier_relationship_notes: str = Form("")
):
    """Save only the discovery context needed for P0 source-to-canonical mapping."""
    form_payload = {
        "firm_name": firm_name.strip(),
        "source_systems": source_systems.strip(),
        "business_data_owner": business_data_owner.strip(),
        "mapping_approver": mapping_approver.strip(),
        "identifier_relationship_notes": identifier_relationship_notes.strip(),
    }
    state_store.update_questionnaire_from_form(form_payload)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    publish_event("MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id, form_payload, "business-data-owner")
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/save-scopes")
async def save_entity_scopes(request: Request):
    form_data = await request.form()
    configured_scope = set(state_store.questionnaire_data.get("migration_setup", {}).get("entities", []))
    scopes_map = {entity: "OUT_OF_SCOPE" for entity in ("Account", "Household", "Client") if entity not in configured_scope}
    for key, value in form_data.items():
        if key.startswith("scope_"):
            entity_name = key.replace("scope_", "")
            if entity_name in configured_scope and entity_name in {"Account", "Household", "Client"}:
                scopes_map[entity_name] = str(value)

    state_store.save_entity_scopes(scopes_map)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    publish_event("ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id, {"scopes": scopes_map}, "migration-analyst")
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/upload-sample-file")
async def upload_entity_sample_file(
    request: Request,
    files: List[UploadFile] = File(...),
    source_role: str = Form("ENTITY_MASTER"),
    source_context: str = Form(""),
    data_dictionary_notes: str = Form(""),
):
    """Batch-inspect files. Optional context refines inference but is never required to attempt it."""
    get_current_user(request)
    configured_scope = state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])
    if not configured_scope:
        raise HTTPException(status_code=400, detail="Select at least one in-scope entity in Migration Setup first.")

    registered, needs_review, excluded, failed = [], [], [], []
    shared_context = " ".join([source_context.strip(), data_dictionary_notes.strip()]).strip()
    for file in files:
        filename = file.filename or "unknown-file"
        try:
            content = await file.read()
            records = read_source_records(filename, content)
            headers = list(records[0].keys()) if records else []
            # Always attempt inference from file evidence first; context is optional enrichment.
            from control_plane.services.entity_classification_agent import classify_source_file
            inference = classify_source_file(headers, records=records, context=shared_context)
        except (ValueError, OSError) as exc:
            failed.append(f"{filename}: {str(exc)}")
            continue

        if inference.get("status") == "NEEDS_CLARIFICATION" or not inference.get("entity"):
            state_store.questionnaire_data.setdefault("source_inference_clarifications", []).append({
                "filename": filename, "headers": headers,
                "source_context": source_context.strip(),
                "data_dictionary_notes": data_dictionary_notes.strip(),
                "inference": inference,
                "information_required": ["Review the candidate entities and evidence.", "Optionally clarify key-field or relationship semantics if still ambiguous."],
                "created_utc": datetime.now(timezone.utc).isoformat(),
            })
            needs_review.append(filename)
            continue

        entity_name = inference["entity"]
        if entity_name not in configured_scope:
            excluded.append(f"{filename} ({entity_name})")
            continue

        normalized_role = "ACCOUNT_MASTER" if entity_name == "Account" else "ENTITY_MASTER"
        try:
            record = state_store.attach_entity_sample_file(entity_name, filename, content, source_role=normalized_role)
            publish_event("SOURCE_FILE_UPLOADED", str(record.get("migration_id", "MIG-UNCONFIGURED")), "source_file", str(record["file_id"]), {**record, "entity": entity_name, "source_role": normalized_role}, "migration-analyst")
        except ValueError as exc:
            failed.append(f"{filename}: {str(exc)}")
            continue
        record["inference"] = inference
        record["inference_approval_status"] = "PENDING"
        record["source_context"] = source_context.strip()
        record["data_dictionary_notes"] = data_dictionary_notes.strip()
        try:
            agent_analysis = MigrationAgentCoordinator().analyze(
                records, context=shared_context, in_scope=configured_scope,
            )
            agent_analysis["file_id"] = record["file_id"]
            agent_analysis["filename"] = record["filename"]
            agent_analysis["analyzed_by"] = "XODUS discovery"
            agent_analysis["analyzed_utc"] = datetime.now(timezone.utc).isoformat()
            record["agent_analysis"] = agent_analysis
        except (ValueError, KeyError, TypeError):
            record["agent_analysis"] = {
                "classification": {"status": "NEEDS_REVIEW", "entity": entity_name, "message": "Automated analysis needs review; entity approval remains pending."},
                "profile": {"row_count": len(records), "column_count": len(headers), "candidate_key_fields": []},
                "mapping": {"proposals": []},
                "review": {"status": "NEEDS_REVIEW", "blockers": ["Agent analysis did not complete; review the source file manually."]},
                "file_id": record["file_id"], "filename": record["filename"],
            }
        state_store.audit_log.append({
            "event": "SOURCE_ENTITY_INFERRED", "actor": "SourceDiscovery",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "details": {"filename": filename, "entity": entity_name,
                        "confidence": inference.get("confidence"), "status": inference.get("status")},
        })
        registered.append(f"{filename} → {entity_name}")

    summary = [f"Batch complete: {len(registered)} classified and registered"]
    if needs_review: summary.append(f"{len(needs_review)} needs review ({', '.join(needs_review)})")
    if excluded: summary.append(f"{len(excluded)} excluded as out of scope ({', '.join(excluded)})")
    if failed: summary.append(f"{len(failed)} failed validation ({'; '.join(failed)})")
    notice = ". ".join(summary) + ". Optional source details and dictionaries can improve ambiguous results, but are not required to attempt inference."
    return RedirectResponse(url="/entity-sample-files?notice=" + quote_plus(notice) + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/confirm-entity")
async def confirm_entity_scope_bulk(request: Request, entity_name: str = Form(...), comments: str = Form("")):
    """Approve all files for an entity in one action only when every file has clear inference evidence."""
    user = get_current_user(request)
    if entity_name not in {"Account", "Household", "Client"}:
        raise HTTPException(status_code=400, detail="Only Account, Household and Client are supported in P0.")
    entity_files = state_store.entity_source_files.get(entity_name, {})
    if not entity_files:
        return RedirectResponse(url="/entity-sample-files?notice=" + quote_plus(f"No source files are registered for {entity_name}.") + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)
    blockers = []
    for record in entity_files.values():
        inference = record.get("inference") or {}
        evidence = inference.get("evidence", {}).get(entity_name, {})
        matched_fields = evidence.get("matched_fields", [])
        signature_fields = evidence.get("signature_fields", [])
        if record.get("inference_approval_status") == "REJECTED" or inference.get("entity") != entity_name or inference.get("status") != "INFERRED" or not matched_fields or not signature_fields:
            blockers.append(record.get("filename", "Unnamed file"))
    if blockers:
        message = "Entity-level approval blocked. Review these files individually first: " + ", ".join(blockers) + "."
        return RedirectResponse(url="/entity-sample-files?notice=" + quote_plus(message) + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)
    now = datetime.now(timezone.utc).isoformat()
    approved_files = []
    for record in entity_files.values():
        record["inference_approval_status"] = "APPROVED"
        record["inference_review"] = {
            "decision": "APPROVE_ENTITY",
            "reviewer": user.get("name", "Unknown reviewer"),
            "comments": comments.strip() or "Entity-level approval: all registered files passed the evidence gate.",
            "reviewed_utc": now,
        }
        approved_files.append(record.get("filename", "source file"))
    state_store.entity_scopes[entity_name] = "IN_SCOPE"
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    publish_event("ENTITY_INFERENCE_REVIEWED", migration_id, "entity", entity_name,
                  {"decision": "APPROVE_ENTITY", "scope": "IN_SCOPE",
                   "files": [{"file_id": item.get("file_id"), "filename": item.get("filename"),
                              "inference_approval_status": item.get("inference_approval_status"),
                              "inference_review": item.get("inference_review")}
                             for item in entity_files.values()],
                   "comments": comments.strip()}, user.get("name", "Unknown reviewer"))
    state_store.audit_log.append({
        "event": "ENTITY_LEVEL_INFERENCE_APPROVED",
        "actor": user.get("name", "Unknown reviewer"),
        "timestamp_utc": now,
        "details": {"entity": entity_name, "files": approved_files, "comments": comments.strip()},
    })
    message = f"Approved {entity_name} entity scope and {len(approved_files)} file(s): " + ", ".join(approved_files)
    return RedirectResponse(url="/entity-sample-files?notice=" + quote_plus(message) + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/confirm-inference")
async def confirm_entity_inference(
    entity_name: str = Form(...),
    decision: str = Form(...),
    comments: str = Form(""),
    request: Request = None,
):
    """Require human confirmation of inferred entity before mapping is enabled."""
    user = get_current_user(request) if request is not None else {"name": "Unknown reviewer"}
    if entity_name not in {"Account", "Household", "Client"}:
        raise HTTPException(status_code=400, detail="Only Account, Household and Client are supported in P0.")
    configured_scope = state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])
    if entity_name not in configured_scope:
        raise HTTPException(status_code=409, detail=f"{entity_name} is not selected in Migration Setup and cannot be approved or mapped.")
    file_id = str((await request.form()).get("file_id", "")).strip() if request is not None else ""
    entity_files = state_store.entity_source_files.get(entity_name, {})
    record = next((item for item in entity_files.values() if item.get("file_id") == file_id), None) if file_id else state_store.entity_sample_files.get(entity_name)
    if not record or not record.get("inference"):
        raise HTTPException(status_code=404, detail=f"No inferred source file is awaiting confirmation for {entity_name}.")
    normalized_decision = decision.strip().upper()
    if normalized_decision not in {"APPROVE", "REJECT"}:
        raise HTTPException(status_code=400, detail="Decision must be APPROVE or REJECT.")
    if normalized_decision == "REJECT" and not comments.strip():
        raise HTTPException(status_code=400, detail="A rejection reason is required.")
    record["inference_approval_status"] = "APPROVED" if normalized_decision == "APPROVE" else "REJECTED"
    record["inference_review"] = {
        "decision": normalized_decision,
        "reviewer": user.get("name", "Unknown reviewer"),
        "comments": comments.strip(),
        "reviewed_utc": datetime.now(timezone.utc).isoformat(),
    }
    approved_for_entity = any(item.get("inference_approval_status") == "APPROVED" for item in entity_files.values())
    state_store.entity_scopes[entity_name] = "IN_SCOPE" if approved_for_entity else "PENDING_DISCOVERY"
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    publish_event("ENTITY_INFERENCE_REVIEWED", migration_id, "source_file", str(record.get("file_id", file_id)),
                  {"entity": entity_name, "filename": record.get("filename"),
                   "decision": normalized_decision, "inference_approval_status": record["inference_approval_status"],
                   "inference": record.get("inference"), "inference_review": record["inference_review"],
                   "scope": state_store.entity_scopes[entity_name]}, user.get("name", "Unknown reviewer"))
    state_store.audit_log.append({
        "event": "ENTITY_INFERENCE_REVIEWED",
        "actor": user.get("name", "Unknown reviewer"),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": {"entity": entity_name, "filename": record.get("filename"), "decision": normalized_decision, "comments": comments.strip()},
    })
    return RedirectResponse(url=f"/entity-sample-files#source-file-{quote_plus(file_id)}", status_code=status.HTTP_303_SEE_OTHER)

def _preview_only_mapping(mapping_res: Dict[str, Any]) -> Dict[str, Any]:
    """Strip row-level canonical output and raw samples from all preview API responses."""
    safe_mapping = dict(mapping_res)
    records = safe_mapping.pop("mapped_records", [])
    safe_mapping["preview_record_count"] = safe_mapping.get("record_count", len(records))
    safe_mapping["mapped_records_withheld"] = True
    safe_mapping["approval_status"] = "PENDING_REVIEW"
    safe_mapping["canonical_output_status"] = "PREVIEW_ONLY_PENDING_APPROVAL"
    safe_mapping["field_lineages"] = [
        {key: value for key, value in lineage.items() if key != "sample_value"}
        for lineage in safe_mapping.get("field_lineages", [])
    ]
    safe_mapping.pop("relationship_candidates", None)
    return safe_mapping


def _get_approved_mapping_rules(migration_id: str, entity_name: str) -> Dict[str, Dict[str, str]]:
    """Return approved target fields and transformation expressions for one migration/entity."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT source_field, canonical_field, transformation_expression FROM canonical_mappings "
            "WHERE migration_id = ? AND entity_name = ? AND is_confirmed = 1",
            (migration_id, entity_name),
        ).fetchall()
        return {
            row["source_field"]: {
                "canonical_field": row["canonical_field"],
                "transformation_expression": (row["transformation_expression"] or "DIRECT_COPY").strip().upper(),
            }
            for row in rows
        }
    finally:
        conn.close()


def _sync_mapping_information_requests(migration_id: str) -> None:
    """Keep information-request status derived from the approved mapping catalog."""
    requests = state_store.questionnaire_data.setdefault("mapping_information_requests", [])
    conn = get_db_connection()
    try:
        # Upsert in-memory requests before reloading the durable projection. The unique
        # migration/entity/field key prevents duplicate open requests across reruns.
        for item in requests:
            item_migration = str(item.get("migration_id", migration_id))
            if not item.get("request_id") or not item.get("entity") or not item.get("canonical_field"):
                continue
            conn.execute(
                """INSERT INTO mapping_information_requests
                (request_id, migration_id, entity_name, canonical_field, source_file, status,
                 question, requested_by, created_utc, resolved_utc, resolution, response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(migration_id, entity_name, canonical_field) DO UPDATE SET
                    source_file = excluded.source_file,
                    question = excluded.question,
                    requested_by = excluded.requested_by,
                    response = CASE WHEN excluded.response != '' THEN excluded.response
                                    ELSE mapping_information_requests.response END""",
                (item["request_id"], item_migration, str(item["entity"]), str(item["canonical_field"]),
                 item.get("source_file"), item.get("status", "OPEN"), item.get("question", ""),
                 item.get("requested_by", ""), item.get("created_utc", datetime.now(timezone.utc).isoformat()),
                 item.get("resolved_utc"), item.get("resolution"), item.get("response", "")),
            )
        approved = {
            (str(row["entity_name"]), str(row["canonical_field"]))
            for row in conn.execute(
                """
                SELECT DISTINCT entity_name, canonical_field
                FROM canonical_mappings
                WHERE migration_id = ? AND is_confirmed = 1
                """,
                (migration_id,),
            ).fetchall()
        }
        # Reload the durable requests so a fresh application process sees the same work.
        persisted_requests = conn.execute(
            """SELECT request_id, migration_id, entity_name, canonical_field, source_file,
                      status, question, requested_by, created_utc, resolved_utc, resolution, response
               FROM mapping_information_requests ORDER BY created_utc, request_id"""
        ).fetchall()
    finally:
        conn.close()

    requests[:] = [{
        "request_id": row["request_id"], "migration_id": row["migration_id"],
        "entity": row["entity_name"], "canonical_field": row["canonical_field"],
        "source_file": row["source_file"], "status": row["status"],
        "question": row["question"], "requested_by": row["requested_by"],
        "created_utc": row["created_utc"], "resolved_utc": row["resolved_utc"],
        "resolution": row["resolution"], "response": row["response"],
    } for row in persisted_requests]

    now = datetime.now(timezone.utc).isoformat()
    for item in requests:
        if str(item.get("migration_id", migration_id)) != migration_id:
            continue
        key = (str(item.get("entity", "")), str(item.get("canonical_field", "")))
        if key in approved:
            if item.get("status") != "RESOLVED":
                item["status"] = "RESOLVED"
                item["resolved_utc"] = now
                item["resolution"] = "Approved source-to-canonical mapping exists."
        elif item.get("status") == "RESOLVED":
            item["status"] = "OPEN"
            item.pop("resolved_utc", None)
            item.pop("resolution", None)

    # Persist the projection after reconciling against the approved mapping catalog.
    conn = get_db_connection()
    try:
        for item in requests:
            conn.execute(
                """UPDATE mapping_information_requests
                   SET status = ?, resolved_utc = ?, resolution = ?, response = ?
                   WHERE request_id = ?""",
                (item.get("status", "OPEN"), item.get("resolved_utc"),
                 item.get("resolution"), item.get("response", ""), item["request_id"]),
            )
        conn.commit()
    finally:
        conn.close()


def _get_approved_mapping_catalog(migration_id: str, entity_name: str) -> Dict[str, str]:
    """Backward-compatible field alias view of explicitly approved rules."""
    return {
        source: rule["canonical_field"]
        for source, rule in _get_approved_mapping_rules(migration_id, entity_name).items()
    }


def _map_uploaded_source_entity(entity_name: str, actor: str = "SourceMappingWorkflow", file_id: str = "") -> Dict[str, Any]:
    """Profile and map the exact persisted source CSV, preserving source identifiers as strings."""
    scope_entity = "Client" if entity_name == "Client" else entity_name
    if scope_entity not in {"Account", "Household", "Client"}:
        raise HTTPException(status_code=400, detail=f"{entity_name} is not part of the P0 MCL workflow.")
    if state_store.entity_scopes.get(scope_entity) != "IN_SCOPE":
        raise HTTPException(status_code=409, detail=f"{entity_name} maps to {scope_entity}, which is not explicitly IN_SCOPE.")
    entity_files = state_store.entity_source_files.get(entity_name, {})
    uploaded = next((item for item in entity_files.values() if item.get("file_id") == file_id), None) if file_id else state_store.entity_sample_files.get(entity_name)
    if uploaded and uploaded.get("inference") and uploaded.get("inference_approval_status") != "APPROVED":
        raise HTTPException(status_code=409, detail=f"Entity inference for {entity_name} must be approved before mapping.")
    if not uploaded or uploaded.get("status") != "UPLOADED" or not uploaded.get("stored_path"):
        raise HTTPException(status_code=409, detail=f"No valid uploaded source file is available for {entity_name}.")
    source_path = Path(uploaded["stored_path"])
    if not source_path.is_file():
        raise HTTPException(status_code=409, detail=f"The uploaded source file for {entity_name} is no longer available; upload it again.")

    run_id = f"RUN-{uuid.uuid4().hex}"
    content = source_path.read_bytes()
    try:
        source_records = read_source_records(uploaded["filename"], content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The current profiler consumes CSV bytes; serialize adapter-normalized records
    # to profile the same logical rows regardless of the original source format.
    import io
    import pandas as pd
    normalized_csv = pd.DataFrame(source_records).to_csv(index=False).encode("utf-8")
    profile = SourceDataProfiler.profile_csv_content(uploaded["filename"], normalized_csv, entity_name=entity_name)
    if profile.get("status") != "PROFILED":
        raise HTTPException(status_code=400, detail=profile.get("error_message", "Source profiling failed."))
    profile["source_format"] = Path(uploaded["filename"]).suffix.lower()
    profile["run_id"] = run_id
    profile["profile_id"] = state_store.persist_source_profile(profile, run_id=run_id)
    # Approved rules are reusable only within the same migration and canonical entity.
    migration_id = state_store.questionnaire_data.get("migration_id", "UNCONFIGURED")
    approved_rules = _get_approved_mapping_rules(migration_id, entity_name)
    approved_catalog = {
        source: rule["canonical_field"] for source, rule in approved_rules.items()
    }
    approved_transformations = {
        source: rule["transformation_expression"] for source, rule in approved_rules.items()
    }
    effective_mapper = SchemaMapperAgent(
        custom_catalog={**schema_mapper.catalog, **approved_catalog},
        approved_transformations=approved_transformations,
    )
    mapping = effective_mapper.map_source_to_canonical(entity_name, source_records)
    mapping["approved_reusable_rules_applied"] = [
        {
            "source_field": source,
            "canonical_field": rule["canonical_field"],
            "transformation_expression": rule["transformation_expression"],
        }
        for source, rule in approved_rules.items()
        if any(source in record for record in source_records)
    ]
    mapping["entity"] = entity_name
    mapping["canonical_entity"] = "Client" if entity_name == "Client" else entity_name
    mapping["run_id"] = run_id
    # Derive information requests from required canonical fields that remain unmapped.
    # These requests are questions for the source owner/SME, never auto-filled defaults.
    mapped_canonical = {
        str(item.get("canonical_field") or item.get("target_field") or "")
        for item in mapping.get("field_lineages", [])
    }
    required_fields = SchemaMapperAgent.MANDATORY_CANONICAL_FIELDS.get(entity_name, [])
    requests = state_store.questionnaire_data.setdefault("mapping_information_requests", [])
    existing_requests = {
        (item.get("entity"), item.get("canonical_field"))
        for item in requests
        if str(item.get("migration_id", migration_id)) == str(migration_id)
    }
    for canonical_field in required_fields:
        if canonical_field not in mapped_canonical and (entity_name, canonical_field) not in existing_requests:
            requests.append({
                "request_id": f"RFI-{uuid.uuid4().hex[:10].upper()}",
                "migration_id": migration_id,
                "entity": entity_name,
                "canonical_field": canonical_field,
                "source_file": uploaded["filename"],
                "status": "OPEN",
                "question": f"Which source field or approved business rule supplies required field {canonical_field}?",
                "requested_by": actor,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "response": "",
            })
    _sync_mapping_information_requests(str(migration_id))
    # Mapper output is a preview; suggestions require human approval before canonical use.
    mapping["approval_status"] = "PENDING_REVIEW" if mapping.get("field_lineages") else "NO_PROPOSALS"
    mapping["information_requests_created"] = [
        item for item in requests if item.get("entity") == entity_name and item.get("source_file") == uploaded["filename"] and item.get("status") == "OPEN"
    ]
    state_store.persist_mapping_lineage(mapping, run_id=run_id, actor=actor)
    publish_event("MAPPING_PROPOSALS_GENERATED", str(migration_id), "mapping_run", str(run_id),
                  {"entity": entity_name, "source_file": uploaded["filename"],
                   "proposal_count": len(mapping.get("field_lineages", [])),
                   "information_request_count": len(mapping.get("information_requests_created", []))}, actor)
    return {
        "status": mapping.get("status", "UNKNOWN"),
        "run_id": run_id,
        "entity_name": entity_name,
        "canonical_entity": mapping["canonical_entity"],
        "source_file": uploaded["filename"],
        "profile": profile,
        "mapping": mapping,
    }


@app.post("/api/entities/map-uploaded-file")
async def map_uploaded_entity_file(entity_name: str = Form(...), request: Request = None):
    """Profile and map an uploaded MCL CSV or another explicitly supported canonical source entity."""
    if entity_name not in {"Account", "Household", "Client"}:
        raise HTTPException(status_code=409, detail="P0 uploaded-file mapping currently supports MCL: Account, Household, and Client.")
    try:
        form = await request.form() if request is not None else {}
        file_id = str(form.get("file_id", "")).strip()
        result = _map_uploaded_source_entity(entity_name, file_id=file_id) if file_id else _map_uploaded_source_entity(entity_name)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Uploaded-file mapping failed for %s", entity_name)
        raise HTTPException(status_code=500, detail=f"Mapping failed; no success was reported: {type(exc).__name__}: {exc}") from exc
    # Keep generated canonical-shaped rows inside the workflow for validation only.
    # The API never exposes them as reusable/approved output.
    if file_id:
        for files in state_store.entity_source_files.values():
            for record in files.values():
                if record.get("file_id") == file_id:
                    record["mapping_generated"] = True
                    record["last_mapping_run_id"] = result.get("run_id")
    mapping_preview = result.get("mapping", {})
    mapping_preview["preview_record_count"] = len(mapping_preview.pop("mapped_records", []))
    mapping_preview["mapped_records_withheld"] = True
    for lineage in mapping_preview.get("field_lineages", []):
        lineage.pop("sample_value", None)
    result["execution_boundary"] = {
        "source_to_canonical_only": True,
        "target_loading": "NOT_RUN",
        "wave_assignment": "DISABLED",
        "canonical_output_status": "PREVIEW_ONLY_PENDING_APPROVAL",
    }
    if request is not None and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/agent-pipeline", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(result)


@app.post("/api/entities/map-approved-files")
async def map_all_approved_source_files(request: Request):
    """Create mapping proposals for every approved source file not mapped yet."""
    user = get_current_user(request)
    approved_files = []
    configured_scope = set(state_store.questionnaire_data.get("migration_setup", {}).get("entities", []))
    for entity_name, files in state_store.entity_source_files.items():
        if entity_name not in configured_scope or entity_name not in {"Account", "Household", "Client"}:
            continue
        for record in files.values():
            if record.get("inference_approval_status") == "APPROVED" and not record.get("mapping_generated"):
                approved_files.append((entity_name, record))
    if not approved_files:
        message = "No new approved files are waiting for profiling. Confirm inferred entities first, or review existing proposals."
        return RedirectResponse(url="/agent-pipeline?notice=" + quote_plus(message) + "#pending-mappings", status_code=status.HTTP_303_SEE_OTHER)
    completed, failures = [], []
    for entity_name, record in approved_files:
        try:
            result = _map_uploaded_source_entity(entity_name, actor=user.get("name", "SourceMappingWorkflow"), file_id=record.get("file_id", ""))
            record["mapping_generated"] = True
            record["last_mapping_run_id"] = result.get("run_id")
            completed.append(record.get("filename", "source file"))
        except HTTPException as exc:
            failures.append(f"{record.get('filename', 'source file')}: {exc.detail}")
        except Exception as exc:
            logger.exception("Batch mapping failed for %s", record.get("filename"))
            failures.append(f"{record.get('filename', 'source file')}: {type(exc).__name__}")
    if completed:
        message = f"Generated mapping proposals for {len(completed)} approved file(s): " + ", ".join(completed)
        if failures:
            message += f". {len(failures)} file(s) need attention: " + " | ".join(failures)
        destination = "/agent-pipeline?notice=" + quote_plus(message) + "#pending-mappings"
    else:
        message = "No mapping proposals generated. " + " | ".join(failures)
        destination = "/entity-sample-files?notice=" + quote_plus(message) + "#discovered-source-inventory"
    state_store.audit_log.append({"event":"BATCH_SOURCE_MAPPING_PROPOSED","actor":user.get("name","Unknown reviewer"),"timestamp_utc":datetime.now(timezone.utc).isoformat(),"details":{"completed_files":completed,"failures":failures}})
    return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/map-mcl")
async def map_mcl_uploaded_files():
    """Run the MCL source-to-canonical flow across Account, Household, and Client."""
    mcl_run_id = f"MCL-{uuid.uuid4().hex}"
    source_entities = ["Account", "Household", "Client"]
    # Validate all prerequisites before starting any profile/mapping work.
    for entity_name in source_entities:
        scope_entity = "Client" if entity_name == "Client" else entity_name
        if state_store.entity_scopes.get(scope_entity) != "IN_SCOPE":
            raise HTTPException(status_code=409, detail=f"MCL run blocked: {scope_entity} is not explicitly IN_SCOPE.")
        uploaded = state_store.entity_sample_files.get(entity_name)
        if not uploaded or uploaded.get("status") != "UPLOADED" or not uploaded.get("stored_path"):
            raise HTTPException(status_code=409, detail=f"MCL run blocked: upload a valid {entity_name} source file first.")
        if not Path(uploaded["stored_path"]).is_file():
            raise HTTPException(status_code=409, detail=f"MCL run blocked: the {entity_name} source file is missing; upload it again.")

    entity_results = {}
    try:
        for entity_name in source_entities:
            entity_results[entity_name] = _map_uploaded_source_entity(entity_name, actor="MCLSourceMappingWorkflow")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("MCL mapping run failed for %s", mcl_run_id)
        raise HTTPException(status_code=500, detail=f"MCL run {mcl_run_id} failed before completion: {type(exc).__name__}: {exc}") from exc

    mappings = {name: result["mapping"] for name, result in entity_results.items()}
    relationship_validation = validate_mcl_relationships(mappings)
    mapping_statuses = {result["mapping"].get("status") for result in entity_results.values()}
    if relationship_validation["status"] == "FAIL" or "WARNING_INCOMPLETE" in mapping_statuses or "NO_SOURCE_RECORDS" in mapping_statuses:
        overall_status = "NEEDS_REMEDIATION"
    elif relationship_validation["status"] == "WARN" or mapping_statuses - {"SUCCESS"}:
        overall_status = "NEEDS_REVIEW_WITH_WARNINGS"
    else:
        overall_status = "NEEDS_REVIEW"

    execution_boundary = {
        "source_to_canonical_only": True,
        "target_loading": "NOT_RUN",
        "wave_assignment": "DISABLED",
        "target_adapter": "NOT_SELECTED_IN_P0",
        "canonical_output_status": "PREVIEW_ONLY_PENDING_APPROVAL",
    }
    try:
        state_store.persist_mcl_execution_summary(
            mcl_run_id=mcl_run_id,
            status=overall_status,
            entity_results=entity_results,
            relationship_validation=relationship_validation,
            execution_boundary=execution_boundary,
        )
        migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
        publish_event("MCL_EXECUTION_SUMMARY_PERSISTED", migration_id, "mcl_run", str(mcl_run_id),
                      {"status": overall_status, "entities": list(entity_results.keys()),
                       "relationship_status": relationship_validation.get("status"),
                       "execution_boundary": execution_boundary}, "migration-analyst")
    except Exception as exc:
        logger.exception("Could not persist MCL execution summary for %s", mcl_run_id)
        raise HTTPException(status_code=500, detail=f"MCL results were produced but batch lineage could not be persisted for {mcl_run_id}: {type(exc).__name__}: {exc}") from exc

    # Relationship checks use in-memory preview rows above. Never return
    # canonical-shaped source records or raw identifier values from this API.
    for result in entity_results.values():
        mapping_preview = result.get("mapping", {})
        mapping_preview["preview_record_count"] = len(mapping_preview.pop("mapped_records", []))
        mapping_preview["mapped_records_withheld"] = True
        mapping_preview.pop("relationship_candidates", None)
        for lineage in mapping_preview.get("field_lineages", []):
            lineage.pop("sample_value", None)
    for check in relationship_validation.get("relationship_checks", []):
        check.pop("orphan_values", None)

    return JSONResponse({
        "status": overall_status,
        "mcl_run_id": mcl_run_id,
        "migration_id": state_store.questionnaire_data.get("migration_id", "UNCONFIGURED"),
        "entities": entity_results,
        "relationship_validation": relationship_validation,
        "execution_boundary": execution_boundary,
    })


@app.post("/api/mapping-gaps/{request_id}/response")
async def respond_to_mapping_information_request(request_id: str, request: Request,
                                                 response_text: str = Form(...)):
    user = get_current_user(request)
    answer = response_text.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="A response is required.")
    requests = state_store.questionnaire_data.setdefault("mapping_information_requests", [])
    item = next((entry for entry in requests if entry.get("request_id") == request_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Information request not found.")
    if item.get("status") != "OPEN":
        raise HTTPException(status_code=409, detail="This information request is already closed.")
    item["response"] = answer
    item["responded_by"] = user["name"]
    item["responded_utc"] = datetime.now(timezone.utc).isoformat()
    item["status"] = "ANSWERED"
    conn = get_db_connection()
    try:
        updated = conn.execute(
            """UPDATE mapping_information_requests
               SET response = ?, status = 'ANSWERED'
               WHERE request_id = ? AND status = 'OPEN'""",
            (answer, request_id),
        )
        if updated.rowcount != 1:
            conn.rollback()
            raise HTTPException(status_code=409, detail="This information request was changed by another workflow action.")
        conn.commit()
    finally:
        conn.close()
    migration_id = str(item.get("migration_id", state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED")))
    publish_event("MAPPING_INFORMATION_REQUEST_CHANGED", migration_id, "information_request", request_id,
                  {"entity": item.get("entity"), "canonical_field": item.get("canonical_field"), "status": "ANSWERED", "response": answer},
                  user["name"])
    state_store.audit_log.append({"event":"MAPPING_INFORMATION_RESPONSE","actor":user["name"],"timestamp_utc":item["responded_utc"],"details":{"request_id":request_id,"entity":item.get("entity"),"canonical_field":item.get("canonical_field")}})
    return RedirectResponse(url="/agent-pipeline", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/workflow/state")
async def get_workflow_state(migration_id: Optional[str] = None):
    """Return durable workflow projections and the event sequence for cross-module refresh."""
    active_id = str(migration_id or state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    return JSONResponse({
        "migration_id": active_id,
        "event_sequence": __import__("control_plane.services.workflow_event_engine", fromlist=["latest_event_sequence"]).latest_event_sequence(active_id),
        "migration": get_projection(active_id, "migration", active_id),
        "entities": {
            entity: get_projection(active_id, "entity", entity)
            for entity in ("Account", "Client", "Household")
        },
    })


@app.post("/api/workflow/replay")
async def replay_workflow_events(migration_id: Optional[str] = Form(None), rebuild: bool = Form(False)):
    """Replay pending events, or rebuild projections from the complete journal."""
    active_id = str(migration_id or state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    replayed = replay_events(active_id, rebuild=rebuild)
    if rebuild:
        state_store.hydrate_from_durable_state()
    return JSONResponse({"migration_id": active_id, "events_replayed": replayed,
                         "projection_rebuilt": rebuild, "status": "SUCCESS"})


@app.get("/api/mapping-suggestions")
async def list_source_mapping_suggestions(status_filter: str = "PENDING"):
    """List proposed source-to-canonical mappings for human review."""
    allowed = {"PENDING", "APPROVED", "REJECTED", "ALL"}
    if status_filter not in allowed:
        raise HTTPException(status_code=400, detail=f"status_filter must be one of {sorted(allowed)}.")
    conn = get_db_connection()
    try:
        if status_filter == "ALL":
            rows = conn.execute(
                """
                SELECT s.*,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM canonical_mappings AS c
                           WHERE c.migration_id = s.migration_id
                             AND c.entity_name = s.entity_name
                             AND c.source_field = s.source_field
                             AND c.canonical_field = s.canonical_field
                             AND c.is_confirmed = 1
                       ) THEN 'APPROVED' ELSE s.status END AS effective_status
                FROM mapping_suggestions AS s
                ORDER BY s.created_utc DESC, s.source_field
                """
            ).fetchall()
        elif status_filter == "PENDING":
            rows = conn.execute(
                """
                SELECT s.* FROM mapping_suggestions AS s
                WHERE s.status = 'PENDING'
                  AND NOT EXISTS (
                      SELECT 1 FROM canonical_mappings AS c
                      WHERE c.migration_id = s.migration_id
                        AND c.entity_name = s.entity_name
                        AND c.source_field = s.source_field
                        AND c.canonical_field = s.canonical_field
                        AND c.is_confirmed = 1
                  )
                ORDER BY s.created_utc DESC, s.source_field
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mapping_suggestions WHERE status = ? ORDER BY created_utc DESC, source_field",
                (status_filter,),
            ).fetchall()
        return JSONResponse({"status": "SUCCESS", "suggestions": [dict(row) for row in rows]})
    finally:
        conn.close()


@app.post("/api/mapping-suggestions/{suggestion_id}/review")
async def review_source_mapping_suggestion(
    suggestion_id: str,
    decision: str = Form(...),
    reviewer: str = Form(...),
    comments: str = Form(""),
    request: Request = None,
):
    """Record an explicit approve/reject decision and prevent repeat review."""
    normalized = decision.strip().upper()
    reviewer_name = reviewer.strip()
    if normalized not in {"APPROVED", "REJECTED"}:
        raise HTTPException(status_code=400, detail="Decision must be APPROVED or REJECTED.")
    if not reviewer_name:
        raise HTTPException(status_code=400, detail="Reviewer is required.")
    if normalized == "REJECTED" and not comments.strip():
        raise HTTPException(status_code=400, detail="A rejection reason is required so the mapping owner knows what to correct.")
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM mapping_suggestions WHERE suggestion_id = ?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Mapping suggestion was not found.")
        if row["status"] != "PENDING":
            raise HTTPException(status_code=409, detail=f"Mapping suggestion is already {row['status']}.")
        proposed_expression = str(row["proposed_rule"] or "DIRECT_COPY").strip().upper()
        if normalized == "APPROVED" and proposed_expression not in SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot approve unsupported transformation '{proposed_expression}'. "
                    "Use one of: " + ", ".join(sorted(SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS))
                ),
            )
        conn.execute(
            "UPDATE mapping_suggestions SET status = ?, reviewer = ?, review_comments = ?, reviewed_utc = ? "
            "WHERE suggestion_id = ? AND status = 'PENDING'",
            (normalized, reviewer_name, comments.strip(), now, suggestion_id),
        )
        # Only an explicit approval creates a reusable, migration-scoped rule.
        # Rejections are auditable but never become confirmed canonical mappings.
        if normalized == "APPROVED" and row["canonical_field"]:
            conn.execute(
                """
                INSERT INTO canonical_mappings
                (mapping_id, migration_id, entity_name, source_field, canonical_field,
                 mapping_type, transformation_expression, confidence_score, is_confirmed,
                 created_by, updated_utc)
                VALUES (?, ?, ?, ?, ?, 'APPROVED_ALIAS', ?, ?, 1, ?, ?)
                ON CONFLICT(mapping_id) DO UPDATE SET
                    canonical_field = excluded.canonical_field,
                    transformation_expression = excluded.transformation_expression,
                    confidence_score = excluded.confidence_score,
                    is_confirmed = 1,
                    created_by = excluded.created_by,
                    updated_utc = excluded.updated_utc
                """,
                (
                    f"MAP-{row['migration_id']}-{row['entity_name']}-{row['source_field']}",
                    row["migration_id"], row["entity_name"], row["source_field"],
                    row["canonical_field"], row["proposed_rule"] or "DIRECT_COPY",
                    float(row["confidence_score"] or 0.0), reviewer_name, now,
                ),
            )
        conn.execute(
            "INSERT INTO approvals (id, run_id, artifact_id, stage, decision, reviewer, comments, created_utc) "
            "VALUES (?, ?, ?, 'SOURCE_TO_CANONICAL_MAPPING', ?, ?, ?, ?)",
            (f"APR-{uuid.uuid4().hex}", row["run_id"], suggestion_id, normalized, reviewer_name, comments.strip(), now),
        )
        updated = conn.execute(
            "SELECT * FROM mapping_suggestions WHERE suggestion_id = ?", (suggestion_id,)
        ).fetchone()
        conn.commit()
        _sync_mapping_information_requests(str(row["migration_id"]))
        publish_event("MAPPING_REVIEWED", str(row["migration_id"]), "mapping_suggestion", suggestion_id, {"entity": row["entity_name"], "source_field": row["source_field"], "canonical_field": row["canonical_field"], "decision": normalized}, reviewer_name)
        if request is not None and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/agent-pipeline", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"status": "SUCCESS", "suggestion": dict(updated)})
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        logger.exception("Mapping suggestion review failed for %s", suggestion_id)
        raise HTTPException(status_code=500, detail="Could not persist mapping review decision.") from exc
    finally:
        conn.close()


@app.post("/api/mapping-suggestions/approve-entity")
async def approve_entity_mapping_suggestions(
    request: Request,
    entity_name: str = Form(...),
    reviewer: str = Form(...),
    comments: str = Form(""),
):
    """Approve all pending mappings for one entity atomically, only when every proposal is valid."""
    user = get_current_user(request)
    reviewer_name = reviewer.strip() or user.get("name", "")
    if entity_name not in {"Account", "Household", "Client"}:
        raise HTTPException(status_code=400, detail="Only Account, Household and Client are supported in P0.")
    if not reviewer_name:
        raise HTTPException(status_code=400, detail="Reviewer is required.")
    migration_id = str(state_store.questionnaire_data.get("migration_id", "UNCONFIGURED"))
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT * FROM mapping_suggestions
            WHERE migration_id = ? AND entity_name = ? AND status = 'PENDING'
              AND NOT EXISTS (
                SELECT 1 FROM canonical_mappings AS c
                WHERE c.migration_id = mapping_suggestions.migration_id
                  AND c.entity_name = mapping_suggestions.entity_name
                  AND c.source_field = mapping_suggestions.source_field
                  AND c.canonical_field = mapping_suggestions.canonical_field
                  AND c.is_confirmed = 1
              )
            ORDER BY source_field, created_utc
            """,
            (migration_id, entity_name),
        ).fetchall()
        if not rows:
            conn.rollback()
            message = f"No pending mapping proposals are waiting for {entity_name} approval."
            return RedirectResponse(url="/agent-pipeline?notice=" + quote_plus(message) + "#pending-mappings", status_code=status.HTTP_303_SEE_OTHER)

        blockers = []
        source_targets = {}
        for row in rows:
            source_field = str(row["source_field"] or "").strip()
            canonical_field = str(row["canonical_field"] or "").strip()
            rule = str(row["proposed_rule"] or "DIRECT_COPY").strip().upper()
            if not source_field or not canonical_field:
                blockers.append(f"{source_field or '(missing source field)'}: unresolved canonical field")
            if rule not in SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS:
                blockers.append(f"{source_field or '(missing source field)'}: unsupported rule {rule}")
            prior_target = source_targets.get(source_field)
            if prior_target and prior_target != canonical_field:
                blockers.append(f"{source_field}: conflicting canonical targets {prior_target} and {canonical_field}")
            source_targets[source_field] = canonical_field
            existing = conn.execute(
                """
                SELECT canonical_field FROM canonical_mappings
                WHERE migration_id = ? AND entity_name = ? AND source_field = ? AND is_confirmed = 1
                LIMIT 1
                """,
                (migration_id, entity_name, source_field),
            ).fetchone()
            if existing and existing["canonical_field"] != canonical_field:
                blockers.append(f"{source_field}: already approved to {existing['canonical_field']}")
        if blockers:
            conn.rollback()
            message = f"Entity-level mapping approval blocked for {entity_name}. Resolve: " + "; ".join(blockers)
            return RedirectResponse(url="/agent-pipeline?notice=" + quote_plus(message) + "#pending-mappings", status_code=status.HTTP_303_SEE_OTHER)

        approved_count = 0
        for row in rows:
            suggestion_id = row["suggestion_id"]
            conn.execute(
                """
                UPDATE mapping_suggestions
                SET status = 'APPROVED', reviewer = ?, review_comments = ?, reviewed_utc = ?
                WHERE suggestion_id = ? AND status = 'PENDING'
                """,
                (reviewer_name, comments.strip() or "Entity-level approval: all pending proposals passed validation.", now, suggestion_id),
            )
            conn.execute(
                """
                INSERT INTO canonical_mappings
                (mapping_id, migration_id, entity_name, source_field, canonical_field,
                 mapping_type, transformation_expression, confidence_score, is_confirmed,
                 created_by, updated_utc)
                VALUES (?, ?, ?, ?, ?, 'APPROVED_ALIAS', ?, ?, 1, ?, ?)
                ON CONFLICT(mapping_id) DO UPDATE SET
                    canonical_field = excluded.canonical_field,
                    transformation_expression = excluded.transformation_expression,
                    confidence_score = excluded.confidence_score,
                    is_confirmed = 1,
                    created_by = excluded.created_by,
                    updated_utc = excluded.updated_utc
                """,
                (
                    f"MAP-{row['migration_id']}-{row['entity_name']}-{row['source_field']}",
                    row["migration_id"], row["entity_name"], row["source_field"],
                    row["canonical_field"], row["proposed_rule"] or "DIRECT_COPY",
                    float(row["confidence_score"] or 0.0), reviewer_name, now,
                ),
            )
            conn.execute(
                """
                INSERT INTO approvals (id, run_id, artifact_id, stage, decision, reviewer, comments, created_utc)
                VALUES (?, ?, ?, 'SOURCE_TO_CANONICAL_MAPPING', 'APPROVED', ?, ?, ?)
                """,
                (f"APR-{uuid.uuid4().hex}", row["run_id"], suggestion_id, reviewer_name, comments.strip() or "Entity-level mapping approval.", now),
            )
            approved_count += 1
        conn.commit()
        _sync_mapping_information_requests(migration_id)
        publish_event("ENTITY_MAPPINGS_APPROVED", migration_id, "entity", entity_name, {"approved_count": approved_count, "comments": comments.strip()}, reviewer_name)
        state_store.audit_log.append({
            "event": "ENTITY_LEVEL_MAPPING_APPROVED",
            "actor": reviewer_name,
            "timestamp_utc": now,
            "details": {"migration_id": migration_id, "entity": entity_name, "approved_count": approved_count, "comments": comments.strip()},
        })
        message = f"Approved {approved_count} mapping proposal(s) for {entity_name} after validating the entire pending set."
        return RedirectResponse(url="/agent-pipeline?notice=" + quote_plus(message) + "#pending-mappings", status_code=status.HTTP_303_SEE_OTHER)
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        logger.exception("Entity-level mapping approval failed for %s", entity_name)
        raise HTTPException(status_code=500, detail="Could not persist entity-level mapping approval.") from exc
    finally:
        conn.close()


@app.get("/api/mcl/runs/{mcl_run_id}")
async def get_mcl_run_lineage(mcl_run_id: str):
    """Retrieve persisted MCL batch summary, entity runs, and field lineage."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM mcl_execution_runs WHERE mcl_run_id = ?",
            (mcl_run_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"MCL run {mcl_run_id} was not found.")
        execution = dict(row)
        entity_summary = json.loads(execution.pop("entity_summary_json"))
        relationship_summary = json.loads(execution.pop("relationship_summary_json"))
        execution_boundary = json.loads(execution.pop("execution_boundary_json"))
        entity_run_ids = [item["run_id"] for item in entity_summary.values() if item.get("run_id")]
        entity_runs = []
        field_lineage = []
        for entity_run_id in entity_run_ids:
            run_row = conn.execute("SELECT * FROM migration_runs WHERE run_id = ?", (entity_run_id,)).fetchone()
            if run_row:
                entity_runs.append(dict(run_row))
            lineage_rows = conn.execute(
                "SELECT * FROM field_lineage_audit WHERE run_id = ? ORDER BY timestamp_utc, source_column",
                (entity_run_id,),
            ).fetchall()
            field_lineage.extend(dict(lineage_row) for lineage_row in lineage_rows)
    finally:
        conn.close()
    return JSONResponse({
        "status": "SUCCESS",
        "execution": execution,
        "entities": entity_summary,
        "relationship_validation": relationship_summary,
        "execution_boundary": execution_boundary,
        "entity_runs": entity_runs,
        "field_lineage": field_lineage,
    })


@app.post("/api/knowledge/file-upload")
async def upload_firm_file(
    category: str = Form(...),
    file: UploadFile = File(...),
    return_to: str = Form("/firm-knowledge"),
):
    filename = file.filename or ""
    allowed_extensions = {".md", ".txt", ".csv", ".tsv", ".json", ".xlsx", ".xlsm", ".xls"}
    if Path(filename).suffix.lower() not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Knowledge assets must be Markdown, text, CSV/TSV, JSON, or Excel files.")
    content = await file.read()
    try:
        doc_record = state_store.add_firm_file(filename, category, content)
        migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
        publish_event("KNOWLEDGE_ASSET_REGISTERED", migration_id, "knowledge_asset", str(doc_record["id"]),
                      doc_record, "knowledge-curator")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    destination = return_to if return_to in {"/migration-setup", "/firm-knowledge"} else "/firm-knowledge"
    return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/pipeline/run")
async def execute_agent_pipeline(request: Request, rep_code: str = Form("REP-9981")):
    """Retain a clear compatibility response for the retired legacy cohort pipeline."""
    get_current_user(request)
    raise HTTPException(
        status_code=409,
        detail=(
            "The legacy cohort pipeline is retired from P0. Use Source Discovery, "
            "Mapping Studio, and Mapping Review for Account, Household, and Client. "
            "No cohort execution, target loading, or wave assignment is available."
        ),
    )


@app.get("/api/pipeline/agent-detail/{agent_id}")
async def get_agent_detail(agent_id: str, request: Request):
    user = get_current_user(request)
    detail = state_store.get_agent_detail(agent_id)
    return JSONResponse({"status": "SUCCESS", "agent_id": agent_id, "detail": detail})


@app.post("/api/pipeline/override-agent")
async def override_agent_logic(
    agent_id: str = Form(...),
    new_logic: str = Form(...),
    override_notes: str = Form(...)
):
    res = state_store.apply_agent_override(agent_id, new_logic, override_notes)
    logger.info(f"Human intervention override applied to Agent '{agent_id}': {override_notes}")
    return JSONResponse(res)


@app.post("/api/hitl/approve")
async def approve_cohort(request: Request):
    """Cohort cutover approval is explicitly deferred from the P0 mapping pilot."""
    get_current_user(request)
    raise HTTPException(status_code=409, detail="Cohort/cutover approval is deferred in P0. Review source-to-canonical mappings in Mapping Studio.")


@app.post("/api/hitl/reject")
async def reject_cohort(request: Request):
    """Cohort rejection is explicitly deferred from the P0 mapping pilot."""
    get_current_user(request)
    raise HTTPException(status_code=409, detail="Cohort/cutover review is deferred in P0. Reject mapping proposals in Mapping Studio with a reason.")


@app.post("/api/pilot/household/profile")
async def profile_pilot_household_file(file: UploadFile = File(...)):
    """Inspects uploaded households.csv, infers types, null rates, and candidate keys."""
    content = await file.read()
    profile = SourceDataProfiler.profile_csv_content(file.filename, content, entity_name="Household")
    if profile.get("status") != "PROFILED":
        raise HTTPException(status_code=400, detail=profile.get("error_message", "CSV profiling failed."))
    run_id = f"RUN-{uuid.uuid4().hex}"
    profile_id = state_store.persist_source_profile(profile, run_id=run_id)
    profile["run_id"] = run_id
    profile["profile_id"] = profile_id
    return JSONResponse({"status": "SUCCESS", "profile": profile})


@app.post("/api/pilot/household/map")
async def map_pilot_household(records: Optional[List[Dict[str, Any]]] = None):
    """Executes source-to-canonical mapping for Household and persists lineage."""
    if records is None:
        records = []

    mapping_res = schema_mapper.map_source_to_canonical("Household", records)
    run_id = f"RUN-{uuid.uuid4().hex}"
    state_store.persist_mapping_lineage(mapping_res, run_id=run_id, actor="Platform Admin")
    safe_mapping = _preview_only_mapping(mapping_res)
    return JSONResponse({"status": mapping_res.get("status", "UNKNOWN"), "run_id": run_id, "mapping": safe_mapping})


@app.get("/api/pilot/household/lineage")
async def get_pilot_household_lineage():
    """Retrieves SQLite metadata lineage audit records for Household."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM field_lineage_audit WHERE entity_name = 'Household' ORDER BY timestamp_utc DESC LIMIT 20;")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return JSONResponse({"status": "SUCCESS", "lineage_records": rows})


# --- CLIENT ENDPOINTS ---

@app.post("/api/pilot/client/profile")
async def profile_pilot_client_file(file: UploadFile = File(...)):
    """Inspects uploaded clients.csv, infers types, null rates, and candidate keys."""
    content = await file.read()
    profile = SourceDataProfiler.profile_csv_content(file.filename, content, entity_name="Client")
    if profile.get("status") != "PROFILED":
        raise HTTPException(status_code=400, detail=profile.get("error_message", "CSV profiling failed."))
    run_id = f"RUN-{uuid.uuid4().hex}"
    profile_id = state_store.persist_source_profile(profile, run_id=run_id)
    profile["run_id"] = run_id
    profile["profile_id"] = profile_id
    return JSONResponse({"status": "SUCCESS", "profile": profile})


@app.post("/api/pilot/client/map")
async def map_pilot_client(records: Optional[List[Dict[str, Any]]] = None):
    """Executes source-to-canonical mapping for Client and persists lineage."""
    if records is None:
        records = []

    mapping_res = schema_mapper.map_source_to_canonical("Client", records)
    run_id = f"RUN-{uuid.uuid4().hex}"
    state_store.persist_mapping_lineage(mapping_res, run_id=run_id, actor="Platform Admin")
    safe_mapping = _preview_only_mapping(mapping_res)
    return JSONResponse({"status": mapping_res.get("status", "UNKNOWN"), "run_id": run_id, "mapping": safe_mapping})


@app.get("/api/pilot/client/lineage")
async def get_pilot_client_lineage():
    """Retrieves SQLite metadata lineage audit records for Client."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM field_lineage_audit WHERE entity_name = 'Client' ORDER BY timestamp_utc DESC LIMIT 20;")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return JSONResponse({"status": "SUCCESS", "lineage_records": rows})