import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ui.auth import session_mgr
from ui.state import state_store
from agents.orchestrator import AgenticOrchestrator
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from agents.operational.cutover_agent import CutoverAgent
from agents.operational.audit_agent import AuditAgent
from data_plane.storage_manager import HybridStorageManager
from control_plane.services.profiler import SourceDataProfiler
from control_plane.services.wave_rules import CohortConditionEngine, WaveConditionEvaluator
from control_plane.services.adf_client import AzureDataFactoryClient
from control_plane.services.adf_exporter import ADFPackageExporter
from control_plane.db import get_db_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataMigrationWorkbench")

app = FastAPI(title="Data Migration Workbench", version="3.5.0")

ui_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(ui_dir, ".."))
storage_dir = os.path.join(root_dir, "data_plane", "storage")

static_dir = os.path.join(ui_dir, "static")
templates_dir = os.path.join(ui_dir, "templates")

os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

orchestrator = AgenticOrchestrator(source_dir=storage_dir)
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
        "migration_id": state_store.questionnaire_data.get("migration_id", "MIG-CORECAP-2026"),
        "firm_name": state_store.questionnaire_data.get("firm_name", "Core Cap Wealth Management"),
        "adls_connected": state_store.adls_connected,
        "adls_container": state_store.adls_container,
        "kernel_specs": state_store.kernel_specs
    }
    base_context.update(extra_context)
    return templates.TemplateResponse(request=request, name=template_name, context=base_context)


# --- AUTHENTICATION ROUTES ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error})


@app.post("/login")
async def process_login(email: str = Form(...), password: str = Form(...), role: str = Form(...)):
    session_id = session_mgr.authenticate(email, password, role)
    if not session_id:
        return RedirectResponse(url="/login?error=Invalid+credentials+provided", status_code=status.HTTP_303_SEE_OTHER)

    response = RedirectResponse(url="/firm-knowledge", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="session_id", value=session_id, httponly=True)
    return response


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
            "page_title": "2. Dashboard Overview",
            "user_name": user["name"],
            "user_role": user["role"],
            "dashboard": state_store.generated_dashboard
        }
    )


@app.get("/firm-knowledge", response_class=HTMLResponse)
async def firm_knowledge_studio(request: Request):
    user = get_current_user(request)
    return render_template_with_context(
        request=request,
        template_name="firm_knowledge.html",
        extra_context={
            "page_title": "1. Firm Knowledge Studio",
            "user_name": user["name"],
            "user_role": user["role"],
            "form": state_store.questionnaire_data,
            "docs": state_store.firm_knowledge_docs
        }
    )


@app.get("/entity-selection", response_class=HTMLResponse)
async def entity_selection_view(request: Request):
    user = get_current_user(request)
    return render_template_with_context(
        request=request,
        template_name="entity_selection.html",
        extra_context={
            "page_title": "3. Entity Scope Selector",
            "user_name": user["name"],
            "user_role": user["role"],
            "menu_card": state_store.get_full_menu_card()
        }
    )


@app.get("/entity-sample-files", response_class=HTMLResponse)
async def entity_sample_files_view(request: Request):
    user = get_current_user(request)
    return render_template_with_context(
        request=request,
        template_name="entity_sample_files.html",
        extra_context={
            "page_title": "4. Entity Sample Files",
            "user_name": user["name"],
            "user_role": user["role"],
            "selected_entities": state_store.get_in_scope_entities()
        }
    )


@app.get("/agent-pipeline", response_class=HTMLResponse)
async def agent_pipeline_view(request: Request):
    user = get_current_user(request)
    latest_run = state_store.pipeline_runs[-1] if state_store.pipeline_runs else None
    return render_template_with_context(
        request=request,
        template_name="agent_pipeline.html",
        extra_context={
            "page_title": "5. Agent Pipeline Studio",
            "user_name": user["name"],
            "user_role": user["role"],
            "firm_context": state_store.questionnaire_data,
            "pipeline_run": latest_run
        }
    )


@app.get("/hitl", response_class=HTMLResponse)
async def hitl_approval_queue(request: Request):
    user = get_current_user(request)
    latest_run = state_store.pipeline_runs[-1] if state_store.pipeline_runs else None

    rep_code = latest_run.get("rep_code", "REP-9981") if latest_run else "REP-9981"
    wave_id = latest_run.get("wave_id", "WAVE-01") if latest_run else "WAVE-01"
    cohort_summary = state_store.get_cohort_sleeve_summary(rep_code)

    cohort = {
        "rep_code": rep_code,
        "wave_id": wave_id,
        "account_id": "ACC-99812",
        "allocations": cohort_summary.get("allocations", []),
        "sum_check": cohort_summary.get("sum_check", "1.0000 (PASS)")
    }

    return render_template_with_context(
        request=request,
        template_name="hitl_queue.html",
        extra_context={
            "page_title": "6. HITL Approval Queue",
            "user_name": user["name"],
            "user_role": user["role"],
            "cohort": cohort,
            "pipeline_run": latest_run,
            "audit_log": state_store.audit_log
        }
    )


