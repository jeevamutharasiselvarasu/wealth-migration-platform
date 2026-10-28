import os
import uuid
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, urlparse
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ui.auth import session_mgr
from ui.work_queue import build_queue
from ui.role_policy import DEFINITION_ROLES, TARGET_PREVIEW_ROLES, VALIDATION_ROLES, APPROVAL_ROLES, CANONICAL_ROLES, CHANGE_REQUEST_ROLES, DISCOVERY_ROLES, PLATFORM_ROLES, SETUP_ROLES, allowed_roles_for
from ui.state import state_store
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.services import activity_feed, canonical_overlay, migration_validation, simulated_target, target_mapping, transformation_rules
from control_plane.services.value_evidence import EVIDENCE_REVIEW_THRESHOLD
from data_plane.storage_manager import HybridStorageManager
from control_plane.services.profiler import SourceDataProfiler
from control_plane.services.mcl_validation import validate_mcl_relationships
from control_plane.services.source_file_adapter import read_source_records
from control_plane.services.wave_rules import CohortConditionEngine, WaveConditionEvaluator
from control_plane.services.adf_client import AzureDataFactoryClient
from control_plane.services.adf_exporter import ADFPackageExporter
from control_plane.db import get_db_connection
from control_plane.services.firm_config import FirmConfigError, load_firm_config
from control_plane.services.workflow_event_engine import publish_event, read_events, get_projection, replay_events
from agents.p0_agent_suite import MigrationAgentCoordinator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataMigrationWorkbench")

app = FastAPI(title="Data Migration Workbench", version="3.5.0")


@app.middleware("http")
async def enforce_role_policy(request: Request, call_next):
    """Reject state-changing requests from roles that may not perform them (session role, server side)."""
    allowed = allowed_roles_for(request.method, request.url.path)
    if allowed is not None:
        session = session_mgr.get_session(request)
        # Unauthenticated callers fall through to the route's own login redirect.
        if session is not None and session.get("role") not in allowed:
            detail = f"{session.get('role')} may not perform this action. Allowed: {', '.join(sorted(allowed))}."
            if "text/html" in request.headers.get("accept", ""):
                path = urlparse(request.headers.get("referer", "/")).path or "/"
                return RedirectResponse(url=f"{path}?error=" + quote_plus(detail), status_code=status.HTTP_303_SEE_OTHER)
            return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)
    return await call_next(request)

ui_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(ui_dir, ".."))

static_dir = os.path.join(ui_dir, "static")
templates_dir = os.path.join(ui_dir, "templates")

os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)
templates.env.filters["mapping_kind"] = lambda rule, field=None: SchemaMapperAgent.mapping_kind(rule, field)

schema_mapper = SchemaMapperAgent()
adf_client = AzureDataFactoryClient()


