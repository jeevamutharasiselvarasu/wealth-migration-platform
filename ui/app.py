import os
import logging
from typing import Dict, Any, List
from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ui.auth import session_mgr
from ui.state import state_store
from agents.orchestrator import AgenticOrchestrator
from agents.operational.cutover_agent import CutoverAgent
from agents.operational.audit_agent import AuditAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataMigrationWorkbench")

app = FastAPI(title="Data Migration Workbench", version="3.4.0")

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
cutover_agent = CutoverAgent()
audit_agent = AuditAgent()

def get_current_user(request: Request) -> Dict[str, str]:
    session = session_mgr.get_session(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_307_TEMPORARY_REDIRECT, headers={"Location": "/login"})
    return session

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

# --- WORKBENCH LIFECYCLE ROUTES ---

@app.get("/firm-knowledge", response_class=HTMLResponse)
async def firm_knowledge_studio(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="firm_knowledge.html",
        context={
            "page_title": "1. Firm Knowledge Studio",
            "user_name": user["name"],
            "user_role": user["role"],
            "form": state_store.questionnaire_data,
            "docs": state_store.firm_knowledge_docs
        }
    )

@app.get("/", response_class=HTMLResponse)
async def dashboard_overview(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "page_title": "2. Dashboard Overview",
            "user_name": user["name"],
            "user_role": user["role"],
            "dashboard": state_store.generated_dashboard
        }
    )

@app.get("/entity-selection", response_class=HTMLResponse)
async def entity_selection_view(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="entity_selection.html",
        context={
            "page_title": "3. Entity Scope Selector",
            "user_name": user["name"],
            "user_role": user["role"],
            "menu_card": state_store.get_full_menu_card()
        }
    )

@app.get("/entity-sample-files", response_class=HTMLResponse)
async def entity_sample_files_view(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request=request,
        name="entity_sample_files.html",
        context={
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
    return templates.TemplateResponse(
        request=request,
        name="agent_pipeline.html",
        context={
            "page_title": "5. Agent Pipeline Studio",
            "user_name": user["name"],
            "user_role": user["role"],
            "firm_context": state_store.questionnaire_data,
            "pipeline_run": latest_run
        }
    )

@app.get("/hitl", response_class=HTMLResponse)
async def hitl_approval_queue(request: Request):
    """Step 6: HITL Approval Queue for sleeve math and atomic cutover authorization."""
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

    return templates.TemplateResponse(
        request=request,
        name="hitl_queue.html",
        context={
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
    return templates.TemplateResponse(
        request=request,
        name="reconciliation.html",
        context={
            "page_title": "7. Reconciliation & Drift",
            "user_name": user["name"],
            "user_role": user["role"],
            "pipeline_runs": state_store.pipeline_runs,
            "drift_gate": state_store.questionnaire_data.get("aum_drift_gate", "0.50%")
        }
    )

# --- FORM SUBMISSIONS & API ENDPOINTS ---

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
async def execute_agent_pipeline(rep_code: str = Form("REP-9981"), wave_id: str = Form("WAVE-01")):
    logger.info(f"Executing 9-agent pipeline for advisor '{rep_code}' ({wave_id})...")
    pipeline_result = orchestrator.run_cohort_pipeline(rep_code=rep_code, wave_id=wave_id)
    state_store.record_pipeline_run(pipeline_result)
    return JSONResponse({"status": "SUCCESS", "result": pipeline_result})

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
# Add these endpoints to ui/app.py

@app.get("/api/pipeline/agent-detail/{agent_id}")
async def get_agent_detail(agent_id: str, request: Request):
    """Returns granular transformation inputs, SQL logic, and target outputs for an agent."""
    user = get_current_user(request)
    detail = state_store.get_agent_detail(agent_id)
    if not detail:
        # Fallback default trace structure if pipeline hasn't been run yet
        detail = {
            "name": f"Agent {agent_id.replace('_', ' ').title()}",
            "phase": "Pipeline Execution",
            "status": "READY_TO_RUN",
            "inputs": {"message": "Run pipeline in Step 5 to generate live traces."},
            "transformation_logic": "-- Transformation SQL or logic will be displayed here after execution.",
            "outputs": {},
            "warnings": []
        }
    return JSONResponse({"status": "SUCCESS", "agent_id": agent_id, "detail": detail})

@app.post("/api/pipeline/override-agent")
async def override_agent_logic(
    agent_id: str = Form(...),
    new_logic: str = Form(...),
    override_notes: str = Form(...)
):
    """Applies a human intervention override to an agent's transformation logic."""
    res = state_store.apply_agent_override(agent_id, new_logic, override_notes)
    logger.info(f"Human intervention override applied to Agent '{agent_id}': {override_notes}")
    return JSONResponse(res)

# Add / verify this endpoint in ui/app.py

@app.get("/api/pipeline/agent-detail/{agent_id}")
async def get_agent_detail(agent_id: str, request: Request):
    """Returns granular source inputs, crosswalk SQL logic, and target outputs for an agent."""
    user = get_current_user(request)
    detail = state_store.get_agent_detail(agent_id)
    return JSONResponse({"status": "SUCCESS", "agent_id": agent_id, "detail": detail})