@app.get("/reconciliation", response_class=HTMLResponse)
async def reconciliation_dashboard(request: Request):
    user = get_current_user(request)
    return render_template_with_context(
        request=request,
        template_name="reconciliation.html",
        extra_context={
            "page_title": "7. Reconciliation & Drift",
            "user_name": user["name"],
            "user_role": user["role"],
            "pipeline_runs": state_store.pipeline_runs,
            "drift_gate": state_store.questionnaire_data.get("aum_drift_gate", "0.50%")
        }
    )


# --- PHASE 1 PILOT ENDPOINTS: SOURCE-TO-CANONICAL ACCOUNT ---

@app.post("/api/pilot/account/profile")
async def profile_pilot_account_file(file: UploadFile = File(...)):
    """Inspects uploaded accounts.csv, infers types, null rates, and candidate keys."""
    content = await file.read()
    profile = SourceDataProfiler.profile_csv_content(file.filename, content, entity_name="Account")
    profile_id = state_store.persist_source_profile(profile, run_id="PILOT-ACCOUNT-RUN-01")
    profile["profile_id"] = profile_id
    return JSONResponse({"status": "SUCCESS", "profile": profile})


@app.post("/api/pilot/account/map")
async def map_pilot_account(records: Optional[List[Dict[str, Any]]] = None):
    """Executes source-to-canonical mapping with confidence scoring and persists metadata lineage."""
    if not records:
        accounts_path = Path("test_files/accounts.csv")
        if not accounts_path.exists():
            accounts_path = Path(os.path.join(root_dir, "test_files", "accounts.csv"))
        if accounts_path.exists():
            import pandas as pd
            df = pd.read_csv(accounts_path)
            records = df.to_dict(orient="records")
        else:
            records = []

    mapping_res = schema_mapper.map_source_to_canonical("Account", records)
    state_store.persist_mapping_lineage(mapping_res, run_id="PILOT-ACCOUNT-RUN-01", actor="Platform Admin")
    return JSONResponse({"status": "SUCCESS", "mapping": mapping_res})


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
        "migration_id": state_store.questionnaire_data.get("migration_id", "MIG-CORECAP-2026"),
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
    advisor_count: int = Form(...),
    operating_model: str = Form(...),
    primary_custodian: str = Form(...),
    source_tamp: str = Form(...),
    performance_source: str = Form(...),
    aum_drift_gate: str = Form(...),
    restricted_models: str = Form(...)
):
    form_payload = {
        "firm_name": firm_name,
        "advisor_count": advisor_count,
        "operating_model": operating_model,
        "primary_custodian": primary_custodian,
        "source_tamp": source_tamp,
        "performance_source": performance_source,
        "aum_drift_gate": aum_drift_gate,
        "restricted_models": restricted_models
    }
    state_store.update_questionnaire_from_form(form_payload)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/save-scopes")