def get_current_user(request: Request) -> Dict[str, str]:
    session = session_mgr.get_session(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"})
    return session


def _can_load_demo(user: Dict[str, str]) -> bool:
    return user.get("role") in PLATFORM_ROLES and session_mgr.can_load_demo(user.get("email", ""))


def _workflow_safe_source_payload(record: Dict[str, Any], entity_name: str, source_role: str) -> Dict[str, Any]:
    """Persist source metadata and evidence without duplicating sampled client values in the event log."""
    payload = {**record, "entity": entity_name, "source_role": source_role}

    def remove_samples(value):
        if isinstance(value, dict):
            return {
                key: remove_samples(item)
                for key, item in value.items()
                if key not in {"sample_value", "sample_values"}
            }
        if isinstance(value, list):
            return [remove_samples(item) for item in value]
        return value

    return remove_samples(payload)


def workflow_progress() -> dict:
    """Counts and the single next action shown in the navigation and dashboard."""
    setup = state_store.questionnaire_data.get("migration_setup", {})
    scope = [e for e in setup.get("entities", []) if e in {"Account", "Household", "Client"}]
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    files = [rec for entity, group in getattr(state_store, "entity_source_files", {}).items() for rec in group.values()]
    files_total = len(files)
    files_confirmed = sum(1 for r in files if r.get("inference_approval_status") == "APPROVED")
    files_pending = sum(1 for r in files if r.get("inference_approval_status") not in {"APPROVED", "REJECTED"})
    proposals_pending = mappings_approved = changes_open = rfi_open = target_pending = 0
    target_provisioned = False
    validation_status = validation_when = ""
    conn = get_db_connection()
    try:
        proposals_pending = conn.execute(
            "SELECT COUNT(*) FROM mapping_suggestions WHERE migration_id = ? AND status = 'PENDING'", (migration_id,)).fetchone()[0]
        mappings_approved = conn.execute(
            "SELECT COUNT(*) FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchone()[0]
        if scope:
            marks = ",".join("?" for _ in scope)
            rfi_open = conn.execute(
                f"SELECT COUNT(*) FROM mapping_information_requests WHERE migration_id = ? AND status != 'RESOLVED' AND entity_name IN ({marks})",
                (migration_id, *scope)).fetchone()[0]
        changes_open = conn.execute(
            "SELECT COUNT(*) FROM canonical_change_requests WHERE migration_id = ? AND status IN ('OPEN','DEFERRED')", (migration_id,)).fetchone()[0]
        target_provisioned = simulated_target.snapshot(conn) is not None
        target_pending = len([m for m in target_mapping.list_mappings(conn, migration_id) if m["status"] == "PROPOSED" and m["canonical_field"]])
        last_val = conn.execute(
            "SELECT status, executed_utc FROM validation_runs WHERE migration_id = ? ORDER BY executed_utc DESC LIMIT 1", (migration_id,)).fetchone()
        if last_val:
            validation_status, validation_when = last_val["status"], str(last_val["executed_utc"])[:10]
    except Exception:
        logger.exception("Could not read workflow progress counts")
    finally:
        conn.close()
    if not scope:
        nxt = {"label": "Set up the migration", "detail": "Choose the entities in scope and register knowledge.", "href": "/migration-setup", "step": "setup"}
    elif files_total == 0:
        nxt = {"label": "Add source files", "detail": "Upload CoreCap files or load the demo data set.", "href": "/entity-sample-files", "step": "discover"}
    elif files_pending:
        nxt = {"label": f"Confirm {files_pending} source file{'s' if files_pending != 1 else ''}", "detail": "Review the inferred entity for each file.", "href": "/entity-sample-files", "step": "discover"}
    elif proposals_pending:
        nxt = {"label": f"Review {proposals_pending} mapping proposal{'s' if proposals_pending != 1 else ''}", "detail": "Approve or reject the suggested source-to-canonical mappings.", "href": "/agent-pipeline", "step": "map"}
    elif mappings_approved == 0:
        nxt = {"label": "Generate mapping proposals", "detail": "Profile the confirmed files and create review items.", "href": "/agent-pipeline", "step": "map"}
    elif rfi_open:
        nxt = {"label": f"Answer {rfi_open} information request{'s' if rfi_open != 1 else ''}", "detail": "Required canonical fields still have no source.", "href": "/agent-pipeline#information-requests", "step": "map"}
    else:
        nxt = {"label": "Review validation results", "detail": "All mappings are approved; check keys, relationships and exceptions.", "href": "/reconciliation", "step": "validate"}
    progress = {
        "scope": scope, "files_total": files_total, "files_confirmed": files_confirmed, "files_pending": files_pending,
        "proposals_pending": proposals_pending, "mappings_approved": mappings_approved, "rfi_open": rfi_open,
        "changes_open": changes_open, "next": nxt, "target_provisioned": target_provisioned, "target_pending": target_pending,
        "validation_status": validation_status, "validation_when": validation_when,
    }
    progress.update(build_workflow_view(progress, setup, migration_id))
    progress["activity"] = activity_feed.recent_activity(migration_id)
    return progress


def build_workflow_view(p: dict, setup: dict, migration_id: str) -> dict:
    """Stepper, per-step sub-tasks, header facts and status chip derived from the progress counts."""
    def task(label, state, detail=""):
        return {"label": label, "state": state, "detail": detail}

    configured = bool(p["scope"])
    discover_done = p["files_total"] > 0 and p["files_pending"] == 0
    map_done = p["mappings_approved"] > 0 and p["proposals_pending"] == 0 and p["rfi_open"] == 0
    validate_done = p["validation_status"] == "PASS"
    tasks = {
        "setup": [
            task("Migration configured", "complete" if configured else "needs_you",
                 ", ".join(p["scope"]) if configured else "Choose the entities in scope."),
            task("Firm contract registered", "complete" if setup.get("firm_config_version") else "not_started",
                 str(setup.get("firm_config_version", ""))),
        ],
        "discover": [
            task("Source files added", "complete" if p["files_total"] else "needs_you", f"{p['files_total']} file(s)"),
            task("Entities confirmed", "complete" if discover_done else ("needs_you" if p["files_pending"] else "not_started"),
                 f"{p['files_confirmed']} of {p['files_total']} confirmed"),
        ],
        "map": [
            task("Mapping proposals reviewed", "complete" if (p["mappings_approved"] and not p["proposals_pending"]) else
                 ("needs_you" if p["proposals_pending"] else "not_started"), f"{p['proposals_pending']} waiting"),
            task("Mappings approved", "complete" if p["mappings_approved"] else "not_started", f"{p['mappings_approved']} approved"),
            task("Information requests answered", "complete" if not p["rfi_open"] else "needs_you", f"{p['rfi_open']} open"),
        ],
        "validate": [
            task("Validation checks run", "complete" if p["validation_status"] else "not_started",
                 f"{p['validation_status']} · {p['validation_when']}" if p["validation_status"] else "Not run yet"),
            task("Canonical change requests", "needs_you" if p["changes_open"] else "complete", f"{p['changes_open']} open or deferred"),
        ],
    }
    current = p["next"]["step"]
    done = {"setup": configured, "discover": discover_done, "map": map_done, "validate": validate_done}
    captions = {
        "setup": "Configured" if configured else "Not configured",
        "discover": f"{p['files_confirmed']}/{p['files_total']} files",
        "map": f"{p['mappings_approved']} approved",
        "validate": p["validation_status"].title() if p["validation_status"] else "Not run",
    }
    hrefs = {"setup": "/migration-setup", "discover": "/entity-sample-files", "map": "/agent-pipeline", "validate": "/reconciliation"}
    titles = {"setup": "Setup", "discover": "Discover", "map": "Map & review", "validate": "Validate"}
    steps = []
    for number, key in enumerate(("setup", "discover", "map", "validate"), start=1):
        state = "current" if key == current else ("done" if done[key] else "todo")
        steps.append({"key": key, "number": number, "title": titles[key], "href": hrefs[key], "state": state, "caption": captions[key]})
    if not configured:
        chip = ("Setup needed", "attention")
    elif validate_done and discover_done and map_done:
        chip = ("Validated", "done")
    elif current == "validate":
        chip = ("Ready to validate", "info")
    else:
        chip = (f"In progress · {titles[current]}", "info")
    facts = {
        "name": setup.get("migration_name") or state_store.questionnaire_data.get("migration_name") or "New migration",
        "route": f"{setup.get('source_firm', 'Source')} → {setup.get('target_system', 'Target')}" if configured else "Not configured",
        "entities": ", ".join(p["scope"]) if configured else "None selected",
        "contract": str(setup.get("canonical_version", "—")),
        "migration_id": migration_id,
    }
    return {"steps": steps, "step_tasks": tasks, "current_step": current, "status_chip": {"label": chip[0], "tone": chip[1]}, "facts": facts}


def render_template_with_context(request: Request, template_name: str, extra_context: dict):
    base_context = {
        "request": request,
        "migration_id": state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"),
        "firm_name": state_store.questionnaire_data.get("firm_name", "New Migration"),
        "adls_connected": state_store.adls_connected,
        "adls_container": state_store.adls_container,
        "kernel_specs": getattr(state_store, "kernel_specs", {}),
        "progress": workflow_progress(),
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
         "description":"Maintain the platform: firm configuration, canonical model, users and access, demo data. Holds every capability.",
         "stages":["Migration setup","Access & governance","Audit"]},
        {"id":"Migration Analyst","label":"Migration Analyst","initials":"MA","plane":"Control plane",
         "description":"Set up the migration, upload and confirm sample files, generate mappings, approve or reject mappings and transformation rules, and answer information requests.",
         "stages":["Discovery","Mapping","Rule design"]},
        {"id":"Domain SME","label":"Domain SME / Data Steward","initials":"SME","plane":"Business review",
         "description":"Own the canonical model: handle requested changes to canonical definitions and the wealth kernel, and resolve canonical meaning. Also holds the Analyst and Validation personas.",
         "stages":["Canonical model","Definitions","Kernel"]},
        {"id":"Validation Specialist","label":"Validation Specialist","initials":"VS","plane":"Validation",
         "description":"Review field completeness, keys, relationships, data-quality exceptions and reconciliation evidence.",
         "stages":["Validation","Exceptions","Evidence"]},
    ]
    allowed_roles = session_mgr.assigned_roles(user["email"]) or {user["role"]}
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
    if role not in (session_mgr.assigned_roles(user["email"]) or {user["role"]}):
        raise HTTPException(status_code=403, detail="The selected persona is not assigned to this user.")
    session_id = request.cookies.get("session_id")
    if not session_id or not session_mgr.select_role(session_id, role):
        raise HTTPException(status_code=403, detail="The selected persona is not available for this session.")
    return RedirectResponse(url="/migration-setup", status_code=status.HTTP_303_SEE_OTHER)


def _target_connection_status() -> Dict[str, Any]:
    """Setup-page view of the target: this POC has no live connection, only the one-time simulated system."""
    conn = get_db_connection()
    try:
        snap = simulated_target.snapshot(conn)
        conn.commit()
    finally:
        conn.close()
    return {"snapshot": snap, "spec": {k: v for k, v in target_mapping.load_spec().items() if k != "entities"}}


@app.get("/migration-setup", response_class=HTMLResponse)
async def migration_setup_page(request: Request, demo_loaded: str = "", error: str = "", new_migration: str = ""):
    user = get_current_user(request)
    setup = state_store.questionnaire_data.get("migration_setup", {})
    return render_template_with_context(request, "migration_setup.html", {
        "page_title":"Migration Setup", "user_name":user["name"], "user_role":user["role"], "setup":setup,
        "error": error, "can_configure": user["role"] in SETUP_ROLES, "can_load_demo": _can_load_demo(user),
        "target_connection": _target_connection_status(),
        "demo_loaded": demo_loaded == "1", "new_migration": new_migration == "1",
        "demo_available": session_mgr.demo_mode,
        "knowledge_docs": state_store.firm_knowledge_docs,
    })


@app.post("/api/migration/new")
async def start_new_migration(request: Request):
    """Begin a clean migration. Earlier migrations stay stored but isolated by migration_id;
    firm knowledge assets are kept because they are platform knowledge, not migration data."""
    user = get_current_user(request)
    previous_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    migration_id = f"MIG-{uuid.uuid4().hex[:10].upper()}"
    state_store.entity_sample_files.clear()
    state_store.entity_source_files.clear()
    state_store.pipeline_runs.clear()
    state_store.questionnaire_data["mapping_information_requests"] = []
    state_store.questionnaire_data["migration_setup"] = {}
    state_store.questionnaire_data["migration_id"] = migration_id
    for entity in ("Account", "Household", "Client"):
        state_store.entity_scopes[entity] = "OUT_OF_SCOPE"
    context_payload = {"migration_id": migration_id, "migration_setup": {}, "previous_migration_id": previous_id}
    publish_event("MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id, context_payload, user["name"])
    publish_event(
        "ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id,
        {"scopes": {entity: "OUT_OF_SCOPE" for entity in ("Account", "Household", "Client")}}, user["name"],
    )
    return RedirectResponse(url="/migration-setup?new_migration=1", status_code=status.HTTP_303_SEE_OTHER)


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
    # Target-first: a migration must reference the configured firm contract.
    try:
        firm_config = load_firm_config()
    except FirmConfigError as exc:
        raise HTTPException(status_code=409, detail=f"Firm configuration is required before migration setup. {exc}")
    if setup["canonical_version"] != firm_config.canonical_contract_ref:
        raise HTTPException(status_code=400, detail=f"Canonical contract reference must match the configured firm contract ({firm_config.canonical_contract_ref}).")
    setup["firm_id"] = firm_config.firm_id
    setup["firm_config_version"] = firm_config.version_id
    if setup["source_acquisition"] == "AZURE_CONNECTION":
        required_azure = ["azure_tenant_id", "azure_subscription_id", "azure_resource_group", "azure_storage_account", "azure_container"]
        missing_azure = [field for field in required_azure if not setup.get(field)]
        if missing_azure:
            raise HTTPException(status_code=400, detail="Azure connection mode requires: " + ", ".join(missing_azure) + ".")
    # Configuration metadata only: never store secrets or claim a live Azure connection.
    current_migration_id = str(state_store.questionnaire_data.get("migration_id", ""))
    if not current_migration_id or current_migration_id == "MIG-UNCONFIGURED":
        state_store.questionnaire_data["migration_id"] = f"MIG-{uuid.uuid4().hex[:10].upper()}"
    migration_id = str(state_store.questionnaire_data["migration_id"])
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
    context_payload = {
        "migration_id": migration_id,
        "firm_name": setup["migration_name"],
        "migration_name": setup["migration_name"],
        "source_systems": setup["source_details"],
        "target_system": setup["target_system"],
        "migration_setup": setup,
    }
    publish_event("MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id, context_payload, user["name"])
    publish_event(
        "ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id,
        {"scopes": {entity: state_store.entity_scopes[entity] for entity in ("Account", "Household", "Client")}},
        user["name"],
    )
    state_store.audit_log.append({"event":"MIGRATION_SETUP_SAVED","actor":user["name"],"timestamp_utc":datetime.now(timezone.utc).isoformat(),"details":{"target_system":setup["target_system"],"source_acquisition":setup["source_acquisition"],"entities":selected_entities}})
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/demo/load")
async def load_demo_migration(request: Request):
    """Load the sample migration from editable JSON/CSV/Markdown inputs, not code constants."""
    user = get_current_user(request)
    if not session_mgr.demo_mode:
        raise HTTPException(status_code=404, detail="Demo mode is disabled.")
    if not _can_load_demo(user):
        raise HTTPException(status_code=403, detail="The one-click demo load is not enabled for this account.")
    manifest_path = Path(root_dir) / "demo_data" / "demo_migration.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Demo migration configuration could not be loaded.") from exc

    setup = dict(manifest.get("migration_setup", {}))
    if not setup.get("target_system") or not setup.get("canonical_version"):
        raise HTTPException(status_code=500, detail="Demo migration configuration is missing target contract details.")
    # Same gate as /api/migration/setup: no migration without a firm configuration.
    try:
        firm_config = load_firm_config()
    except FirmConfigError as exc:
        raise HTTPException(status_code=409, detail=f"Firm configuration is required before migration setup. {exc}")
    if setup["canonical_version"] != firm_config.canonical_contract_ref:
        raise HTTPException(status_code=400, detail=f"Canonical contract reference must match the configured firm contract ({firm_config.canonical_contract_ref}).")
    setup["firm_id"] = firm_config.firm_id
    setup["firm_config_version"] = firm_config.version_id

    selected_entities = [entity for entity in setup.get("entities", ["Account", "Household", "Client"]) if entity in {"Account", "Household", "Client"}]
    if not selected_entities:
        raise HTTPException(status_code=500, detail="Demo migration must configure at least one in-scope entity.")
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
    for entity in {"Account", "Household", "Client"}:
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
        {"scopes": {entity: state_store.entity_scopes[entity] for entity in ("Account", "Household", "Client")}},
        user["name"],
    )

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
        source_context = str(item.get("context", ""))
        dictionary_notes = str(item.get("dictionary_notes", ""))
        from control_plane.services.entity_classification_agent import classify_source_file
        inference = classify_source_file(
            headers, records=records,
            context=" ".join(part for part in (source_context, dictionary_notes) if part).strip(),
        )
        entity_name = inference.get("entity")
        if inference.get("status") == "NEEDS_CLARIFICATION" or entity_name not in {"Account", "Household", "Client"}:
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
    ]
    registered_knowledge = []
    for item in knowledge_items:
        relative_path = str(item.get("path", "")).strip()
        if not relative_path:
            continue
        knowledge_path = (Path(root_dir) / relative_path).resolve()
        if not knowledge_path.is_relative_to(Path(root_dir).resolve()):
            raise HTTPException(status_code=400, detail="Demo knowledge path is outside the repository.")
        try:
            knowledge_content = knowledge_path.read_bytes()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Demo knowledge asset could not be read: {knowledge_path.name}") from exc
        try:
            doc = state_store.add_firm_file(knowledge_path.name, str(item.get("category", "mapping_reference")), knowledge_content)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=f"Demo knowledge asset could not be registered: {knowledge_path.name}") from exc
        doc["source_path"] = relative_path
        publish_event("KNOWLEDGE_ASSET_REGISTERED", migration_id, "knowledge_asset", doc["id"], doc, user["name"])
        registered_knowledge.append(doc)

    target_note = ""
    conn = get_db_connection()
    try:
        if simulated_target.snapshot(conn) is None:
            snap = simulated_target.provision(conn, user["name"])
            target_note = f" Simulated target system {snap['snapshot_id']} provisioned."
            state_store.audit_log.append({"event": "TARGET_SYSTEM_PROVISIONED", "actor": user["name"],
                                          "timestamp_utc": snap["provisioned_utc"], "details": {"snapshot_id": snap["snapshot_id"], "via": "DEMO_LOAD"}})
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({
        "event": "DEMO_MIGRATION_LOADED",
        "actor": user["name"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": {"migration_name": setup.get("migration_name"), "source_files": loaded_files},
    })
    return RedirectResponse(url="/?notice=" + quote_plus(f"Demo loaded: {len(loaded_files)} source files and {len(registered_knowledge)} knowledge assets registered.{target_note} Continue to source discovery to review each inferred file."), status_code=status.HTTP_303_SEE_OTHER)


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
            "my_queue": build_queue(user["role"], workflow_progress()),
            "can_load_demo": _can_load_demo(user),
            "notice": request.query_params.get("notice", ""),
            "user_name": user["name"],
            "user_role": user["role"],
            "dashboard": state_store.generated_dashboard,
            "migration_setup": state_store.questionnaire_data.get("migration_setup", {}),
            "knowledge_docs_count": len(state_store.firm_knowledge_docs),
            "in_scope_entities": [entity for entity in ("Account", "Household", "Client") if entity in state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])],
            "wave_conditions": CohortConditionEngine.get_condition_catalog(),
            "required_fields_by_entity": {entity: SchemaMapperAgent.required_fields(entity) for entity in ("Account", "Household", "Client")},
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
            "can_edit_knowledge": user["role"] in DISCOVERY_ROLES,
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
async def entity_sample_files_view(request: Request, uploaded: str = "", notice: str = "", error: str = ""):
    user = get_current_user(request)
    source_uploads = []
    migration_setup = state_store.questionnaire_data.get("migration_setup", {})
    in_scope_entities = set(migration_setup.get("entities", []))
    for entity, files in getattr(state_store, "entity_source_files", {}).items():
        for file_id, record in files.items():
            inferred_entity = "Needs clarification" if entity == "Needs clarification" else (record.get("inference", {}).get("entity") or entity)
            selected = inferred_entity in in_scope_entities
            review_status = record.get("inference_approval_status", "PENDING")
            display_status = "NEEDS_CLARIFICATION" if inferred_entity not in {"Account", "Household", "Client"} else "OUT_OF_SCOPE" if not selected else review_status
            source_uploads.append({**record, "file_id": record.get("file_id", file_id), "inferred_entity": inferred_entity, "display_status": display_status, "scope_eligible": selected})
    source_uploads.sort(key=lambda item: item.get("uploaded_at", ""), reverse=True)
    return render_template_with_context(
        request=request,
        template_name="entity_sample_files.html",
        extra_context={
            "page_title": "Source Discovery & Entity Inference",
            "can_discover": user["role"] in DISCOVERY_ROLES,
            "user_name": user["name"],
            "user_role": user["role"],
            "selected_entities": state_store.get_source_upload_entities(),
            "in_scope_entities": in_scope_entities,
            "source_uploads": source_uploads,
            "uploaded_entity": uploaded,
            "notice": notice,
            "error": error,
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


MAPPING_REVIEW_ROLES = APPROVAL_ROLES


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
            "can_review_mappings": user["role"] in MAPPING_REVIEW_ROLES,
            "can_discover": user["role"] in DISCOVERY_ROLES,
            "can_answer_rfi": user["role"] in DEFINITION_ROLES,
            "approved_rules": transformation_rules.approved_rules(),
            "firm_context": state_store.questionnaire_data,
            "pipeline_run": latest_run,
            "source_files": source_files,
            "mapping_suggestions": mapping_suggestions,
            "information_requests": [item for item in state_store.questionnaire_data.get("mapping_information_requests", []) if item.get("entity") in selected_scope and str(item.get("migration_id", active_migration_id)) == active_migration_id],
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
async def reconciliation_dashboard(request: Request, error: str = ""):
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
        latest = conn.execute(
            "SELECT run_id, status, results_json, executed_by, executed_utc FROM validation_runs "
            "WHERE migration_id = ? ORDER BY executed_utc DESC LIMIT 1", (migration_id,)).fetchone()
    finally:
        conn.close()
    validation_run = None
    if latest:
        validation_run = {"run_id": latest["run_id"], "executed_by": latest["executed_by"],
                          "executed_utc": latest["executed_utc"], **json.loads(latest["results_json"])}
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
            "validation_run": validation_run, "error": error,
            "can_run_validation": user["role"] in VALIDATION_ROLES,
        }
    )


