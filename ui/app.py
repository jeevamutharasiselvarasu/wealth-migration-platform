import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ui.auth import session_mgr
from ui.state import state_store
from agents.orchestrator import AgenticOrchestrator
from agents.operational.cutover_agent import CutoverAgent
from agents.operational.audit_agent import AuditAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataMigrationWorkbench")

app = FastAPI(title="Data Migration Workbench — Control Plane API", version="3.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ui_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(ui_dir, ".."))
storage_dir = os.path.join(root_dir, "data_plane", "storage")
static_dir = os.path.join(ui_dir, "static")

os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

orchestrator = AgenticOrchestrator(source_dir=storage_dir)
cutover_agent = CutoverAgent()
audit_agent = AuditAgent()

# --- PYDANTIC REQUEST SCHEMAS ---

class CreateMigrationRequest(BaseModel):
    name: str = Field(..., description="Unique migration name")
    category: str = "Custom migration"
    description: Optional[str] = None
    sourceSystem: str = "Amplify TAMP"
    targetSystem: str = "Salesforce SPARC + Orion"
    entities: List[str] = []
    templateId: Optional[str] = None
    createdBy: str = "migration-analyst"

class RulebookUpdateRequest(BaseModel):
    content: str
    updatedBy: str = "migration-analyst"

class ApprovalRequest(BaseModel):
    artifactId: Optional[str] = None
    stage: str
    decision: str
    reviewer: str
    comments: Optional[str] = None

# --- MAIN SPA ENTRY POINT & ROUTE HANDLERS ---

@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
@app.get("/firm-knowledge", include_in_schema=False)
@app.get("/entity-selection", include_in_schema=False)
@app.get("/entity-sample-files", include_in_schema=False)
@app.get("/agent-pipeline", include_in_schema=False)
@app.get("/hitl", include_in_schema=False)
@app.get("/reconciliation", include_in_schema=False)
async def serve_spa_root(request: Request):
    """Serves the main SPA container frame for all client-side lifecycle stages."""
    prototype_path = os.path.join(static_dir, "prototype.html")
    if os.path.exists(prototype_path):
        return FileResponse(prototype_path)
    return HTMLResponse("<h1>Data Migration Workbench SPA Entry Point</h1>")

@app.get("/api/health")
async def health_check():
    return {
        "service": "Data Migration Control Plane",
        "status": "healthy",
        "layers": ["SOURCE", "RAW", "PROCESSED", "TRANSFORMED", "TARGET"],
        "sqlOutput": "GENERIC_ANSI_SQL"
    }

# --- STAGE 00: MIGRATION SETUP & KNOWLEDGE ASSETS ---

@app.post("/api/migrations")
async def create_migration(req: CreateMigrationRequest):
    logger.info(f"Created migration boundary: {req.name}")
    return {"status": "SUCCESS", "migrationId": req.name}

@app.get("/api/migrations/{migration_id}/knowledge/{asset_id}")
async def get_knowledge_asset(migration_id: str, asset_id: str):
    return JSONResponse({"migrationId": migration_id, "assetId": asset_id, "content": state_store.questionnaire_data})

@app.put("/api/migrations/{migration_id}/knowledge/{asset_id}")
async def update_knowledge_asset(migration_id: str, asset_id: str, request: Request):
    payload = await request.json()
    state_store.update_questionnaire_from_form(payload.get("content", {}))
    return {"status": "SUCCESS", "assetId": asset_id}

@app.get("/api/migrations/{migration_id}/rulebook")
async def get_rulebook(migration_id: str):
    return {
        "migrationId": migration_id,
        "content": state_store.get_rulebook_markdown(),
        "parsed": {"fieldRules": [], "crosswalks": [], "joins": [], "validationRules": []}
    }

@app.put("/api/migrations/{migration_id}/rulebook")
async def update_rulebook(migration_id: str, req: RulebookUpdateRequest):
    state_store.save_rulebook_markdown(req.content)
    return {"status": "SUCCESS", "updatedBy": req.updatedBy, "parsed": {"fieldRules": []}}

# --- STAGE 01 & 02: INGESTION, DISCOVERY & SAMPLE UPLOADS ---

@app.post("/api/runs/{run_id}/upload-file")
async def upload_run_file(run_id: str, entity: str = Form(...), file: UploadFile = File(...)):
    content = await file.read()
    res = state_store.attach_entity_sample_file(entity, file.filename, content)
    return JSONResponse({"status": "SUCCESS", "file": res})

@app.post("/api/runs/{run_id}/discovery")
async def execute_discovery_profiling(run_id: str):
    result = orchestrator.run_cohort_pipeline(rep_code="REP-9981", wave_id="WAVE-01")
    state_store.record_pipeline_run(result)
    return JSONResponse({
        "status": "SUCCESS",
        "entityCount": len(state_store.get_in_scope_entities()),
        "discoveredEntities": [
            {
                "entity": ent["entity_name"],
                "sourceFile": ent.get("expected_file", "accounts.csv"),
                "candidateKeys": ent.get("required_fields", ["ACCT_NUM"])[:1],
                "fields": [{"column": f, "type": "string", "candidateKey": False, "nullRate": "0.00%", "requiresCrosswalk": False} for f in ent.get("required_fields", [])]
            } for ent in state_store.get_in_scope_entities()
        ]
    })

# --- STAGE 05: 9-AGENT PIPELINE EXECUTION & EXPORT ---

@app.post("/api/pipeline/run")
async def execute_agent_pipeline(rep_code: str = Form("REP-9981"), wave_id: str = Form("WAVE-01")):
    pipeline_result = orchestrator.run_cohort_pipeline(rep_code=rep_code, wave_id=wave_id)
    state_store.record_pipeline_run(pipeline_result)
    return JSONResponse({"status": "SUCCESS", "result": pipeline_result})

@app.get("/api/pipeline/agent-detail/{agent_id}")
async def get_agent_detail(agent_id: str):
    detail = state_store.get_agent_detail(agent_id)
    return JSONResponse({"status": "SUCCESS", "agent_id": agent_id, "detail": detail})

@app.post("/api/pipeline/override-agent")
async def override_agent_logic(agent_id: str = Form(...), new_logic: str = Form(...), override_notes: str = Form(...)):
    res = state_store.apply_agent_override(agent_id, new_logic, override_notes)
    return JSONResponse(res)

@app.get("/api/runs/{run_id}/export-package")
async def export_package(run_id: str, mode: str = "local"):
    return {
        "status": "SUCCESS",
        "runId": run_id,
        "mode": mode,
        "targetLocation": f"App_Data/Storage/{run_id}/TRANSFORMED/transformation_rules.sql",
        "sqlContent": "-- Transformation SQL view definitions\nCREATE OR REPLACE VIEW stg_accounts AS SELECT ACCT_NUM AS account_id FROM accounts;"
    }

# --- STAGE 06: RECONCILIATION & STAGE APPROVALS (HITL) ---

@app.post("/api/runs/{run_id}/reconciliation")
async def get_reconciliation_summary(run_id: str):
    latest_run = state_store.pipeline_runs[-1] if state_store.pipeline_runs else {}
    rec_summary = latest_run.get("reconciliation_summary", {})
    return JSONResponse({
        "status": "SUCCESS",
        "sourceTotalAum": rec_summary.get("source_aum", 108000.00),
        "targetTotalAum": rec_summary.get("target_aum", 108000.00),
        "aumVariance": 0.00,
        "aumVariancePct": rec_summary.get("variance_pct", 0.00),
        "withinTolerance": rec_summary.get("status") == "PASS",
        "domainResults": [
            {"domain": "Accounts", "sourceCount": 3, "targetCount": 3, "variance": 0, "matched": True},
            {"domain": "Positions", "sourceCount": 2, "targetCount": 2, "variance": 0, "matched": True}
        ]
    })

@app.get("/api/runs/{run_id}/approvals")
async def get_run_approvals(run_id: str):
    return {"status": "SUCCESS", "approvals": [{"stage": "00_setup", "decision": "APPROVED", "reviewer": "Migration Analyst"}]}

@app.post("/api/runs/{run_id}/approvals")
async def approve_stage(run_id: str, req: ApprovalRequest):
    entry = state_store.record_approval(rep_code="REP-9981", approver=req.reviewer, decision="APPROVED")
    return {"status": "SUCCESS", "stage": req.stage, "entry": entry}

@app.delete("/api/runs/{run_id}/approvals/{stage}")
async def revoke_stage_approval(run_id: str, stage: str):
    return {"status": "SUCCESS", "revokedStage": stage}