async def save_entity_scopes(request: Request):
    form_data = await request.form()
    scopes_map = {}
    for key, value in form_data.items():
        if key.startswith("scope_"):
            entity_name = key.replace("scope_", "")
            scopes_map[entity_name] = str(value)

    state_store.save_entity_scopes(scopes_map)
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/upload-sample-file")
async def upload_entity_sample_file(entity_name: str = Form(...), file: UploadFile = File(...)):
    content = await file.read()
    state_store.attach_entity_sample_file(entity_name, file.filename, content)
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/knowledge/file-upload")
async def upload_firm_file(category: str = Form(...), file: UploadFile = File(...)):
    content = await file.read()
    state_store.add_firm_file(file.filename, category, content)
    return RedirectResponse(url="/firm-knowledge", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/pipeline/run")
async def execute_agent_pipeline(rep_code: str = Form("REP-9981")):
    """Run source-to-canonical mapping for explicitly selected IN_SCOPE entities only."""
    scoped_entities = [
        entity["entity_name"]
        for entity in state_store.get_full_menu_card().values()
        for entity in entity
        if entity.get("current_scope") == "IN_SCOPE"
    ]
    migration_id = state_store.questionnaire_data.get("migration_id", "UNCONFIGURED")
    logger.info("Starting source-to-canonical mapping for %s across %s explicitly in-scope entities", rep_code, len(scoped_entities))
    pipeline_result = orchestrator.run_cohort_pipeline(
        rep_code=rep_code,
        in_scope_entity_names=scoped_entities,
        migration_id=migration_id,
    )
    state_store.record_pipeline_run(pipeline_result)
    status_code = 200 if pipeline_result.get("status") not in {"BLOCKED_NO_SCOPE"} else 409
    return JSONResponse({"status": pipeline_result.get("status", "UNKNOWN"), "result": pipeline_result}, status_code=status_code)


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
async def approve_cohort(rep_code: str = Form(...), approver: str = Form(...)):
    cutover_res = cutover_agent.execute_atomic_cutover(wave_id="WAVE-01", advisor_cohort=rep_code)
    audit_entry = state_store.record_approval(rep_code, approver, "APPROVED")
    return JSONResponse({
        "status": "SUCCESS",
        "message": f"Cohort {rep_code} approved for atomic cutover.",
        "cutover_details": cutover_res,
        "audit_entry": audit_entry
    })


@app.post("/api/hitl/reject")
async def reject_cohort(rep_code: str = Form(...), reason: str = Form(...)):
    audit_entry = state_store.record_approval(rep_code, "Domain SME", f"REJECTED: {reason}")
    return JSONResponse({
        "status": "REJECTED",
        "message": f"Cohort {rep_code} returned for analyst review.",
        "audit_entry": audit_entry
    })

# --- HOUSEHOLD ENDPOINTS ---

@app.post("/api/pilot/household/profile")
async def profile_pilot_household_file(file: UploadFile = File(...)):
    """Inspects uploaded households.csv, infers types, null rates, and candidate keys."""
    content = await file.read()
    profile = SourceDataProfiler.profile_csv_content(file.filename, content, entity_name="Household")
    profile_id = state_store.persist_source_profile(profile, run_id="PILOT-HH-RUN-01")
    profile["profile_id"] = profile_id
    return JSONResponse({"status": "SUCCESS", "profile": profile})


@app.post("/api/pilot/household/map")
async def map_pilot_household(records: Optional[List[Dict[str, Any]]] = None):
    """Executes source-to-canonical mapping for Household and persists lineage."""
    if not records:
        hh_path = Path("test_files/households.csv")
        if not hh_path.exists():
            hh_path = Path(os.path.join(root_dir, "test_files", "households.csv"))
        if hh_path.exists():
            import pandas as pd
            df = pd.read_csv(hh_path)
            records = df.to_dict(orient="records")
        else:
            records = []

    mapping_res = schema_mapper.map_source_to_canonical("Household", records)
    state_store.persist_mapping_lineage(mapping_res, run_id="PILOT-HH-RUN-01", actor="Platform Admin")
    return JSONResponse({"status": "SUCCESS", "mapping": mapping_res})


@app.get("/api/pilot/household/lineage")
async def get_pilot_household_lineage():
    """Retrieves SQLite metadata lineage audit records for Household."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM field_lineage_audit WHERE entity_name = 'Household' ORDER BY timestamp_utc DESC LIMIT 20;")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return JSONResponse({"status": "SUCCESS", "lineage_records": rows})


# --- CLIENT / PARTY ENDPOINTS ---

@app.post("/api/pilot/client/profile")
async def profile_pilot_client_file(file: UploadFile = File(...)):
    """Inspects uploaded clients.csv, infers types, null rates, and candidate keys."""
    content = await file.read()
    profile = SourceDataProfiler.profile_csv_content(file.filename, content, entity_name="Client")
    profile_id = state_store.persist_source_profile(profile, run_id="PILOT-CLIENT-RUN-01")
    profile["profile_id"] = profile_id
    return JSONResponse({"status": "SUCCESS", "profile": profile})


@app.post("/api/pilot/client/map")
async def map_pilot_client(records: Optional[List[Dict[str, Any]]] = None):
    """Executes source-to-canonical mapping for Client/Party and persists lineage."""
    if not records:
        client_path = Path("test_files/clients.csv")
        if not client_path.exists():
            client_path = Path(os.path.join(root_dir, "test_files", "clients.csv"))
        if client_path.exists():
            import pandas as pd
            df = pd.read_csv(client_path)
            records = df.to_dict(orient="records")
        else:
            records = []

    mapping_res = schema_mapper.map_source_to_canonical("Client", records)
    state_store.persist_mapping_lineage(mapping_res, run_id="PILOT-CLIENT-RUN-01", actor="Platform Admin")
    return JSONResponse({"status": "SUCCESS", "mapping": mapping_res})


@app.get("/api/pilot/client/lineage")
async def get_pilot_client_lineage():
    """Retrieves SQLite metadata lineage audit records for Client/Party."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM field_lineage_audit WHERE entity_name = 'Client' ORDER BY timestamp_utc DESC LIMIT 20;")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return JSONResponse({"status": "SUCCESS", "lineage_records": rows})