@app.post("/api/validation/run")
async def run_validation(request: Request):
    """Execute deterministic source-to-canonical checks on approved mappings and confirmed files."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    try:
        cfg = load_firm_config()
    except FirmConfigError as exc:
        raise HTTPException(status_code=409, detail=f"Firm configuration is required before validation. {exc}")
    contract = {
        e: {"required": cfg.required_fields(e), "keys": [f.name for f in cfg.entities[e].fields if f.is_key]}
        for e in migration_validation.ENTITIES if e in cfg.entities}
    conn = get_db_connection()
    try:
        mappings: Dict[str, Dict[str, str]] = {}
        for row in conn.execute(
                "SELECT entity_name, source_field, canonical_field FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1",
                (migration_id,)).fetchall():
            mappings.setdefault(row["entity_name"], {})[row["source_field"]] = row["canonical_field"]
    finally:
        conn.close()
    if not mappings:
        return RedirectResponse(url="/reconciliation?error=" + quote_plus("Approve at least one mapping before running validation."),
                                status_code=status.HTTP_303_SEE_OTHER)
    files: Dict[str, List[Dict[str, Any]]] = {}
    for entity, group in state_store.entity_source_files.items():
        for rec in group.values():
            if rec.get("inference_approval_status") != "APPROVED" or entity not in migration_validation.ENTITIES:
                continue
            stored = rec.get("stored_path")
            if not stored or not Path(stored).is_file():
                continue
            try:
                records = read_source_records(rec.get("filename", "source.csv"), Path(stored).read_bytes())
            except (OSError, ValueError):
                continue
            files.setdefault(entity, []).append({"filename": rec.get("filename", "source"), "records": records})
    results = migration_validation.run_checks(contract, mappings, files)
    run_id = "VAL-" + uuid.uuid4().hex[:10].upper()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO validation_runs (run_id, migration_id, status, results_json, executed_by, executed_utc) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, migration_id, results["status"], json.dumps(results), user["name"], now))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({
        "event": "VALIDATION_RUN_EXECUTED", "actor": user["name"], "timestamp_utc": now,
        "details": {"run_id": run_id, "status": results["status"], "summary": results["summary"]},
    })
    return RedirectResponse(url="/reconciliation#validation-results", status_code=status.HTTP_303_SEE_OTHER)


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
    if user["role"] not in CANONICAL_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kernel Configuration edits are strictly restricted to Platform Admin or the Domain SME (canonical model owner)."
        )

    state_store.update_kernel_specs(user["name"], sleeve_invariant, sleeve_tolerance, aum_drift_gate)
    logger.info(f"Wealth Kernel specifications updated by {user['name']} ({user['role']})")
    return RedirectResponse(url="/firm-knowledge", status_code=status.HTTP_303_SEE_OTHER)


CHANGE_TYPES = {
    "ADD_FIELD": "Add a canonical field",
    "CHANGE_DEFINITION": "Change a field definition or rule",
    "ADD_REFERENCE_VALUE": "Add or change a reference/lookup value",
    "OTHER": "Other canonical model change",
}
CHANGE_ENTITIES = ("Account", "Household", "Client")
CHANGE_DECISIONS = {"APPROVED", "REJECTED", "DEFERRED"}


# ---------------------------------------------------------------------------
# Canonical-to-target mapping (forward only, demo target spec, no target loading)
# ---------------------------------------------------------------------------
def _canonical_field_lists() -> Dict[str, List[str]]:
    try:
        cfg = load_firm_config()
    except FirmConfigError:
        return {}
    return {e: cfg.canonical_field_names(e) + canonical_overlay.active_added_fields(e)
            for e in target_mapping.ENTITIES if e in cfg.entities}


def _target_mapping_context(request: Request, user: Dict[str, str], preview_result=None, notice: str = "", error: str = "") -> Dict[str, Any]:
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        mappings = target_mapping.list_mappings(conn, migration_id)
        approved_src = conn.execute(
            "SELECT COUNT(*) FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchone()[0]
        target_snapshot = simulated_target.snapshot(conn)
        ccr = {(r["entity_name"], r["canonical_field"]): r for r in conn.execute(
            "SELECT entity_name, canonical_field, request_id, status FROM canonical_change_requests WHERE migration_id = ? "
            "AND change_type = 'ADD_FIELD' AND status != 'REJECTED' ORDER BY created_utc", (migration_id,)).fetchall()}
        conn.commit()
    finally:
        conn.close()
    spec = target_mapping.load_spec()
    gaps = target_mapping.gap_report(spec, _canonical_field_lists())
    for g in gaps["missing"]:
        existing = ccr.get((g["entity"], g["suggested_canonical_field"]))
        g["change_request"] = dict(existing) if existing else None
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for m in mappings:
        grouped.setdefault(m["entity_name"], []).append(m)
    return {
        "page_title": "Target system & mapping", "user_name": user["name"], "user_role": user["role"],
        "spec": spec, "target_snapshot": target_snapshot, "gaps": gaps,
        "target_system": simulated_target.describe(spec) if target_snapshot else None,
        "can_raise_change": user["role"] in CHANGE_REQUEST_ROLES, "grouped": grouped, "has_mappings": bool(mappings), "approved_source_mappings": approved_src,
        "approved_rules": transformation_rules.approved_rules(), "preview": preview_result,
        "can_propose": user["role"] in DISCOVERY_ROLES, "can_provision": user["role"] in PLATFORM_ROLES, "can_review": user["role"] in APPROVAL_ROLES,
        "can_preview": user["role"] in TARGET_PREVIEW_ROLES, "notice": notice, "error": error,
    }


def _target_redirect(key: str, message: str):
    return RedirectResponse(url=f"/target-mapping?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/target-mapping", response_class=HTMLResponse)
async def target_mapping_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    return render_template_with_context(request, "target_mapping.html", _target_mapping_context(request, user, notice=notice, error=error))


@app.get("/target-setup")
async def target_setup_redirect(request: Request):
    """Target system and mapping are one page; keep the old address working."""
    get_current_user(request)
    return RedirectResponse(url="/target-mapping#target-system", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/target-system/provision")
async def provision_target_system(request: Request):
    """One-time creation of the simulated Simplicity target database (read-only afterwards)."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            snap = simulated_target.provision(conn, user["name"])
        except ValueError as exc:
            return _target_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "TARGET_SYSTEM_PROVISIONED", "actor": user["name"],
                                  "timestamp_utc": snap["provisioned_utc"], "details": {"snapshot_id": snap["snapshot_id"], "spec_version": snap["spec_version"]}})
    return _target_redirect("notice", f"Target system {snap['snapshot_id']} set up. It is fixed; shape the canonical model to fit it.")


@app.post("/api/target-mapping/gaps/raise")
async def raise_gap_change_requests(request: Request, entity_name: str = Form(...), target_field: str = Form(...)):
    """Turn a target-vs-canonical gap into an ordinary canonical change request for the Domain SME to decide.

    The field name and definition come from the server-side gap report, never from the form.
    target_field '*' raises every required gap that has no open request.
    """
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    gaps = [g for g in target_mapping.gap_report(target_mapping.load_spec(), _canonical_field_lists())["missing"]
            if g["entity"] == entity_name.strip() and (target_field == "*" and g["required"] or g["target_field"] == target_field)]
    if not gaps:
        return _target_redirect("error", "No matching gap: the canonical model already covers it.")
    raised: List[str] = []
    conn = get_db_connection()
    try:
        for g in gaps:
            field = g["suggested_canonical_field"]
            exists = conn.execute(
                "SELECT 1 FROM canonical_change_requests WHERE migration_id = ? AND entity_name = ? AND canonical_field = ? "
                "AND change_type = 'ADD_FIELD' AND status != 'REJECTED'", (migration_id, g["entity"], field)).fetchone()
            if exists:
                continue
            request_id = "CCR-" + uuid.uuid4().hex[:10].upper()
            conn.execute(
                "INSERT INTO canonical_change_requests (request_id, migration_id, entity_name, canonical_field, change_type, "
                "proposed_change, reason, status, requested_by, created_utc) VALUES (?, ?, ?, ?, 'ADD_FIELD', ?, ?, 'OPEN', ?, ?)",
                (request_id, migration_id, g["entity"], field,
                 f"Add optional canonical field {field} ({g['type']}) to {g['entity']}.",
                 f"Target gap: Simplicity requires {g['target_field']}{' (required)' if g['required'] else ''} and the canonical model has no source field.",
                 user["name"], datetime.now(timezone.utc).isoformat()))
            raised.append(request_id)
            state_store.audit_log.append({"event": "CANONICAL_CHANGE_REQUESTED", "actor": user["name"],
                                          "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                          "details": {"request_id": request_id, "entity": g["entity"], "field": field, "source": "TARGET_GAP"}})
        conn.commit()
    finally:
        conn.close()
    if not raised:
        return _target_redirect("error", "A change request already exists for that gap.")
    return _target_redirect("notice", f"{len(raised)} canonical change request(s) raised ({', '.join(raised)}). The Domain SME approves and applies them; then regenerate target proposals.")


@app.post("/api/target-mapping/propose")
async def propose_target_mappings(request: Request):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        if simulated_target.snapshot(conn) is None:
            return _target_redirect("error", "Provision the simulated target system first.")
        created = target_mapping.propose(conn, migration_id, _canonical_field_lists())
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "TARGET_MAPPINGS_PROPOSED", "actor": user["name"],
                                  "timestamp_utc": datetime.now(timezone.utc).isoformat(), "details": {"created": created}})
    return _target_redirect("notice", f"{created} target mapping proposal(s) created. Each needs human approval." if created else "All target fields already have proposals.")


@app.post("/api/target-mapping/preview", response_class=HTMLResponse)
async def preview_target_mapping(request: Request):
    """Read-only dry run: canonical rows are derived in memory; only counts and findings are returned."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        approved = [m for m in target_mapping.list_mappings(conn, migration_id) if m["status"] == "APPROVED"]
        conn.commit()
    finally:
        conn.close()
    if not approved:
        return _target_redirect("error", "Approve at least one target mapping before running a preview.")
    canonical_rows: Dict[str, List[Dict[str, Any]]] = {}
    join_info: Dict[str, Dict[str, int]] = {}
    try:
        key_cfg = load_firm_config()
    except FirmConfigError:
        key_cfg = None
    for entity, group in state_store.entity_source_files.items():
        if entity not in target_mapping.ENTITIES:
            continue
        file_rows: List[List[Dict[str, Any]]] = []
        rules = _get_approved_mapping_rules(migration_id, entity)
        mapper = SchemaMapperAgent(
            custom_catalog={**schema_mapper.catalog, **{s: r["canonical_field"] for s, r in rules.items()}},
            approved_transformations={s: r["transformation_expression"] for s, r in rules.items()})
        for rec in group.values():
            stored = rec.get("stored_path")
            if rec.get("inference_approval_status") != "APPROVED" or not stored or not Path(stored).is_file():
                continue
            try:
                records = read_source_records(rec.get("filename", "source.csv"), Path(stored).read_bytes())
            except (OSError, ValueError):
                continue
            file_rows.append(mapper.map_source_to_canonical(entity, records)["mapped_records"])
        key_field = next((f.name for f in key_cfg.entities[entity].fields if f.is_key), None) if key_cfg and entity in key_cfg.entities else None
        joined = target_mapping.merge_by_key(file_rows, key_field)
        canonical_rows[entity] = joined["rows"]
        join_info[entity] = {"key": key_field, "files": len(file_rows), "keyed_rows": joined["keyed_rows"],
                             "unkeyed_rows": joined["unkeyed_rows"], "conflicts": joined["conflicts"]}
    result = target_mapping.preview(target_mapping.load_spec(), approved, canonical_rows, join_info)
    state_store.audit_log.append({"event": "TARGET_MAPPING_PREVIEWED", "actor": user["name"],
                                  "timestamp_utc": datetime.now(timezone.utc).isoformat(), "details": {"migration_id": migration_id}})
    return render_template_with_context(request, "target_mapping.html", _target_mapping_context(request, user, preview_result=result))


@app.post("/api/target-mapping/{target_mapping_id}/review")
async def review_target_mapping(request: Request, target_mapping_id: str, decision: str = Form(...),
                                comments: str = Form(""), rule_override: str = Form("")):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            row = target_mapping.review(conn, target_mapping_id, decision, user["name"], comments, rule_override)
        except LookupError:
            raise HTTPException(status_code=404, detail="Target mapping not found.")
        except ValueError as exc:
            return _target_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "TARGET_MAPPING_" + row["status"], "actor": user["name"],
                                  "timestamp_utc": row["reviewed_utc"],
                                  "details": {"entity": row["entity_name"], "target_field": row["target_field"], "canonical_field": row["canonical_field"], "rule": row["transformation_expression"]}})
    return _target_redirect("notice", f"{row['entity_name']}.{row['target_field']} {row['status'].lower()}.")


# ---------------------------------------------------------------------------
# Transformation rules: governed, versioned, human-approved, registered types only
# ---------------------------------------------------------------------------
def _rules_redirect(key: str, message: str):
    return RedirectResponse(url=f"/transformation-rules?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/transformation-rules", response_class=HTMLResponse)
async def transformation_rules_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        rules = transformation_rules.list_rules(conn)
        usage = {}
        for row in conn.execute(
                "SELECT UPPER(transformation_expression) AS expr, COUNT(*) AS n FROM canonical_mappings "
                "WHERE is_confirmed = 1 AND UPPER(transformation_expression) LIKE 'RULE:%' GROUP BY 1").fetchall():
            usage[row["expr"]] = row["n"]
        conn.commit()
    finally:
        conn.close()
    for rule in rules:
        rule["params_text"] = json.dumps(rule["params"], indent=1, sort_keys=True)
        rule["used_by"] = usage.get(rule["expression"], 0)
    return render_template_with_context(request, "transformation_rules.html", {
        "page_title": "Transformation Rules", "user_name": user["name"], "user_role": user["role"],
        "rules": rules, "rule_types": transformation_rules.RULE_TYPES,
        "builtin_rules": sorted(SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS),
        "date_formats": list(transformation_rules.DATE_FORMATS),
        "can_author": user["role"] in DISCOVERY_ROLES, "can_approve": user["role"] in APPROVAL_ROLES,
        "notice": notice, "error": error,
    })


@app.post("/api/transformation-rules/preview")
async def preview_transformation_rule(request: Request, rule_type: str = Form(...), params_json: str = Form(...), samples: str = Form("")):
    get_current_user(request)
    try:
        params = json.loads(params_json)
        rows = transformation_rules.preview(rule_type, params, [line for line in samples.splitlines() if line.strip() != ""] or [""])
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "rows": rows})


@app.post("/api/transformation-rules")
async def create_transformation_rule(request: Request, rule_id: str = Form(...), rule_type: str = Form(...),
                                     description: str = Form(...), params_json: str = Form(...)):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            rule = transformation_rules.create_rule_version(conn, rule_id, rule_type, description, json.loads(params_json), user["name"])
        except (ValueError, TypeError) as exc:
            return _rules_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "TRANSFORMATION_RULE_DRAFTED", "actor": user["name"],
                                  "timestamp_utc": rule["created_utc"], "details": {"rule": rule["expression"], "type": rule_type}})
    return _rules_redirect("notice", f"{rule['expression']} saved as a draft. It cannot be used by a mapping until it is approved.")


@app.post("/api/transformation-rules/{rule_id}/{version}/decision")
async def decide_transformation_rule(request: Request, rule_id: str, version: int, decision: str = Form(...), comments: str = Form("")):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            rule = transformation_rules.decide_rule(conn, rule_id, version, decision.strip().upper(), user["name"], comments)
        except LookupError:
            raise HTTPException(status_code=404, detail="Rule version not found.")
        except ValueError as exc:
            return _rules_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "TRANSFORMATION_RULE_" + rule["status"], "actor": user["name"],
                                  "timestamp_utc": rule["reviewed_utc"], "details": {"rule": rule["expression"], "comments": comments.strip()}})
    return _rules_redirect("notice", f"{rule['expression']} {rule['status'].lower()}.")


@app.post("/api/transformation-rules/{rule_id}/{version}/retire")
async def retire_transformation_rule(request: Request, rule_id: str, version: int):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            rule = transformation_rules.retire_rule(conn, rule_id, version, user["name"])
        except LookupError:
            raise HTTPException(status_code=404, detail="Rule version not found.")
        except ValueError as exc:
            return _rules_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "TRANSFORMATION_RULE_RETIRED", "actor": user["name"],
                                  "timestamp_utc": rule["reviewed_utc"], "details": {"rule": rule["expression"]}})
    return _rules_redirect("notice", f"{rule['expression']} retired.")


@app.get("/canonical-changes", response_class=HTMLResponse)
async def canonical_changes_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM canonical_change_requests WHERE migration_id = ? ORDER BY created_utc DESC", (migration_id,)).fetchall()]
        applied = canonical_overlay.applied_request_ids(conn)
        conn.commit()
    finally:
        conn.close()
    for row in rows:
        row["applied_utc"] = applied.get(row["request_id"])
        if row["applied_utc"] and row["change_type"] == "ADD_FIELD":
            row["yaml_snippet"] = canonical_overlay.yaml_snippet(row["entity_name"], row["canonical_field"].strip().upper(), row["proposed_change"])
    return render_template_with_context(request, "canonical_changes.html", {
        "page_title": "Canonical Change Requests", "user_name": user["name"], "user_role": user["role"],
        "requests": rows, "change_types": CHANGE_TYPES, "entities": CHANGE_ENTITIES,
        "can_raise": user["role"] in CHANGE_REQUEST_ROLES, "can_decide": user["role"] in CANONICAL_ROLES,
        "notice": notice, "error": error,
    })


def _changes_redirect(key: str, message: str):
    return RedirectResponse(url=f"/canonical-changes?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/canonical-changes")
async def raise_canonical_change(
    request: Request,
    entity_name: str = Form(...),
    canonical_field: str = Form(...),
    change_type: str = Form(...),
    proposed_change: str = Form(...),
    reason: str = Form(...),
):
    user = get_current_user(request)
    entity_name, canonical_field = entity_name.strip(), canonical_field.strip()
    if entity_name not in CHANGE_ENTITIES:
        return _changes_redirect("error", "Choose Account, Household or Client.")
    if change_type not in CHANGE_TYPES:
        return _changes_redirect("error", "Choose a change type.")
    if not canonical_field or not proposed_change.strip() or not reason.strip():
        return _changes_redirect("error", "Field, proposed change and reason are all required.")
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    request_id = "CCR-" + uuid.uuid4().hex[:10].upper()
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO canonical_change_requests (request_id, migration_id, entity_name, canonical_field, change_type, "
            "proposed_change, reason, status, requested_by, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)",
            (request_id, migration_id, entity_name, canonical_field, change_type, proposed_change.strip(),
             reason.strip(), user["name"], datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({
        "event": "CANONICAL_CHANGE_REQUESTED", "actor": user["name"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": {"request_id": request_id, "entity": entity_name, "field": canonical_field, "change_type": change_type},
    })
    return _changes_redirect("notice", f"{request_id} raised for {entity_name}.{canonical_field}; the Domain SME will review it.")


@app.post("/api/canonical-changes/{request_id}/apply")
async def apply_canonical_change(request: Request, request_id: str):
    """Apply an approved change request to the canonical model as an audited overlay."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM canonical_change_requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canonical change request not found.")
        existing = []
        try:
            cfg = load_firm_config()
            existing = cfg.canonical_field_names(row["entity_name"]) if row["entity_name"] in cfg.entities else []
        except FirmConfigError:
            pass
        try:
            record = canonical_overlay.apply_request(conn, dict(row), user["name"], existing)
        except ValueError as exc:
            return _changes_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({
        "event": "CANONICAL_CHANGE_APPLIED", "actor": user["name"],
        "timestamp_utc": record["applied_utc"],
        "details": {"request_id": request_id, "entity": record["entity_name"], "field": record["canonical_field"], "change_type": record["change_type"]},
    })
    return _changes_redirect("notice", f"{request_id} applied to the canonical model.")


@app.post("/api/canonical-changes/{request_id}/decision")
async def decide_canonical_change(request: Request, request_id: str, decision: str = Form(...), note: str = Form("")):
    user = get_current_user(request)
    decision = decision.strip().upper()
    if decision not in CHANGE_DECISIONS:
        return _changes_redirect("error", "Choose approve, reject or defer.")
    if decision in {"REJECTED", "DEFERRED"} and not note.strip():
        return _changes_redirect("error", "Enter a note when rejecting or deferring a change request.")
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT status FROM canonical_change_requests WHERE request_id = ?", (request_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canonical change request not found.")
        if row["status"] not in {"OPEN", "DEFERRED"}:
            return _changes_redirect("error", f"{request_id} is already {row['status'].lower()}.")
        conn.execute(
            "UPDATE canonical_change_requests SET status = ?, decided_by = ?, decided_utc = ?, decision_note = ? WHERE request_id = ?",
            (decision, user["name"], datetime.now(timezone.utc).isoformat(), note.strip(), request_id))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({
        "event": "CANONICAL_CHANGE_DECIDED", "actor": user["name"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": {"request_id": request_id, "decision": decision, "note": note.strip()},
    })
    return _changes_redirect("notice", f"{request_id} {decision.lower()}.")


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

    registered, needs_review, excluded, failed, duplicates = [], [], [], [], []
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

        if inference.get("status") == "NEEDS_CLARIFICATION" or inference.get("entity") not in {"Account", "Household", "Client"}:
            entity_name = "Needs clarification"
            needs_review.append(filename)
        else:
            entity_name = inference["entity"]
            if entity_name not in configured_scope:
                excluded.append(f"{filename} ({entity_name})")

        normalized_role = (
            "RELATIONSHIP_EXTRACT" if inference.get("classification_type") == "RELATIONSHIP"
            else "ACCOUNT_MASTER" if entity_name == "Account"
            else "ENTITY_MASTER"
        )
        try:
            record = state_store.attach_entity_sample_file(entity_name, filename, content, source_role=normalized_role)
            if record.pop("_duplicate_upload", False):
                duplicates.append(filename)
                continue
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
        # Publish only after inference, source context, dictionary notes, and analysis
        # are attached so the durable event contains the complete reviewable file record.
        publish_event(
            "SOURCE_FILE_UPLOADED", str(record.get("migration_id", "MIG-UNCONFIGURED")),
            "source_file", str(record["file_id"]),
            _workflow_safe_source_payload(record, entity_name, normalized_role),
            "migration-analyst",
        )
        state_store.audit_log.append({
            "event": "SOURCE_ENTITY_INFERRED", "actor": "SourceDiscovery",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "details": {"filename": filename, "entity": entity_name,
                        "confidence": inference.get("confidence"), "status": inference.get("status")},
        })
        if entity_name == "Needs clarification":
            continue
        registered.append(f"{filename} → {entity_name}")

    summary = [f"Batch complete: {len(registered)} classified and registered"]
    if needs_review: summary.append(f"{len(needs_review)} needs review ({', '.join(needs_review)})")
    if excluded: summary.append(f"{len(excluded)} retained as out of scope ({', '.join(excluded)})")
    if failed: summary.append(f"{len(failed)} failed validation ({'; '.join(failed)})")
    if duplicates: summary.append(f"{len(duplicates)} identical re-uploads recognized; existing records preserved ({', '.join(duplicates)})")
    notice = ". ".join(summary) + ". Files are retained even when out of scope or awaiting clarification. Re-uploading identical content is idempotent; same-name files with different content remain separate."
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
    target_entity: str = Form(""),
    request: Request = None,
):
    """Allow rejection of any retained file and explicit reclassification into an in-scope entity."""
    user = get_current_user(request) if request is not None else {"name": "Unknown reviewer"}
    form = await request.form() if request is not None else {}
    file_id = str(form.get("file_id", "")).strip()
    normalized_decision = decision.strip().upper()
    if normalized_decision not in {"APPROVE", "REJECT"}:
        return RedirectResponse(url="/entity-sample-files?error=" + quote_plus("Choose Confirm classification or Reject file.") + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)
    if normalized_decision == "REJECT" and not comments.strip():
        return RedirectResponse(url="/entity-sample-files?error=" + quote_plus("Enter a rejection reason before rejecting this file.") + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)

    record = None
    current_entity = None
    for bucket_entity, files in state_store.entity_source_files.items():
        for item in files.values():
            if item.get("file_id") == file_id:
                record, current_entity = item, bucket_entity
                break
        if record:
            break
    if record is None and entity_name in {"Account", "Household", "Client"}:
        record = state_store.entity_sample_files.get(entity_name)
        current_entity = entity_name if record else None
    if not record:
        raise HTTPException(status_code=404, detail="No retained source file was found for this review.")
    if not record.get("inference"):
        record["inference"] = {}

    if normalized_decision == "REJECT":
        record["inference_approval_status"] = "REJECTED"
        record["discovery_status"] = "REJECTED"
        reviewed_entity = record.get("inference", {}).get("entity") or current_entity or entity_name
        record["inference_review"] = {
            "decision": "REJECT", "reviewer": user.get("name", "Unknown reviewer"),
            "comments": comments.strip(), "reviewed_utc": datetime.now(timezone.utc).isoformat(),
        }
    else:
        selected_entity = (target_entity or entity_name).strip()
        old_entity = current_entity
        if selected_entity == "RELATIONSHIP_DEFERRED":
            # Link/bridge files are relationship artifacts; P0 has no relationship entity, so record an explicit disposition.
            if not comments.strip():
                return RedirectResponse(url="/entity-sample-files?error=" + quote_plus("Enter a note explaining the relationship disposition (what the link joins and why it is deferred).") + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)
            record["inferred_entity"] = "Relationship (deferred)"
            record["inference"]["entity"] = "Relationship (deferred)"
            record["inference"]["status"] = "RELATIONSHIP_DEFERRED"
            record["inference_approval_status"] = "NOT_REQUIRED"
            record["discovery_status"] = "RELATIONSHIP_DEFERRED"
            record["inference_review"] = {
                "decision": "RELATIONSHIP_DEFERRED", "reviewer": user.get("name", "Unknown reviewer"),
                "comments": comments.strip(), "reviewed_utc": datetime.now(timezone.utc).isoformat(),
                "previous_entity": old_entity, "confirmed_entity": "Relationship (deferred)",
            }
            reviewed_entity = "Relationship (deferred)"
        elif selected_entity == "OUT_OF_SCOPE":
            # Preserve the file in its current inventory bucket; only its eligibility changes.
            record["inferred_entity"] = "Out of scope"
            record["inference"]["entity"] = "Out of scope"
            record["inference"]["status"] = "OUT_OF_SCOPE"
            record["inference_approval_status"] = "NOT_REQUIRED"
            record["discovery_status"] = "OUT_OF_SCOPE"
            record["inference_review"] = {
                "decision": "OUT_OF_SCOPE", "reviewer": user.get("name", "Unknown reviewer"),
                "comments": comments.strip(), "reviewed_utc": datetime.now(timezone.utc).isoformat(),
                "previous_entity": old_entity, "confirmed_entity": "Out of scope",
            }
            reviewed_entity = "Out of scope"
        else:
            if selected_entity not in {"Account", "Household", "Client"}:
                raise HTTPException(status_code=400, detail="Select Account, Household, Client or Out of scope.")
            configured_scope = state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])
            if selected_entity not in configured_scope:
                raise HTTPException(status_code=409, detail=f"{selected_entity} is not selected in Migration Setup. Re-enable it before classifying this file.")
            if old_entity != selected_entity:
                # Buckets are keyed by source role/filename, not file_id. Remove the
                # existing entry by its durable identity before moving it to the new bucket.
                old_files = state_store.entity_source_files.setdefault(old_entity, {})
                for old_key, old_record in list(old_files.items()):
                    if str(old_record.get("file_id", "")) == file_id:
                        old_files.pop(old_key, None)
                        break
                if state_store.entity_sample_files.get(old_entity, {}).get("file_id") == file_id:
                    state_store.entity_sample_files.pop(old_entity, None)
                new_files = state_store.entity_source_files.setdefault(selected_entity, {})
                new_key = str(record.get("source_role", "ENTITY_MASTER"))
                if new_key in new_files:
                    new_key = new_key + ":" + str(record.get("filename", "source")) + ":" + file_id
                new_files[new_key] = record
            record["entity"] = selected_entity
            record["entity_name"] = selected_entity
            record["inferred_entity"] = selected_entity
            record["inference"]["entity"] = selected_entity
            record["inference"]["status"] = "INFERRED"
            record["inference_approval_status"] = "APPROVED"
            record["discovery_status"] = "APPROVED"
            record["inference_review"] = {
                "decision": "APPROVE", "reviewer": user.get("name", "Unknown reviewer"),
                "comments": comments.strip(), "reviewed_utc": datetime.now(timezone.utc).isoformat(),
                "previous_entity": old_entity, "confirmed_entity": selected_entity,
            }
            if record.get("source_role") in {"ENTITY_MASTER", "ACCOUNT_MASTER"}:
                state_store.entity_sample_files[selected_entity] = record
            reviewed_entity = selected_entity
            state_store.entity_scopes[selected_entity] = "IN_SCOPE"

    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    publish_event("ENTITY_INFERENCE_REVIEWED", migration_id, "source_file", str(record.get("file_id", file_id)),
                  {"entity": reviewed_entity, "previous_entity": current_entity, "filename": record.get("filename"),
                   "decision": normalized_decision, "inference_approval_status": record["inference_approval_status"],
                   "inference": record.get("inference"), "inference_review": record["inference_review"],
                   "scope": state_store.entity_scopes.get(reviewed_entity, "OUT_OF_SCOPE")}, user.get("name", "Unknown reviewer"))
    state_store.audit_log.append({
        "event": "ENTITY_INFERENCE_REVIEWED", "actor": user.get("name", "Unknown reviewer"),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "details": {"entity": reviewed_entity, "previous_entity": current_entity,
                    "filename": record.get("filename"), "decision": normalized_decision, "comments": comments.strip()},
    })
    outcome = "rejected" if normalized_decision == "REJECT" else "confirmed as " + str(reviewed_entity)
    notice = quote_plus(str(record.get("filename", "File")) + " " + outcome + ".")
    return RedirectResponse(url=f"/entity-sample-files?notice={notice}#source-file-{quote_plus(file_id)}", status_code=status.HTTP_303_SEE_OTHER)

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


def _proposal_covered_fields(migration_id: str, entity_name: str, statuses: tuple, exclude_run_id: str = "") -> set:
    """Canonical fields already supplied by proposals of this entity in this migration, across all files."""
    conn = get_db_connection()
    try:
        marks = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"""SELECT DISTINCT canonical_field FROM mapping_suggestions
                WHERE migration_id = ? AND entity_name = ? AND status IN ({marks})
                  AND canonical_field IS NOT NULL AND run_id != ?""",
            (migration_id, entity_name, *statuses, exclude_run_id),
        ).fetchall()
    finally:
        conn.close()
    return {str(r["canonical_field"]) for r in rows}


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
        # Commit inserts/upserts before closing this connection; otherwise SQLite
        # rolls them back and the in-memory list can advertise requests that do not exist.
        conn.commit()
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
    required_fields = SchemaMapperAgent.required_fields(entity_name)
    # Required fields are evaluated per entity across all files: another file's live proposal covers the field.
    mapped_canonical |= _proposal_covered_fields(
        str(migration_id), entity_name, ("PENDING", "APPROVED"), exclude_run_id=str(uploaded.get("last_mapping_run_id") or ""))
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
    uploaded["mapping_generated"] = True
    uploaded["last_mapping_run_id"] = run_id
    publish_event("MAPPING_PROPOSALS_GENERATED", str(migration_id), "mapping_run", str(run_id),
                  {"entity": entity_name, "source_file": uploaded["filename"],
                   "file_id": uploaded.get("file_id"), "run_id": run_id,
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
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    _sync_mapping_information_requests(migration_id)
    requests = state_store.questionnaire_data.setdefault("mapping_information_requests", [])
    item = next((entry for entry in requests if entry.get("request_id") == request_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Information request not found.")
    if item.get("status") != "OPEN":
        raise HTTPException(status_code=409, detail="This information request is already closed.")
    responded_utc = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        durable = conn.execute(
            "SELECT status FROM mapping_information_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if durable is None:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Information request is not persisted yet; refresh and retry.")
        if durable["status"] != "OPEN":
            conn.rollback()
            item["status"] = durable["status"]
            raise HTTPException(status_code=409, detail="This information request was changed by another workflow action.")
        updated = conn.execute(
            """UPDATE mapping_information_requests
               SET response = ?, status = 'ANSWERED'
               WHERE request_id = ?""",
            (answer, request_id),
        )
        if updated.rowcount != 1:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Could not persist the information request response; refresh and retry.")
        conn.commit()
    finally:
        conn.close()
    item["response"] = answer
    item["responded_by"] = user["name"]
    item["responded_utc"] = responded_utc
    item["status"] = "ANSWERED"
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
async def list_source_mapping_suggestions(status_filter: str = "PENDING", migration_id: str = ""):
    """List proposed source-to-canonical mappings for the active migration (or an explicit migration_id; "ALL" spans migrations)."""
    scope_migration = migration_id.strip() or str(state_store.questionnaire_data.get("migration_id", ""))
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
        suggestions = []
        for row in rows:
            item = dict(row)
            if scope_migration not in ("", "ALL") and item.get("migration_id") != scope_migration:
                continue
            item["mapping_kind"] = SchemaMapperAgent.mapping_kind(item.get("proposed_rule"), item.get("canonical_field"))
            suggestions.append(item)
        return JSONResponse({"status": "SUCCESS", "suggestions": suggestions})
    finally:
        conn.close()


@app.post("/api/mapping-suggestions/{suggestion_id}/review")
async def review_source_mapping_suggestion(
    suggestion_id: str,
    decision: str = Form(...),
    reviewer: str = Form(...),
    comments: str = Form(""),
    rule_override: str = Form(""),
    request: Request = None,
):
    """Record an explicit approve/reject decision and prevent repeat review."""
    if request is not None:
        user = get_current_user(request)
        if user.get("role") not in APPROVAL_ROLES:
            raise HTTPException(status_code=403, detail="Only Migration Analyst or Platform Admin may approve or reject mappings.")
        reviewer_name = str(user.get("name", "")).strip()
    else:
        # Direct service-level invocation is retained for focused unit tests.
        reviewer_name = reviewer.strip()
    normalized = decision.strip().upper()
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
        override = rule_override.strip().upper() if isinstance(rule_override, str) else ""
        if normalized == "APPROVED" and override:
            if not transformation_rules.approved_rule_for_expression(override):
                conn.rollback()
                raise HTTPException(status_code=400, detail=f"'{override}' is not an approved transformation rule version.")
            proposed_expression = override
            conn.execute("UPDATE mapping_suggestions SET proposed_rule = ? WHERE suggestion_id = ? AND status = 'PENDING'", (override, suggestion_id))
        if normalized == "APPROVED" and not SchemaMapperAgent.supports_transformation(proposed_expression):
            detail = (
                f"The rule '{proposed_expression}' is not executable yet. "
                "Register a safe transformation handler before approving it for execution."
            )
            if request is not None and "text/html" in request.headers.get("accept", ""):
                conn.rollback()
                return RedirectResponse(
                    url="/agent-pipeline?error=" + quote_plus(detail) + "#pending-mappings",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            raise HTTPException(status_code=400, detail=detail)
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
                    row["canonical_field"], proposed_expression,
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
    if user.get("role") not in APPROVAL_ROLES:
        raise HTTPException(status_code=403, detail="Only Migration Analyst or Platform Admin may approve entity mappings.")
    reviewer_name = str(user.get("name", "")).strip()
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
            if not SchemaMapperAgent.supports_transformation(rule):
                blockers.append(f"{source_field or '(missing source field)'}: rule '{rule}' has no registered safe execution handler")
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
        # Required fields with an unanswered information request are unresolved gaps:
        # entity-level approval must wait until a response is recorded.
        for row in rows:
            if float(row["confidence_score"] or 0.0) < EVIDENCE_REVIEW_THRESHOLD:
                blockers.append(f"{row['source_field']} -> {row['canonical_field']}: low-confidence proposal ({float(row['confidence_score'] or 0.0):.0%}) needs individual review")
        # A field supplied by any pending proposal in this set is covered: approving the set closes the request.
        covered_by_set = {str(r["canonical_field"]) for r in rows if r["canonical_field"]}
        covered_by_set |= {str(r["canonical_field"]) for r in conn.execute(
            "SELECT canonical_field FROM canonical_mappings WHERE migration_id = ? AND entity_name = ? AND is_confirmed = 1",
            (migration_id, entity_name)).fetchall() if r["canonical_field"]}
        for gap in state_store.questionnaire_data.get("mapping_information_requests", []):
            if (gap.get("entity") == entity_name and gap.get("status") == "OPEN"
                    and gap.get("canonical_field") not in covered_by_set
                    and str(gap.get("migration_id", migration_id)) == migration_id):
                blockers.append(f"{gap.get('canonical_field')}: required field has an open information request ({gap.get('request_id')})")
        if blockers:
            conn.rollback()
            message = f"Cannot approve all {entity_name} mappings yet. {len(blockers)} validation issue(s): " + "; ".join(blockers)
            return RedirectResponse(url="/agent-pipeline?error=" + quote_plus(message) + "#pending-mappings", status_code=status.HTTP_303_SEE_OTHER)

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