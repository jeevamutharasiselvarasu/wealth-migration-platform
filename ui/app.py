import base64
import csv
import hashlib
import io
import zipfile
import os
import re
import uuid
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, urlencode, urlparse
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from control_plane.services import wave_validation, validation_rules, knowledge_chunks, knowledge_sources, knowledge_index, retrieval_tuning, embeddings, decision_model

from ui.auth import session_mgr
from ui.work_queue import build_queue
from ui.role_policy import POST_POLICY, RUN_FAILURE_ROLES, PLATFORM_KNOWLEDGE_ROLES, DEFINITION_ROLES, TARGET_PREVIEW_ROLES, VALIDATION_ROLES, APPROVAL_ROLES, CANONICAL_ROLES, CHANGE_REQUEST_ROLES, DISCOVERY_ROLES, PLATFORM_ROLES, SETUP_ROLES, admin_may_view, allowed_roles_for, gated_stage_for, platform_only_page
from ui.state import state_store
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane import file_store
from control_plane.services import sql_package, gap_recommender, user_registry, adf_simulator, artifact_store, execution_simulator, package_contract, cross_file, adf_package, azure_simulated, activity_feed, canonical_overlay, migration_validation, validation_scenarios, rule_inference, rule_suggestions, drift_detection, signoff_evidence, target_connector, target_inference, target_mapping, transformation_rules, migration_registry, confluence_source, azure_connection, blob_source, git_connection, adf_environment, validation_signoff, platform_knowledge
from control_plane.services import sql_target
from control_plane.services import canonical_derivations, run_monitor, target_validation
from control_plane.services import xodus_pipelines, live_target, deployment_check, adf_runs, git_pull_requests, promotion_gate, live_validation
from control_plane.services import canonical_model, code_mappings, target_adapter, entity_resolution
from control_plane.services.value_evidence import EVIDENCE_REVIEW_THRESHOLD
from data_plane.storage_manager import HybridStorageManager
from control_plane.services.profiler import SourceDataProfiler
from control_plane.services.mcl_validation import validate_mcl_relationships
from control_plane.services.source_file_adapter import read_source_records
from control_plane.services.wave_rules import CohortConditionEngine, WaveConditionEvaluator
from control_plane.services.adf_client import AzureDataFactoryClient
from control_plane.db import database_kind, get_db_connection, using_postgres
from control_plane.services.firm_config import P0_ENTITIES, FirmConfigError, load_firm_config, reference_fields, supporting_plan, with_supporting_entities
from control_plane.services.workflow_event_engine import publish_event, read_events, get_projection, replay_events
from agents.p0_agent_suite import MigrationAgentCoordinator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataMigrationWorkbench")

app = FastAPI(title="Data Migration Workbench", version="3.5.0")


@app.middleware("http")
async def request_read_cache(request: Request, call_next):
    """On Azure SQL every read is a network round trip: within one request an identical read is served once (a write clears it)."""
    if database_kind() != "mssql":
        return await call_next(request)
    from control_plane import mssql_compat
    with mssql_compat.request_cache():
        return await call_next(request)


@app.middleware("http")
async def enforce_role_policy(request: Request, call_next):
    """Reject state-changing requests from roles that may not perform them (session role, server side)."""
    allowed = allowed_roles_for(request.method, request.url.path)
    _use_target_schema(session_mgr.get_session(request))
    if allowed is not None:
        session = session_mgr.get_session(request)
        # Unauthenticated callers fall through to the route's own login redirect.
        if session is not None and session.get("role") not in allowed:
            detail = f"{session.get('role')} may not perform this action. Allowed: {', '.join(sorted(allowed))}."
            if "text/html" in request.headers.get("accept", ""):
                path = urlparse(request.headers.get("referer", "/")).path or "/"
                return RedirectResponse(url=f"{path}?error=" + quote_plus(detail), status_code=status.HTTP_303_SEE_OTHER)
            return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)
    elif request.method.upper() in {"GET", "HEAD"}:
        # The Platform Admin persona sees platform setup only, never migration data.
        session = session_mgr.get_session(request)
        if session is not None and session.get("role") == "Platform Admin" and not admin_may_view(request.url.path):
            detail = "The Platform Admin sees platform setup only, not migration data."
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(url="/setup?error=" + quote_plus(detail), status_code=status.HTTP_303_SEE_OTHER)
            return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)
        if session is not None and session.get("role") != "Platform Admin" and platform_only_page(request.url.path):
            detail = "Azure, GitHub and target system setup are platform setup, done by a Platform Admin. Your migration uses those settings."
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(url="/setup?error=" + quote_plus(detail), status_code=status.HTTP_303_SEE_OTHER)
            return JSONResponse({"detail": detail}, status_code=status.HTTP_403_FORBIDDEN)
    gate = gated_stage_for(request.method, request.url.path)
    if gate is not None and session_mgr.get_session(request) is not None:
        blocker = _stage_blocker(gate)
        if blocker:
            if "text/html" in request.headers.get("accept", ""):
                path = urlparse(request.headers.get("referer", "/")).path or "/"
                return RedirectResponse(url=f"{path}?error=" + quote_plus(blocker), status_code=status.HTTP_303_SEE_OTHER)
            return JSONResponse({"detail": blocker}, status_code=status.HTTP_409_CONFLICT)
    return await call_next(request)

ui_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.abspath(os.path.join(ui_dir, ".."))

static_dir = os.path.join(ui_dir, "static")
templates_dir = os.path.join(ui_dir, "templates")

os.makedirs(static_dir, exist_ok=True)
os.makedirs(templates_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _register_db_error_handlers() -> None:
    """When the database stays unreachable after the retries, show a clear banner page instead of 'Internal Server Error'."""
    try:
        import pymssql
    except ImportError:  # local SQLite runs have no pymssql
        return

    async def _db_down(request: Request, exc: Exception):
        from control_plane import mssql_compat
        mssql_compat.health.fail(exc)
        page = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Database unreachable</title>"
                "<body style='font-family:Inter,system-ui,sans-serif;background:#F4F7FB;margin:0'>"
                "<div role='alert' style='background:#FEF2F2;color:#7F1D1D;border-bottom:3px solid #E5484D;padding:14px 20px;font-weight:700'>"
                f"Database connection failed after {mssql_compat.RETRIES} attempts</div>"
                "<main style='max-width:620px;margin:40px auto;padding:0 16px;color:#0F172A'><h1 style='font-size:20px'>The platform could not reach its database</h1>"
                "<p style='line-height:1.6'>Nothing was lost: no change from this page was saved. Check that the Azure SQL database is running (a paused serverless "
                "database wakes on the first connection) and that this machine's IP is allowed in the database firewall, then try again.</p>"
                "<p><a href='' style='display:inline-block;background:#0B6FC4;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none;font-weight:700'>Try again</a></p></main>")
        return HTMLResponse(page, status_code=503)

    app.add_exception_handler(pymssql.InterfaceError, _db_down)
    app.add_exception_handler(pymssql.OperationalError, _db_down)


_register_db_error_handlers()
templates = Jinja2Templates(directory=templates_dir)
canonical_overlay.ACTIVE_MIGRATION = lambda: str(state_store.questionnaire_data.get("migration_id") or "") or None
def _asset_version(name: str) -> str:
    """Modification time of a static file, appended to its URL so an edited stylesheet is never served from the browser cache."""
    try:
        return str(int(os.path.getmtime(os.path.join(os.path.dirname(templates_dir), "static", name))))
    except OSError:
        return "0"


templates.env.globals["asset_v"] = _asset_version


def _connection_issues() -> list:
    from control_plane import connection_health
    return connection_health.failing()


templates.env.globals["connection_issues"] = _connection_issues
templates.env.filters["mapping_kind"] = lambda rule, field=None: SchemaMapperAgent.mapping_kind(rule, field)

schema_mapper = SchemaMapperAgent()
adf_client = AzureDataFactoryClient()


# A source file is decided once confirmed, rejected, or classified as out of scope / a deferred relationship file (NOT_REQUIRED).
DECIDED_FILE_STATUSES = {"APPROVED", "REJECTED", "NOT_REQUIRED"}


def get_current_user(request: Request) -> Dict[str, str]:
    session = session_mgr.get_session(request)
    if not session:
        # 303, not 307: a signed-out POST (a button clicked after the session ended) must land on the sign-in page as a GET, not re-post its form there
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login?notice=" + quote_plus("Your session ended. Sign in again.")})
    return session


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


def _use_target_schema(session) -> None:
    """The live schema this request works on: the one a Platform Admin picked while setting targets up, else the migration's own.
    The connection (server, database, login) is the platform's; every migration chooses its schema (orion, sparc, ...)."""
    if session is not None and session.get("role") == "Platform Admin":
        if "target_schema" not in session:  # first request of this sign-in: start on the confirmed target, if there is one (a pick on Target system overrides it)
            conn = None
            try:
                conn = get_db_connection()
                session["target_schema"] = target_connector.latest_confirmed_schema(conn)
                conn.commit()
            except Exception:  # noqa: BLE001  advisory default: never block a request
                session["target_schema"] = ""
            finally:
                if conn is not None:
                    conn.close()
        live_target.set_current_schema(session.get("target_schema", ""))
    else:
        live_target.set_current_schema((state_store.questionnaire_data.get("migration_setup") or {}).get("target_schema", ""))


def _stage_blocker(stage: str) -> str:
    """Why an action of `stage` cannot run yet: the first earlier stage that is not done (empty when it may run)."""
    try:
        stages = workflow_progress().get("stages", [])
    except Exception:
        logger.exception("Could not check the stage gate")
        return ""
    for item in stages:
        if item["number"] >= stage:
            break
        if item["state"] != "done":
            return (f"Stage {item['number']} {item['title']} is not complete yet. Finish it before stage {stage} actions "
                    f"(for example approve and confirm every business rule).")
    return ""


def workflow_progress() -> dict:
    """Counts and the single next action shown in the navigation and dashboard."""
    setup = state_store.questionnaire_data.get("migration_setup", {})
    scope = [e for e in setup.get("entities", []) if e in set(P0_ENTITIES)]
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    files = [rec for entity, group in getattr(state_store, "entity_source_files", {}).items() for rec in group.values()]
    files_total = len(files)
    files_confirmed = sum(1 for r in files if r.get("inference_approval_status") == "APPROVED")
    files_pending = sum(1 for r in files if r.get("inference_approval_status") not in DECIDED_FILE_STATUSES)
    proposals_pending = mappings_approved = changes_open = rfi_open = target_pending = 0
    target_provisioned = False
    validation_status = validation_when = ""
    target_validation_status = ""
    rules_approved = rules_draft = target_approved = runs_n = 0
    codes_pending = 0
    codes_needed = False
    er_pending = 0
    roles_needed = False
    sql_ready = published = False
    run_status = ""
    data_plane: Dict[str, Any] = {}
    conn = get_db_connection()
    try:
        _rules = transformation_rules.list_rules(conn)
        rules_approved = sum(1 for r in _rules if r["status"] == "APPROVED")
        rules_draft = sum(1 for r in _rules if r["status"] == "DRAFT")
        er_pending = entity_resolution.pending(conn, migration_id)
        roles_needed = any(e in scope and not _has_entity_file(e) and not entity_resolution.derivation(conn, migration_id, e) for e in entity_resolution.recipes())
        _cc = code_mappings.counts(conn, migration_id)
        codes_pending = _cc["PROPOSED"]
        if not sum(_cc.values()):  # nothing profiled yet: needed once a coded canonical field has an approved mapping
            _coded = canonical_model.coded_fields(canonical_model.current())
            codes_needed = any(r["canonical_field"] in _coded.get(r["entity_name"], {}) for r in conn.execute(
                "SELECT entity_name, canonical_field FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchall())
        sql_ready = bool(package_contract.list_packages(conn, migration_id))
        runs_n = 1 if run_monitor.succeeded(conn, migration_id) else 0
        run_status = ((run_monitor.stored(conn, migration_id) or {}).get("status") or "")
        _tv = target_validation.latest(conn, migration_id)
        target_validation_status = ""
        if _tv:
            target_validation_status = validation_scenarios.effective_status(
                _tv["checks"], validation_scenarios.list_exceptions(conn, migration_id, _tv["run_id"]))  # stage 09 is done when the real run's log shows the pipeline succeeded
        _git, _pkg = git_connection.get_config(conn), artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
        published = bool(_git and _pkg and _git.get("last_artifact_id") == _pkg["artifact_id"])  # the latest package is the one that was published
        _xart, _xpkg = _xodus_package(conn, migration_id)
        if _xpkg:  # stable-pipeline package: 07 = package built; 08 = pull request merged and deployment check passed for this version;
            # 10 = the batch was validated, signed off, promoted and validated in the live tables
            _v = _xpkg["manifest"]["version"]
            _dc = deployment_check.latest(conn, migration_id)
            # The package must carry the approved code values (canonical model v2): a newly decided value makes it stale until regenerated.
            sql_ready = not _code_map_stale(conn, migration_id, _xpkg["manifest"])
            _adf_now = _adf_status(conn, migration_id)
            if _adf_now and _adf_now["stale_reasons"]:
                sql_ready = False  # the approved mappings or rules changed since the package was built: build it again
            published = bool(_merged_pr(conn, migration_id, _v)) and bool(_dc and _dc.get("version") == _v and _dc.get("status") == "PASS")
            _promo = adf_runs.latest(conn, migration_id, "PL_XODUS_Promote")
            data_plane = {"xodus": True, "promoted": bool(_promo and _promo["status"] == "SUCCEEDED"),
                          "live_validation": (live_validation_status(conn, migration_id) or {}).get("status", "")}
        proposals_pending = conn.execute(
            "SELECT COUNT(*) FROM mapping_suggestions WHERE migration_id = ? AND status = 'PENDING'", (migration_id,)).fetchone()[0]
        mappings_approved = conn.execute(
            "SELECT COUNT(*) FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchone()[0]
        if scope:
            marks = ",".join("?" for _ in scope)
            rfi_open = conn.execute(
                f"SELECT COUNT(*) FROM mapping_information_requests WHERE migration_id = ? AND status = 'OPEN' AND entity_name IN ({marks})",
                (migration_id, *scope)).fetchone()[0]
        changes_open = conn.execute(
            "SELECT COUNT(*) FROM canonical_change_requests WHERE migration_id = ? AND status IN ('OPEN','DEFERRED')", (migration_id,)).fetchone()[0]
        target_provisioned = target_connector.is_confirmed(target_connector.get_config(conn))
        _tm = target_mapping.list_mappings(conn, migration_id, target_connector.spec(conn))
        target_pending = len([m for m in _tm if m["status"] == "PROPOSED" and m["canonical_field"]])
        target_approved = len([m for m in _tm if m["status"] == "APPROVED"])
        last_val = conn.execute(
            "SELECT run_id, status, results_json, executed_utc FROM validation_runs WHERE migration_id = ? ORDER BY executed_utc DESC LIMIT 1", (migration_id,)).fetchone()
        if last_val and _validation_stale(conn, migration_id, last_val["executed_utc"]):
            validation_status, validation_when = "STALE", str(last_val["executed_utc"])[:10]
            last_val = None  # mappings changed since this run: it must be run again before stage 05 counts as done
        if last_val:
            validation_status, validation_when = last_val["status"], str(last_val["executed_utc"])[:10]
            # Findings accepted through an approved exception no longer hold the workflow back (same rule as the Validation page).
            try:
                checks = json.loads(last_val["results_json"]).get("checks", [])
                excs = validation_scenarios.list_exceptions(conn, migration_id, last_val["run_id"])
                validation_status = validation_scenarios.effective_status(checks, excs)
                accepted = sum(1 for e in excs if e["status"] == "APPROVED")
                if accepted and validation_status == "PASS" and last_val["status"] != "PASS":
                    validation_when += f" · {accepted} accepted exception(s)"
            except (ValueError, TypeError, KeyError):
                pass
    except Exception:
        logger.exception("Could not read workflow progress counts")
    finally:
        conn.close()
    if not scope:
        nxt = {"label": "Set up the migration", "detail": "Choose the entities in scope and register knowledge.", "href": "/migration-setup", "step": "setup"}
    elif files_total == 0:
        nxt = {"label": "Add source files", "detail": "Upload the source files or load them from storage.", "href": "/entity-sample-files", "step": "discover"}
    elif files_pending:
        nxt = {"label": f"Confirm {files_pending} source file{'s' if files_pending != 1 else ''}", "detail": "Review the inferred entity for each file.", "href": "/discovery", "step": "discover"}
    elif proposals_pending:
        nxt = {"label": f"Review {proposals_pending} mapping proposal{'s' if proposals_pending != 1 else ''}", "detail": "Approve or reject the suggested source-to-canonical mappings.", "href": "/agent-pipeline", "step": "map"}
    elif mappings_approved == 0:
        nxt = {"label": "Generate mapping proposals", "detail": "Profile the confirmed files and create review items.", "href": "/agent-pipeline", "step": "map"}
    elif rfi_open:
        nxt = {"label": f"Answer {rfi_open} information request{'s' if rfi_open != 1 else ''}", "detail": "Required canonical fields still have no source.", "href": "/agent-pipeline#information-requests", "step": "map"}
    elif er_pending or roles_needed:
        nxt = {"label": f"Decide {er_pending} match rule{'s' if er_pending != 1 else ''}" if er_pending else "Decide where account roles come from",
               "detail": "Entity resolution: which source rows are the same record, and who holds each account.", "href": "/entity-resolution", "step": "map"}
    elif codes_pending or codes_needed:
        nxt = {"label": f"Map {codes_pending} code value{'s' if codes_pending != 1 else ''}" if codes_pending else "Profile the code values",
               "detail": "Each source value of a coded field needs a canonical code.", "href": "/code-sets", "step": "map"}
    elif not target_provisioned:
        nxt = {"label": "Confirm the target system", "detail": "One-time platform setup: configure, infer and confirm the target before target mapping.", "href": "/target-setup", "step": "map"}
    else:
        nxt = {"label": "Review validation results", "detail": "All mappings are approved; check keys, relationships and exceptions.", "href": "/reconciliation", "step": "validate"}
    progress = {
        "scope": scope, "files_total": files_total, "files_confirmed": files_confirmed, "files_pending": files_pending,
        "proposals_pending": proposals_pending, "mappings_approved": mappings_approved, "rfi_open": rfi_open,
        "changes_open": changes_open, "next": nxt, "target_provisioned": target_provisioned, "target_pending": target_pending,
        "validation_status": validation_status, "validation_when": validation_when, "target_validation_status": target_validation_status,
        "run_status": run_status, "data_plane": data_plane, "codes_pending": codes_pending, "codes_needed": codes_needed, "er_pending": er_pending, "roles_needed": roles_needed,
    }
    progress.update(build_workflow_view(progress, setup, migration_id))
    progress.update(build_stage_view(progress, rules_approved, rules_draft, target_approved, sql_ready, runs_n, published))
    _current = next((s["number"] for s in progress.get("stages", []) if s["state"] in ("current", "failed")), None)
    _later = {"05": ("Review mapping validation", "Run the validation checks on the approved mappings.", "/reconciliation", "validate"),
              "06": ("Approve the target mappings", "Map each canonical field to the target.", "/target-mapping", "map"),
              "07": ("Generate the pipeline package", "Build the pipelines and SQL from the approved mappings.", "/transformation-sql", "map"),
              "08": ("Publish and check the deployment", "Merge the pull request, run the setup pipeline and the deployment check.", "/publish-artifacts#data-plane", "validate"),
              "09": ("Start a batch", "Run the pipelines on the source files.", "/execution#start-run", "validate"),
              "10": ("Validate and promote the batch", "Target validation, sign-off, promotion and the live checks.", "/validation-rules#target-validation", "validate")}
    if _current in _later and (nxt.get("step") == "validate" or _current >= "07"):  # once mapping is done, point at the stage that is next
        label, detail, href, step = _later[_current]
        progress["next"] = {"label": label, "detail": detail, "href": href, "step": step}
    progress["activity"] = activity_feed.recent_activity(migration_id)
    return progress


def build_stage_view(p: dict, rules_approved: int, rules_draft: int, target_approved: int, sql_ready: bool, runs_n: int, published: bool = False) -> dict:
    """The ten pipeline stages in order, each done / current / todo, so the rail counts the same stages it lists.
    A stage is done when its own work exists and every earlier stage is done; the first unfinished stage is the current one."""
    mapped = (p["mappings_approved"] > 0 and p["proposals_pending"] == 0 and p["rfi_open"] == 0 and not p.get("er_pending")
              and not p.get("roles_needed"))
    done_flags = [
        ("01", "Loader & Inspector", p["files_total"] > 0),
        ("02", "Discovery", p["files_total"] > 0 and p["files_pending"] == 0),
        ("03", "Business Rules", rules_approved > 0 and rules_draft == 0 and not p.get("codes_pending") and not p.get("codes_needed")),
        ("04", "Schema Mapper", mapped),
        ("05", "Mapping Validation Review", p["validation_status"] == "PASS"),
        ("06", "Target mapping", p["target_provisioned"] and target_approved > 0 and p["target_pending"] == 0),
        ("07", "Transformation SQL", sql_ready),
        ("08", "Publish artifacts", published),
        ("09", "ADF Execution", runs_n > 0),
        ("10", "Target validation", runs_n > 0 and p.get("target_validation_status") == "PASS"
         and (not p.get("data_plane", {}).get("xodus") or (p["data_plane"]["promoted"] and p["data_plane"]["live_validation"] == "PASS"))),
    ]
    # A stage whose own check ran and failed is shown red ("failed"), not just "needs attention".
    failed_stages = {"05": p.get("validation_status") == "FAIL", "09": p.get("run_status") == "FAILED", "10": p.get("target_validation_status") == "FAIL"}
    stages, current_seen = [], False
    for num, title, ok in done_flags:
        if current_seen:
            state = "todo"  # nothing after the first unfinished stage counts as started
        elif ok:
            state = "done"
        else:
            state, current_seen = ("failed" if failed_stages.get(num) else "current"), True
        stages.append({"number": num, "title": title, "state": state})
    cur = next((s for s in stages if s["state"] in ("current", "failed")), None)
    return {"stages": stages, "stages_done": sum(1 for s in stages if s["state"] == "done"),
            "stage_current": cur["number"] + " " + cur["title"] if cur else "All stages complete"}


def build_workflow_view(p: dict, setup: dict, migration_id: str) -> dict:
    """Stepper, per-step sub-tasks, header facts and status chip derived from the progress counts."""
    def task(label, state, detail=""):
        return {"label": label, "state": state, "detail": detail}

    configured = bool(p["scope"])
    discover_done = p["files_total"] > 0 and p["files_pending"] == 0
    map_done = p["mappings_approved"] > 0 and p["proposals_pending"] == 0 and p["rfi_open"] == 0 and p["target_provisioned"]
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
            task("Target system confirmed", "complete" if p["target_provisioned"] else "needs_you", "Platform-level, one time"),
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


def _azure_cfg_for_display():
    """The single Azure connection record (identifiers only, no secret) for pages that show where source data lives."""
    conn = get_db_connection()
    try:
        return azure_connection.get_config(conn)
    except Exception:
        return None
    finally:
        conn.close()


def _target_scope() -> List[str]:
    """What this migration populates in the target (the user's choice), for display."""
    setup = state_store.questionnaire_data.get("migration_setup") or {}
    return list(setup.get("target_entities") or [])


def _supporting_scope() -> List[str]:
    """Canonical entities added for the selection (not chosen by the user), for display."""
    setup = state_store.questionnaire_data.get("migration_setup") or {}
    return [e for e in setup.get("entities", []) if e not in _target_scope()]


def _target_entity_choices(conn=None) -> List[Dict[str, Any]]:
    """What the confirmed target system receives, read from its inferred model: one choice per entity (with its table), plus a
    greyed choice for each target table no canonical entity matches yet. Empty until a target is confirmed. Nothing is listed
    from a fixed set of names, so a different target system gives a different list."""
    own = conn is None
    conn = conn or get_db_connection()
    try:
        cfg = target_connector.get_config(conn)
        spec = target_connector.spec(conn)
    finally:
        if own:
            conn.close()
    ents = (spec or {}).get("entities") or {}
    out: List[Dict[str, Any]] = [{"name": e, "table": str(b.get("target_table") or ""), "selectable": e in P0_ENTITIES, "reason": ""} for e, b in ents.items()]
    out.sort(key=lambda ch: (P0_ENTITIES.index(ch["name"]) if ch["name"] in P0_ENTITIES else len(P0_ENTITIES), ch["name"]))
    model = (cfg or {}).get("model") or {}
    unmatched = list(model.get("unassigned_tables") or [])
    everything = [t for body in ents.values() for t in (body.get("table_columns") or {})] + unmatched
    for table in unmatched:
        out.append({"name": target_inference.entity_name_for_table(table, everything), "table": table, "selectable": False,
                    "reason": "No canonical entity matches this table yet. A Platform Admin can add one under Canonical model."})
    return out


def render_template_with_context(request: Request, template_name: str, extra_context: dict):
    base_context = {
        "request": request,
        "pilot_entities": list(P0_ENTITIES),
        "target_scope": _target_scope(), "supporting_scope": _supporting_scope(),
        "migration_id": state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"),
        "firm_name": state_store.questionnaire_data.get("firm_name", "New Migration"),
        "adls_connected": state_store.adls_connected,
        "adls_container": state_store.adls_container,
        "kernel_specs": getattr(state_store, "kernel_specs", {}),
        "progress": workflow_progress(),
        "demo_mode": session_mgr.demo_mode,
    }
    _sess = session_mgr.get_session(request)
    if _sess:
        _allowed = session_mgr.assigned_roles(_sess.get("email", "")) or {_sess.get("role")}
        base_context["persona_options"] = [r for r in session_mgr.ALL_ROLES if r in _allowed]
        base_context["active_persona"] = _sess.get("role")
        # Actions this persona may not perform (the same POST policy the server enforces): the page shows those forms disabled.
        base_context["blocked_post_patterns"] = base64.b64encode(json.dumps(
            [pat for pat, roles in POST_POLICY if _sess.get("role") not in roles]).encode("utf-8")).decode("ascii")  # encoded: not page text
        base_context["trole"] = _TEMPLATE_ROLE.get(_sess.get("role"), "IL")
        if _sess.get("role") == "Platform Admin":
            # Platform pages only: no migration name, id or pipeline progress in the page frame.
            base_context.update({"platform_only": True, "progress": {}, "migration_id": "", "firm_name": ""})
    base_context.update(extra_context)
    return templates.TemplateResponse(request=request, name=template_name, context=base_context)


# --- AUTHENTICATION ROUTES ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = None, notice: str = None):
    demo_user = next(iter(session_mgr.authorized_users.values()), None) if session_mgr.demo_mode else None
    demo_email = next(iter(session_mgr.authorized_users.keys()), "") if session_mgr.demo_mode else ""
    return templates.TemplateResponse(request=request, name="login.html", context={
        "error": error,
        "notice": notice,
        "demo_mode": bool(demo_user),
        "demo_email": demo_email,
    })


@app.post("/login")
async def process_login(email: str = Form(...), password: str = Form(...)):
    # Authentication and persona selection are intentionally separate steps.
    session_id = session_mgr.authenticate(email, password, session_mgr.default_role_for(email))
    if not session_id:
        return RedirectResponse(url="/login?error=" + quote_plus(session_mgr.last_error), status_code=status.HTTP_303_SEE_OTHER)
    response = RedirectResponse(url="/suite", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="session_id", value=session_id, httponly=True, samesite="lax",
                        secure=os.getenv("XODUS_SECURE_COOKIES", "") == "1")  # HTTPS only when hosted behind Azure's HTTPS ingress
    return response


@app.get("/healthz")
async def healthz():
    """Public status for the host owner: which commit is running and whether account setup looks right. No emails, no secrets."""
    conn = get_db_connection()
    try:
        users = user_registry.list_users(conn)
        conn.commit()
    finally:
        conn.close()
    boot = user_registry.bootstrap_admin_email()
    reg = next((u for u in users if u["email"] == boot), None) if boot else None
    return {"status": "ok", "database": database_kind(), "commit": (os.getenv("RENDER_GIT_COMMIT") or "unknown")[:7], "registration_page": True,
            "bootstrap_admin_configured": bool(boot), "bootstrap_admin_is_demo_email": bool(boot) and boot in session_mgr.authorized_users,
            "bootstrap_admin_registered": bool(reg), "bootstrap_admin_status": reg["status"] if reg else None,
            "registered_users": len(users), "active_admins": sum(1 for u in users if u["status"] == "ACTIVE" and "Platform Admin" in u["roles"])}


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = "", notice: str = ""):
    return templates.TemplateResponse(request=request, name="register.html", context={"error": error, "notice": notice})


@app.post("/register")
async def register_account(name: str = Form(""), email: str = Form(""), password: str = Form(""), confirm: str = Form(""), website: str = Form("")):
    """Public self-registration. Accounts are pending (no roles) until a Platform Admin approves them; only the bootstrap admin is active at once."""
    def back(key: str, message: str):
        return RedirectResponse(url=f"/register?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)
    if website.strip():  # honeypot: real people never fill this hidden field
        return back("notice", "Registration received.")
    problem = user_registry.validate_registration(name, email, password, confirm)
    if problem:
        return back("error", problem)
    conn = get_db_connection()
    try:
        try:
            result = user_registry.register(conn, name, email, password, set(session_mgr.authorized_users), user_registry.bootstrap_admin_email())
        except ValueError as exc:
            return back("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("USER_REGISTERED", {"email": email.strip().lower(), "name": " ".join(name.split()), "role": "self-registration"}, {"status": result["status"], "bootstrap": result["bootstrap"]})
    if result["status"] == "ACTIVE":
        return RedirectResponse(url="/login?notice=" + quote_plus("Administrator account created. Sign in with the email and password you just chose."), status_code=status.HTTP_303_SEE_OTHER)
    return back("notice", "Registration received. A Platform Admin must approve it and assign your roles before you can sign in.")


@app.get("/api/admin/storage-status")
async def storage_status(request: Request):
    """Platform Admin only: which database is live and what it holds (table names, row counts, stored-file totals). No values are returned."""
    user = get_current_user(request)
    if user.get("role") not in PLATFORM_ROLES:
        raise HTTPException(status_code=403, detail="Only a Platform Admin can view storage status.")
    conn = get_db_connection()
    try:
        if using_postgres():
            schema = "SCHEMA_NAME()" if database_kind() == "mssql" else "current_schema()"
            names = [r[0] for r in conn.execute(f"SELECT table_name FROM information_schema.tables WHERE table_schema = {schema} ORDER BY table_name").fetchall()]
        else:
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
        tables = {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
        files = None
        if "stored_files" in tables:
            r = conn.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM stored_files").fetchone()
            files = {"count": r[0], "total_bytes": int(r[1])}
        events = {}
        if "workflow_events" in tables:
            events = {r[0]: r[1] for r in conn.execute("SELECT event_type, COUNT(*) FROM workflow_events GROUP BY event_type ORDER BY event_type").fetchall()}
        users = {}
        if "app_users" in tables:
            users = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM app_users GROUP BY status").fetchall()}
        conn.commit()
    finally:
        conn.close()
    return {"database": database_kind(), "tables": tables, "stored_files": files,
            "workflow_events_by_type": events, "registered_users_by_status": users,
            "active_migration_id": state_store.questionnaire_data.get("migration_id")}


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    if user.get("role") not in PLATFORM_ROLES:
        return RedirectResponse(url="/?error=" + quote_plus("Only a Platform Admin manages users."), status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db_connection()
    try:
        users = user_registry.list_users(conn)
        events = user_registry.list_events(conn, 50)
        conn.commit()
    finally:
        conn.close()
    demo = [{"email": e, "name": v["name"], "roles": v["roles"], "has_password": bool(v["password_sha256"])} for e, v in session_mgr.authorized_users.items()]
    return render_template_with_context(request, "admin_users.html", {
        "space": "gov",
        "page_title": "Users & access", "user_name": user["name"], "user_role": user["role"], "users": users, "demo_users": demo, "events": events,
        "all_roles": user_registry.ALL_ROLES, "notice": notice, "error": error})


@app.post("/api/users/{email}/decision")
async def decide_user(email: str, request: Request, action: str = Form(...)):
    actor = get_current_user(request)
    form = await request.form()
    conn = get_db_connection()
    try:
        try:
            updated = user_registry.decide(conn, actor["email"], email, action, [str(r) for r in form.getlist("roles")])
        except ValueError as exc:
            return RedirectResponse(url="/admin/users?error=" + quote_plus(str(exc)), status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    for sid, sess in list(session_mgr.active_sessions.items()):  # a changed or disabled account must not keep an old session
        if sess.get("email") == updated["email"]:
            del session_mgr.active_sessions[sid]
    _target_setup_audit("USER_ACCESS_CHANGED", actor, {"target": updated["email"], "action": action.upper(), "status": updated["status"], "roles": updated["roles"]})
    return RedirectResponse(url="/admin/users?notice=" + quote_plus(f"{updated['email']}: {updated['status']}, roles {', '.join(updated['roles']) or 'none'}."), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/users/{email}/delete")
async def delete_user_account(email: str, request: Request):
    """Delete a pending or disabled account (a Platform Admin only). The access history keeps a record of it."""
    actor = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            removed = user_registry.delete_user(conn, actor["email"], email)
        except ValueError as exc:
            return RedirectResponse(url="/admin/users?error=" + quote_plus(str(exc)), status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    for sid, sess in list(session_mgr.active_sessions.items()):  # a deleted account must not keep a session
        if sess.get("email") == removed["email"]:
            del session_mgr.active_sessions[sid]
    _target_setup_audit("USER_ACCOUNT_DELETED", actor, {"target": removed["email"], "status": removed["status"], "roles": removed["roles"]})
    return RedirectResponse(url="/admin/users?notice=" + quote_plus(f"{removed['email']} deleted."), status_code=status.HTTP_303_SEE_OTHER)


_TEMPLATE_ROLE = {"Platform Admin": "PA", "IMO Lead": "IL", "Migration Analyst": "MA", "Domain SME": "MA",
                  "Validation Specialist": "VS", "Compliance Reviewer": "CR"}
_SPACE_LINKS = {"index.html": "/suite", "data-migration.html": "/migrations", "deal-room.html": "/deal-room",
                "governance.html": "/governance", "tenant.html": "/tenant"}


def _space_page(request: Request, filename: str, live_note: str = "", patches=()) -> HTMLResponse:
    """Serve a supplied suite-template space (Deal Room, Governance) behind the same sign-in, with links pointed at this app."""
    user = get_current_user(request)
    html = (Path(__file__).parent / "space_pages" / filename).read_text(encoding="utf-8")
    html = re.sub(r"(?<![\w/.-])(index|data-migration|deal-room|governance|tenant)\.html", lambda m: _SPACE_LINKS[m.group(0)], html)
    for old, new in patches:
        html = html.replace(old, new, 1)
    role = _TEMPLATE_ROLE.get(user.get("role"), "IL")
    html = html.replace("<head>", f'<head><script>try{{localStorage.setItem("xodus.role","{role}")}}catch(e){{}}</script>', 1)
    sess = session_mgr.get_session(request) or {}
    allowed = session_mgr.assigned_roles(sess.get("email", "")) or {user.get("role")}
    options = [r for r in session_mgr.ALL_ROLES if r in allowed]
    # The template's own "Demo: view as" list is replaced by this app's persona selector so there is one source of truth.
    persona_js = ("<script>(function(){var OPTS=" + json.dumps(options) + ",CUR=" + json.dumps(user.get("role")) + ";"
                  "function fix(){var s=document.getElementById('xrole');if(!s||s.dataset.xp)return;s.dataset.xp='1';"
                  "s.innerHTML=OPTS.map(function(r){return '<option'+(r==CUR?' selected':'')+'>'+r+'</option>'}).join('');"
                  "var l=document.querySelector('label[for=xrole]');if(l){l.textContent='Persona';l.title='Your persona scopes the workspace; it does not grant extra permissions.'}"
                  "var c=s.cloneNode(true);s.replaceWith(c);c.addEventListener('change',function(){"
                  "fetch('/persona-selection',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
                  "body:'role='+encodeURIComponent(c.value)+'&next='+encodeURIComponent(location.pathname)}).then(function(){location.reload()})})}"
                  "new MutationObserver(fix).observe(document.documentElement,{childList:true,subtree:true});fix()})()</script>")
    html = re.sub(r"<body[^>]*>", lambda m: m.group(0) + persona_js + live_note, html, count=1)
    return HTMLResponse(html)


@app.get("/deal-room", response_class=HTMLResponse)
async def deal_room_page(request: Request):
    return _space_page(request, "deal-room.html")


def _preview_note(text: str, href: str, link: str) -> str:
    return ('<div style="background:var(--tint-b,#ECF3FB);color:var(--ink,#152536);border-bottom:1px solid var(--line,#DDE7EF);font:600 12.5px Inter,system-ui,sans-serif;padding:8px 20px;display:flex;gap:12px;flex-wrap:wrap;align-items:center">'
            f'<span style="color:var(--muted,#657487)">Live in this build:</span><a href="{href}" style="color:var(--ink-b,#0B5FA5);font-weight:700">{link}</a><span style="color:var(--muted,#657487)">{text}</span></div>')


@app.get("/tenant", response_class=HTMLResponse)
async def tenant_settings_page(request: Request):
    _admin = get_current_user(request)["role"] in PLATFORM_ROLES  # only the Platform Admin can open Users & access
    note = _preview_note("Brand, identity and deployment defaults below are the template preview and are kept in this browser only. Who has access is managed in Governance.",
                         "/admin/users" if _admin else "/governance", "Users &amp; access (Governance) &rarr;" if _admin else "Governance &rarr;")
    return _space_page(request, "tenant.html", note)


@app.get("/migrations", response_class=HTMLResponse)
async def migrations_page(request: Request, notice: str = ""):
    """All migrations of this firm: the active one and earlier ones. A finished migration can seed the next one as a template."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        items = migration_registry.list_migrations(conn, str(state_store.questionnaire_data.get("migration_id", "")))
    finally:
        conn.close()
    return render_template_with_context(request, "migrations.html", {
        "page_title": "Migration workspaces", "user_name": user["name"], "user_role": user["role"], "migrations": items,
        "can_start": user["role"] in SETUP_ROLES, "notice": notice, "lobby": True})


@app.get("/migration-workspaces", response_class=HTMLResponse)
async def migration_workspaces_page(request: Request, view: str = "workspaces"):
    """Template views the live pipeline does not have yet (workspaces list, command center, validation rules, team). Sample data."""
    view = view if view in {"workspaces", "command", "validation", "team"} else "workspaces"
    ws = "null" if view == "workspaces" else "'ws-corecap'"
    note = _preview_note("These workspace views show the template's sample data. The live CoreCap pipeline is in the Data Migration rail.", "/", "Open the live overview &rarr;")
    return _space_page(request, "data-migration.html", note, patches=(("function session(){try{return JSON.parse(XS.get('xodus.dm.session',true)||'null')}catch(e){return null}}",
                                                                       "function session(){return {id: SEEDED[XS.role()] || 'avery.chen', method: 'sso'}}"),  # already signed in to the suite: skip the template's own continue screen
                                                                      ("var V={view:'workspaces',stage:'M3',level:'migration',sec:'profile',ws:null,",
                                                                      f"var V={{view:'{view}',stage:'M3',level:'migration',sec:'profile',ws:{ws},"),))


@app.get("/governance", response_class=HTMLResponse)
async def governance_page(request: Request):
    conn = get_db_connection()
    try:
        waiting = user_registry.pending_count(conn)
        conn.commit()
    finally:
        conn.close()
    is_platform = get_current_user(request)["role"] in PLATFORM_ROLES
    items = [{"href": "/canonical-changes", "title": "Canonical change requests",
              "text": "Proposed changes to the canonical model, reviewed and approved before they take effect.", "badge": ""}]
    if is_platform:
        items.append({"href": "/admin/users", "title": "Users & access",
                      "text": "Who can sign in and which personas they hold. Sign-in requests are approved here.",
                      "badge": f"{waiting} waiting" if waiting else ""})
    # The two live governance areas sit in the page itself, above the gate overview, not in a banner.
    panel = ("var XLIVE=" + json.dumps(items) + ";function liveCard(){return '<div class=\"card\" style=\"margin-bottom:16px\"><h3>Governance administration</h3>"
             "<p class=\"d\">Live in this build. The gate chain, integrity checks and audit trail below are the template preview.</p>"
             "<div style=\"display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px\">'+XLIVE.map(function(i){return '<a href=\"'+i.href+'\" class=\"pol\" style=\"text-decoration:none;color:inherit;border:1px solid var(--line);border-radius:12px;padding:12px 14px\"><div><b>'+i.title+'</b><span>'+i.text+'</span></div>'+(i.badge?'<span class=\"pill queued\">'+i.badge+'</span>':'<span class=\"chip g\">Open</span>')+'</a>'}).join('')+'</div></div>'}\n")
    patches = (("function render(){\n XS.top('gov'", panel + "function render(){\n XS.top('gov'"),
               ("Oversight view</span></div>'\n +'<div class=\"kpis\">", "Oversight view</span></div>'+liveCard()\n +'<div class=\"kpis\">"))
    return _space_page(request, "governance.html", "", patches=patches)


@app.get("/suite", response_class=HTMLResponse)
async def suite_home(request: Request):
    """Signed-in suite landing: the same session opens each space. Only Data Migration is live in this pilot."""
    user = get_current_user(request)
    return render_template_with_context(request, "suite_home.html", {"user_name": user["name"], "user_role": user["role"]})


@app.get("/continue-data-migration", response_class=HTMLResponse)
async def continue_data_migration(request: Request):
    """Entry screen for the Data Migration space: same session, no second password."""
    user = get_current_user(request)
    return render_template_with_context(request, "continue_data_migration.html", {"user_name": user["name"], "user_role": user["role"]})


@app.post("/persona-selection")
async def select_persona(request: Request, role: str = Form(...), next: str = Form("/suite")):
    user = get_current_user(request)
    if role not in (session_mgr.assigned_roles(user["email"]) or {user["role"]}):
        raise HTTPException(status_code=403, detail="The selected persona is not assigned to this user.")
    session_id = request.cookies.get("session_id")
    if not session_id or not session_mgr.select_role(session_id, role):
        raise HTTPException(status_code=403, detail="The selected persona is not available for this session.")
    # Persona switching returns to the page the user was on; only same-site paths are accepted.
    target = next if next.startswith("/") and not next.startswith("//") and "\\" not in next else "/suite"
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


def _target_connection_status() -> Dict[str, Any]:
    """Setup-page view of the target: one-time connection configuration, inference and confirmation state."""
    conn = get_db_connection()
    try:
        cfg = target_connector.get_config(conn)
        conn.commit()
    finally:
        conn.close()
    return {"config": cfg}


def _platform_setup_defaults() -> Dict[str, str]:
    """Migration setup values that come from platform setup: the confirmed target, the firm contract and the source storage.
    A key is present only when platform setup supplies it; the migration form shows those read-only and saving keeps them."""
    conn = get_db_connection()
    try:
        tcfg = target_connector.get_config(conn)
        az = azure_connection.get_config(conn)
        conn.commit()
    finally:
        conn.close()
    out: Dict[str, str] = {}
    if target_connector.is_confirmed(tcfg):
        out["target_system"] = tcfg["target_name"]
        if tcfg.get("owner"):
            out["target_owner"] = tcfg["owner"]
    try:
        out["canonical_version"] = load_firm_config().canonical_contract_ref
    except FirmConfigError:
        pass
    account = (az or {}).get("storage_account") or blob_source.account_name(blob_source.connection_string())
    if account:  # the container the stages read: platform setup's, else the server default
        out["source_storage"] = f"{account}/{(az or {}).get('source_container') or _blob_container()}"
    return out


def _azure_page(request: Request, user, notice: str = "", error: str = "", preview: str = "", lines: int = 25):
    conn = get_db_connection()
    try:
        cfg = azure_connection.get_config(conn)
        git_cfg = git_connection.get_config(conn)
        sql_cfg = sql_target.get_config(conn)
        live_cfg = live_target.get_config(conn)
        conn.commit()
    finally:
        conn.close()
    return render_template_with_context(request, "azure_setup.html", {
        "live": live_cfg or {},
        "sqlt": sql_cfg or {}, "sqlt_default_env": sql_target.DEFAULT_ENV, "sqlt_default_schema": sql_target.DEFAULT_SCHEMA,
        "git": git_cfg or {}, "git_default_env": git_connection.DEFAULT_TOKEN_ENV, "git_default_folder": git_connection.DEFAULT_FOLDER,
        "git_default_branch": git_connection.DEFAULT_BRANCH,
        "page_title": "Azure connection", "user_name": user["name"], "user_role": user["role"], "cfg": cfg or {},
        "fields": azure_connection.FIELDS, "can_edit_azure": user["role"] in PLATFORM_ROLES,
        "default_secret_env": azure_connection.DEFAULT_SECRET_ENV, "notice": notice, "error": error,
        "blob": _blob_panel(with_files=False), "can_seed_blob": user["role"] in PLATFORM_ROLES,
        "preview": _blob_preview(preview, lines) if user["role"] in DISCOVERY_ROLES else None, "can_preview": user["role"] in DISCOVERY_ROLES,
    })


def _blob_preview(path: str, lines: int = 25):
    """Read-only preview of one blob for the Azure connection page; a problem is shown instead of failing the page."""
    if not path:
        return None
    try:
        return blob_source.preview(_blob_container(), path, max_lines=max(5, min(int(lines or 25), 500)))
    except Exception as exc:
        return {"name": path, "error": str(exc)[:200] if isinstance(exc, ValueError) else f"Could not read the file: {type(exc).__name__}"}


@app.get("/azure-setup", response_class=HTMLResponse)
async def azure_setup_page(request: Request, notice: str = "", error: str = "", preview: str = "", lines: int = 25):
    return _azure_page(request, get_current_user(request), notice, error, preview, lines)


@app.post("/api/azure/connection")
async def save_azure_connection(request: Request):
    user = get_current_user(request)
    form = await request.form()
    conn = get_db_connection()
    try:
        try:
            existing = azure_connection.get_config(conn) or {}  # fields no longer on the form keep their stored value
            azure_connection.save(conn, user["name"], {k: str(form.get(k)) if k in form else str(existing.get(k) or "") for k in azure_connection.FIELDS})
        except ValueError as exc:
            return RedirectResponse(url="/azure-setup?" + urlencode({"error": str(exc)}), status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("AZURE_CONNECTION_SAVED", user, {"factory": str(form.get("factory_name", "")).strip()})
    return RedirectResponse(url="/azure-setup?" + urlencode({"notice": "Settings saved. Test the connection next."}), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/azure/connection/test")
async def test_azure_connection(request: Request):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = azure_connection.test_connection(conn)
        except ValueError as exc:
            return RedirectResponse(url="/azure-setup?" + urlencode({"error": str(exc)}), status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("AZURE_CONNECTION_TESTED", user, {"status": cfg["status"]})
    key = "notice" if cfg["status"] == "CONNECTED" else "error"
    return RedirectResponse(url="/azure-setup?" + urlencode({key: cfg["check_detail"]}), status_code=status.HTTP_303_SEE_OTHER)


def _git_back(key: str, msg: str):
    return RedirectResponse(url="/github-setup?" + urlencode({key: msg}), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/github-setup", response_class=HTMLResponse)
async def github_setup_page(request: Request, notice: str = "", error: str = ""):
    """GitHub integration: where the generated ADF package is published. Separate from the Azure connection."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        git_cfg = git_connection.get_config(conn)
        pkg = artifact_store.latest(conn, str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED")), "ADF_PACKAGE")
        conn.commit()
    finally:
        conn.close()
    return render_template_with_context(request, "github_setup.html", {
        "git": git_cfg or {}, "pkg": pkg, "git_default_env": git_connection.DEFAULT_TOKEN_ENV, "git_default_folder": git_connection.DEFAULT_FOLDER,
        "git_default_branch": git_connection.DEFAULT_BRANCH, "page_title": "GitHub integration", "user_name": user["name"], "user_role": user["role"],
        "can_edit_azure": user["role"] in PLATFORM_ROLES, "notice": notice, "error": error})


def _sqlt_back(key: str, msg: str):
    return RedirectResponse(url="/azure-setup?" + urlencode({key: msg}) + "#sqlt", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/sql-target")
async def save_sql_target(request: Request):
    user = get_current_user(request)
    form = await request.form()
    conn = get_db_connection()
    try:
        try:
            sql_target.save(conn, user["name"], {k: str(form.get(k, "")) for k in sql_target.FIELDS})
        except ValueError as exc:
            return _sqlt_back("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("SQL_TARGET_SAVED", user, {"server": str(form.get("server", "")).strip()})
    return _sqlt_back("notice", "SQL settings saved. Test the connection next.")


@app.post("/api/sql-target/test")
async def test_sql_target(request: Request):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = sql_target.test_connection(conn)
        except ValueError as exc:
            return _sqlt_back("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("SQL_TARGET_TESTED", user, {"status": cfg["status"]})
    return _sqlt_back("notice" if cfg["status"] == "CONNECTED" else "error", cfg["check_detail"])


@app.post("/api/git/connection")
async def save_git_connection(request: Request):
    user = get_current_user(request)
    form = await request.form()
    conn = get_db_connection()
    try:
        try:
            git_connection.save(conn, user["name"], {k: str(form.get(k, "")) for k in git_connection.FIELDS})
        except ValueError as exc:
            return _git_back("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("GIT_CONNECTION_SAVED", user, {"repository": str(form.get("repository", "")).strip()})
    return _git_back("notice", "GitHub settings saved. Test the connection next.")


@app.post("/api/git/connection/test")
async def test_git_connection(request: Request):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = git_connection.test_connection(conn)
        except ValueError as exc:
            return _git_back("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("GIT_CONNECTION_TESTED", user, {"status": cfg["status"]})
    return _git_back("notice" if cfg["status"] == "CONNECTED" else "error", cfg["check_detail"])


def _publish_back(key: str, msg: str):
    return RedirectResponse(url="/publish-artifacts?" + urlencode({key: msg}), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/publish-artifacts", response_class=HTMLResponse)
async def publish_artifacts_page(request: Request, notice: str = "", error: str = ""):
    """Stage 08: hand the stored ADF package to the data factory owner through the linked GitHub repository."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        git_cfg = git_connection.get_config(conn)
        pkg = artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
        placed, simulated = [], False
        if pkg:
            try:
                rec = artifact_store.read(conn, pkg["artifact_id"])
                with zipfile.ZipFile(io.BytesIO(rec["data"])) as z:
                    names = [n for n in z.namelist() if not n.endswith("/")]
                    simulated = '"simulated": true' in z.read("manifest.json").decode("utf-8") if "manifest.json" in names else False
                placed = sorted(git_connection.layout({"adf_folder": (git_cfg or {}).get("adf_folder") or git_connection.DEFAULT_FOLDER}, migration_id, {n: "" for n in names}))
            except (ValueError, zipfile.BadZipFile) as exc:
                error = error or str(exc)
        adf_status = _adf_status(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    published = bool(git_cfg and pkg and git_cfg.get("last_artifact_id") == pkg["artifact_id"])
    # "Published" must not read as current when the latest package is stale or a different one went out.
    published_older = bool(git_cfg and git_cfg.get("last_artifact_id") and not published) or bool(
        published and adf_status and adf_status["stale_reasons"])
    return render_template_with_context(request, "publish_artifacts.html", {
        "page_title": "Publish artifacts", "user_name": user["name"], "user_role": user["role"], "notice": notice, "error": error,
        "git": git_cfg or {}, "pkg": pkg, "placed": placed, "simulated": simulated, "published": published, "published_older": published_older,
        "can_publish": user["role"] in APPROVAL_ROLES, "dp": _stage_data_plane(migration_id, "publish"), "can_check": user["role"] in TARGET_PREVIEW_ROLES,
    })


@app.get("/api/target-mapping/create-scripts")
async def download_create_scripts(request: Request):
    """One .sql file with the schema/table create scripts from the latest stored ADF package. A person runs it; the pipeline never does."""
    user = get_current_user(request)
    if user["role"] not in TARGET_PREVIEW_ROLES:
        raise HTTPException(status_code=403, detail="Your role cannot download the create scripts.")
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        art = artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
        rec = artifact_store.read(conn, art["artifact_id"]) if art else None
    finally:
        conn.close()
    if not rec:
        raise HTTPException(status_code=404, detail="Generate the ADF package first (07 Transformation SQL).")
    with zipfile.ZipFile(io.BytesIO(rec["data"])) as z:
        parts = [z.read(n).decode("utf-8") for n in ("sql/01_target_tables.sql", "sql/00_staging_tables.sql") if n in z.namelist()]
    text = (f"-- XODUS create scripts for {migration_id} (package {art['artifact_id']} v{art['artifact_version']}).\n"
            "-- Run once, in this order, against the target database. They only create what is missing; nothing is dropped or altered.\n"
            "-- GO separates batches (SSMS / Azure Data Studio); in the Azure portal query editor run each batch on its own.\n\n" + "\n".join(parts))
    return Response(content=text, media_type="application/sql", headers={"Content-Disposition": f'attachment; filename="xodus_create_{migration_id}.sql"'})


@app.post("/api/git/publish")
async def publish_adf_to_git(request: Request):
    """Add the latest stored ADF package to the linked GitHub repository as one commit. The factory owner links the repository in Data Factory.
    With replace=1 the same commit removes the files earlier XODUS packages left (never linked services)."""
    user = get_current_user(request)
    _form = await request.form()
    replace = str(_form.get("replace", "")) == "1"
    prune_linked = replace and str(_form.get("prune_linked", "")) == "1"
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            art = artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
            if not art:
                raise ValueError("Generate the ADF package first (07 Transformation SQL).")
            rec = artifact_store.read(conn, art["artifact_id"])
            with zipfile.ZipFile(io.BytesIO(rec["data"])) as z:
                files = {n: z.read(n).decode("utf-8") for n in z.namelist() if not n.endswith("/")}
            note = " (simulated Azure values)" if '"simulated": true' in files.get("manifest.json", "") else ""
            # The pipeline writes to the run-log table; a missing table is caught here, before anything is deployed or run.
            log_check = sql_target.run_log_status(conn) if "sql/02_run_log.sql" in files else {"checked": False, "exists": None, "detail": ""}
            if log_check["checked"] and not log_check["exists"]:
                raise ValueError(log_check["detail"])
            if "sql/02_run_log.sql" in files and not log_check["checked"]:
                note += " (run-log table not verified: " + log_check["detail"].rstrip(".") + ")"
            result = git_connection.publish(conn, user["name"], migration_id, files,
                                            f"XODUS ADF package {art['artifact_id']} v{art['artifact_version']} for {migration_id}{note}, published by {user['name']}",
                                            artifact_id=art["artifact_id"], replace=replace, prune_linked=prune_linked)
        except (ValueError, zipfile.BadZipFile) as exc:
            return _publish_back("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("ADF_PACKAGE_PUBLISHED_TO_GIT", user, {"artifact_id": art["artifact_id"], "commit": result["commit_sha"], "files": result["files"],
                                                              "repository": result["repository"], "branch": result["branch"], "removed": result["removed"]})
    gone = f" and removed {result['removed']} old file(s)" if replace else ""
    return _publish_back("notice", f"Published {result['files']} file(s) from {art['artifact_id']}{gone} to {result['repository']} ({result['branch']}), commit {result['commit_sha'][:7]}.")


@app.get("/models", response_class=HTMLResponse)
async def models_page(request: Request):
    """Read-only side-by-side view: the supplied target system as it is, and the canonical model the platform maps into."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        cfg = target_connector.get_config(conn)
        conn.commit()
    finally:
        conn.close()
    target, target_error = None, ""
    if not cfg:
        target_error = "The target system is not set up yet (Setup → Target system, then stage 06 Target mapping)."
    elif cfg.get("connector_type") == "azure-sql":
        # An Azure SQL target is read once with a read-only sign-in when it is set up; show the confirmed model, not a live call.
        conn = get_db_connection()
        try:
            spec = target_connector.spec(conn)
            conn.commit()
        finally:
            conn.close()
        if not spec:
            target_error = "The target system is not confirmed yet (Platform setup → Target system)."
        else:
            tables: Dict[str, Dict[str, Any]] = {}
            code_lists: Dict[str, List[str]] = {}
            for entity, body in spec["entities"].items():
                for f in body["fields"]:
                    t = tables.setdefault(f["table"], {"name": f["table"], "description": f"Mapped from the canonical {entity}.", "columns": []})
                    t["columns"].append({"name": f["column"], "type": f.get("type") or "", "required": bool(f.get("required")), "generated": False,
                                         "description": f.get("description") or ""})
                    if f.get("codes"):
                        code_lists[f"{f['table']}.{f['column']}"] = list(f["codes"])
            target = {"system": spec.get("target_system"), "version": spec.get("spec_version"), "tables": sorted(tables.values(), key=lambda t: t["name"]),
                      "code_lists": code_lists}
    else:
        try:
            target = target_connector._http_get(cfg["endpoint"] + "/schema")
        except Exception as exc:  # shown to the reader, not raised
            target_error = f"Could not read the target system: {exc}"
    canonical = []
    try:
        firm = load_firm_config()
        for name, entity in firm.entities.items():
            added = canonical_overlay.active_added_fields(name)
            fields = [{"name": f.name, "type": f.type, "required": not f.nullable, "key": f.is_key, "sensitive": f.sensitive,
                       "description": f.description, "added": False} for f in entity.fields]
            fields += [{"name": n, "type": "", "required": False, "key": False, "sensitive": False, "description": "Added by an approved change request", "added": True}
                       for n in added if n not in {f["name"] for f in fields}]
            canonical.append({"name": name, "grain": entity.grain, "fields": fields})
    except FirmConfigError as exc:
        target_error = target_error or f"Canonical model unavailable: {exc}"
    return render_template_with_context(request, "models.html", {
        "page_title": "Target and canonical models", "user_name": user["name"], "user_role": user["role"],
        "target": target, "target_error": target_error, "canonical": canonical, "target_status": (cfg or {}).get("status", ""),
    })


@app.get("/setup", response_class=HTMLResponse)
async def setup_hub(request: Request):
    """One place for the inputs a migration needs before data moves: configuration, target mapping, firm knowledge."""
    user = get_current_user(request)
    setup = state_store.questionnaire_data.get("migration_setup", {}) or {}
    cfg = _target_connection_status().get("config") or {}
    conn = get_db_connection()
    try:
        az, gt, sq = azure_connection.get_config(conn) or {}, git_connection.get_config(conn) or {}, sql_target.get_config(conn) or {}
        conn.commit()
    finally:
        conn.close()
    return render_template_with_context(request, "setup_hub.html", {
        "page_title": "Platform setup" if user["role"] in PLATFORM_ROLES else "Setup", "user_name": user["name"], "user_role": user["role"],
        "sql_status": str(sq.get("status") or "not set up").lower(), "sql_db": sq.get("database_name", ""), "azure_status": "connected" if az.get("status") == "CONNECTED" else ("saved" if az.get("factory_name") else "not set up"),
        # XODUS does not sign in to Data Factory; a failed sign-in test is not a setup failure
        "azure_factory": az.get("factory_name", ""),
        "git_status": str(gt.get("status") or "not set up").lower(), "git_repo": gt.get("repository", ""), "git_branch": gt.get("branch", ""),
        "configured": bool(setup), "setup_entities": setup.get("entities") or [],
        "target_confirmed": cfg.get("status") == "CONFIRMED",
        "target_system": cfg.get("target_name", "") if user["role"] in PLATFORM_ROLES else setup.get("target_system", ""),  # the admin sees platform values only
        "knowledge_saved": str(state_store.questionnaire_data.get("last_updated") or "Not yet")[:10],
    })


@app.get("/migration-setup", response_class=HTMLResponse)
async def migration_setup_page(request: Request, error: str = "", new_migration: str = "", target_schema: str = ""):
    user = get_current_user(request)
    setup = state_store.questionnaire_data.get("migration_setup", {})
    schemas = _confirmed_target_schemas()
    chosen_schema = target_schema if target_schema in schemas else (setup.get("target_schema") if setup.get("target_schema") in schemas else (schemas[0] if len(schemas) == 1 else ""))
    live_target.set_current_schema(chosen_schema)  # the entity choices below follow this schema's confirmed model
    template_prefill = state_store.questionnaire_data.get("migration_template") or {}
    prefilled = bool(template_prefill) and not setup
    if prefilled:
        setup = template_prefill  # pre-fills the form only; nothing is in scope or approved until the setup is saved
    choices = _target_entity_choices()
    selectable = [c["name"] for c in choices if c["selectable"]]
    chosen = list(setup.get("target_entities") or selectable)  # a new migration starts with everything the target receives ticked
    return render_template_with_context(request, "migration_setup.html", {
        "target_schemas": schemas, "chosen_schema": chosen_schema, "target_choices": choices, "chosen_targets": chosen, "supporting_info": supporting_plan(selectable, selectable) if selectable else {},
        "page_title":"Migration Setup", "user_name":user["name"], "user_role":user["role"], "setup":setup, "prefilled": prefilled,
        "error": error, "can_configure": user["role"] in SETUP_ROLES,
        "target_connection": _target_connection_status(),
        "new_migration": new_migration == "1",
        "knowledge_docs": state_store.firm_knowledge_docs,
        "platform_defaults": _platform_setup_defaults(),
    })


def _confirmed_target_schemas() -> List[str]:
    conn = get_db_connection()
    try:
        out = target_connector.confirmed_schemas(conn)
        conn.commit()
    finally:
        conn.close()
    return out


@app.post("/api/migration/new")
async def start_new_migration(request: Request):
    """Begin a migration. Earlier migrations stay stored but isolated by migration_id. Without a template it is empty;
    from a template it brings stages 01 to 03 (files, entity decisions, knowledge, code sets) and nothing after."""
    user = get_current_user(request)
    form = await request.form()
    template_id = str(form.get("template_id", "")).strip()
    previous_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    migration_id = f"MIG-{uuid.uuid4().hex[:10].upper()}"
    prefill: Dict[str, Any] = {}
    if template_id:
        conn = get_db_connection()
        try:
            prefill = migration_registry.carry_over(conn, template_id)
        finally:
            conn.close()
        if not prefill:
            raise HTTPException(status_code=404, detail="That migration has no saved setup to use as a template.")
    state_store.questionnaire_data["migration_template"] = prefill
    state_store.entity_sample_files.clear()
    state_store.entity_source_files.clear()
    state_store.pipeline_runs.clear()
    state_store.questionnaire_data["mapping_information_requests"] = []
    state_store.questionnaire_data["migration_setup"] = {}
    for stale in ("firm_name", "migration_name", "source_systems"):
        state_store.questionnaire_data[stale] = ""  # the previous migration's name must not label the new one
    state_store.questionnaire_data["migration_id"] = migration_id
    for entity in P0_ENTITIES:
        state_store.entity_scopes[entity] = "OUT_OF_SCOPE"
    state_store.firm_knowledge_docs.clear()  # a new migration starts empty; a template brings its own knowledge assets back below
    context_payload = {"migration_id": migration_id, "migration_setup": {}, "previous_migration_id": previous_id, "migration_template": prefill, "firm_name": "", "migration_name": "", "source_systems": ""}
    publish_event("MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id, context_payload, user["name"])
    publish_event(
        "ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id,
        {"scopes": {entity: "OUT_OF_SCOPE" for entity in P0_ENTITIES}}, user["name"],
    )
    if template_id:
        conn = get_db_connection()
        try:
            migration_registry.copy_foundation(conn, template_id, migration_id, user["name"])
        finally:
            conn.close()
        state_store.hydrate_from_durable_state()
    return RedirectResponse(url="/migration-setup?new_migration=1" + ("&from_template=1" if prefill else ""), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/migration/archive")
async def archive_migration_workspace(request: Request):
    """Retire a workspace from the list. The active workspace cannot be archived; its records stay in the event log."""
    user = get_current_user(request)
    form = await request.form()
    target_id = str(form.get("migration_id", "")).strip()
    if not target_id or target_id == str(state_store.questionnaire_data.get("migration_id", "")):
        raise HTTPException(status_code=400, detail="Open a different workspace first: the active workspace cannot be archived.")
    conn = get_db_connection()
    try:
        known = {m["id"] for m in migration_registry.list_migrations(conn, "")}
    finally:
        conn.close()
    if target_id not in known:
        raise HTTPException(status_code=404, detail="That workspace is not in the list.")
    publish_event("MIGRATION_WORKSPACE_ARCHIVED", target_id, "migration", target_id, {"archived_by": user["name"]}, user["name"])
    return RedirectResponse(url="/migrations?notice=Workspace+archived", status_code=303)


@app.post("/api/migration/open")
async def open_migration_workspace(request: Request):
    """Make an earlier migration the active workspace again: its files, scope, mappings and requests are restored from the durable log.
    Only one migration is worked on at a time, so this switches the workspace for everyone using this server."""
    user = get_current_user(request)
    form = await request.form()
    target_id = str(form.get("migration_id", "")).strip()
    conn = get_db_connection()
    try:
        context = migration_registry.latest_context(conn, target_id)
    finally:
        conn.close()
    if not context:
        raise HTTPException(status_code=404, detail="That migration has no saved setup to open.")
    current_id = str(state_store.questionnaire_data.get("migration_id", ""))
    if target_id != current_id:
        state_store.entity_sample_files.clear()
        state_store.entity_source_files.clear()
        state_store.pipeline_runs.clear()
        state_store.questionnaire_data["mapping_information_requests"] = []
        state_store.questionnaire_data["migration_setup"] = {}
        state_store.questionnaire_data["migration_template"] = {}
        for entity in P0_ENTITIES:
            state_store.entity_scopes[entity] = "OUT_OF_SCOPE"
        # Becoming the latest context event makes this the active migration after a restart too.
        publish_event("MIGRATION_CONTEXT_UPDATED", target_id, "migration", target_id, {**context, "reopened_from": current_id}, user["name"])
        state_store.hydrate_from_durable_state()
        _sync_mapping_information_requests(target_id)
    destination = "/setup" if str(form.get("next", "")) == "/setup" else "/"  # fixed allow-list, never a free URL
    return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/migration/setup")
async def save_migration_setup(request: Request):
    user = get_current_user(request)
    form = await request.form()
    schemas = _confirmed_target_schemas()
    target_schema = str(form.get("target_schema", "")).strip()
    if schemas and target_schema not in schemas:
        raise HTTPException(status_code=400, detail="Choose the target schema this migration loads into: " + ", ".join(schemas) + ".")
    live_target.set_current_schema(target_schema)  # the entities below are the ones this schema's target model receives
    # Explicit scope selection controls downstream P0 workflows.
    _choices = _target_entity_choices()
    allowed_entities = {c["name"] for c in _choices if c["selectable"]}  # the user chooses among what the target receives
    _target_entity_names = [c["name"] for c in _choices if c["selectable"]]
    selected_targets = [entity for entity in form.getlist("entities") if entity in allowed_entities]
    if not selected_targets:
        raise HTTPException(status_code=400, detail="Select at least one target entity to populate: " + ", ".join(sorted(allowed_entities)) + ".")
    try:
        selected_entities = with_supporting_entities(selected_targets, _target_entity_names)  # plus the supporting data those need, added for the user
    except FirmConfigError as exc:
        raise HTTPException(status_code=409, detail=f"Firm configuration is required before migration setup. {exc}")
    setup = {
        "migration_name": str(form.get("migration_name", "")).strip(),
        "source_firm": str(form.get("source_firm", "")).strip(),
        "target_system": str(form.get("target_system", "")).strip(),
        "canonical_version": str(form.get("canonical_version", "")).strip(),
        "target_owner": str(form.get("target_owner", "")).strip(),
        "knowledge_schema_ref": str(form.get("knowledge_schema_ref", "")).strip(),
        "knowledge_rules_ref": str(form.get("knowledge_rules_ref", "")).strip(),
        "knowledge_sme": str(form.get("knowledge_sme", "")).strip(),
        "relationship_notes": str(form.get("relationship_notes", "")).strip(),
        "source_acquisition": str(form.get("source_acquisition", "SAMPLE_FILES")).strip(),
        "source_details": str(form.get("source_details", "")).strip(),
        "target_schema": target_schema,
        "entities": selected_entities,
        "target_entities": [e for e in P0_ENTITIES if e in selected_targets],
        "configured_by": user["name"],
        "configuration_status": "CONFIGURED",
    }
    # Values platform setup supplies win over the form: a migration cannot point at a different target or contract.
    platform_defaults = _platform_setup_defaults()
    for key in ("target_system", "target_owner", "canonical_version"):
        if platform_defaults.get(key):
            setup[key] = platform_defaults[key]
    if setup["source_acquisition"] == "AZURE_CONNECTION" and platform_defaults.get("source_storage") and not setup["source_details"]:
        setup["source_details"] = platform_defaults["source_storage"]
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
    if (state_store.questionnaire_data.get("migration_template") or {}).get("template_migration_id"):
        setup["template_migration_id"] = state_store.questionnaire_data["migration_template"]["template_migration_id"]
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
    previous = set((state_store.questionnaire_data.get("migration_setup") or {}).get("entities") or [])
    state_store.questionnaire_data["migration_setup"] = setup
    for entity in set(P0_ENTITIES):
        # An entity already in scope keeps its reviewed scope; only one newly added starts at discovery.
        if entity not in selected_entities:
            state_store.entity_scopes[entity] = "OUT_OF_SCOPE"
        elif entity not in previous:
            state_store.entity_scopes[entity] = "PENDING_DISCOVERY"
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
        {"scopes": {entity: state_store.entity_scopes[entity] for entity in P0_ENTITIES}},
        user["name"],
    )
    state_store.audit_log.append({"event":"MIGRATION_SETUP_SAVED","actor":user["name"],"timestamp_utc":datetime.now(timezone.utc).isoformat(),"details":{"target_system":setup["target_system"],"source_acquisition":setup["source_acquisition"],"entities":selected_entities}})
    if setup["source_acquisition"] == "AZURE_CONNECTION":
        return RedirectResponse(url="/entity-sample-files?notice=" + quote_plus("Setup saved. Source files are read from the Azure storage the Platform Admin set up."), status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/relationships/review")
async def review_relationship_candidate(request: Request, child: str = Form(...), parent: str = Form(...), decision: str = Form(...), reason: str = Form("")):
    """Analyst/Admin confirms or rejects a detected relationship. Confirmation is evidence only: nothing is joined or merged."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            cross_file.review_relationship(conn, migration_id, child, parent, decision, reason, user["name"])
        except ValueError as exc:
            return RedirectResponse(url="/discovery?error=" + quote_plus(str(exc)) + "#cross-file-analysis", status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("RELATIONSHIP_" + decision.strip().upper(), user, {"child": child, "parent": parent})
    return RedirectResponse(url="/discovery?notice=" + quote_plus(f"Relationship {child} -> {parent} {decision.strip().lower()}.") + "#cross-file-analysis", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/agent-studio", response_class=HTMLResponse)
async def agent_studio(request: Request):
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/agents/analyze/{file_id}")
async def analyze_source_file_with_agents(request: Request, file_id: str):
    user = get_current_user(request)
    setup = state_store.questionnaire_data.get("migration_setup", {})
    selected = [e for e in setup.get("entities", []) if e in set(P0_ENTITIES)]
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
        content = file_store.read_bytes(record["stored_path"])
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
    return RedirectResponse(url="/discovery", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        session_mgr.logout(session_id)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("session_id")
    return response


# --- WORKBENCH MODULE ROUTES ---

def _canonical_notice() -> Optional[Dict[str, Any]]:
    migration_id = str(state_store.questionnaire_data.get("migration_id") or "")
    if not migration_id or migration_id == "MIG-UNCONFIGURED":
        return None
    conn = get_db_connection()
    try:
        out = canonical_overlay.notice(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    return out


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def dashboard_overview(request: Request):
    user = get_current_user(request)
    return render_template_with_context(
        request=request,
        template_name="dashboard.html",
        extra_context={
            "page_title": "Migration Dashboard",
            "canonical_notice": _canonical_notice(),
            "can_adopt": user["role"] in APPROVAL_ROLES,
            "my_queue": build_queue(user["role"], workflow_progress()),
                "notice": request.query_params.get("notice", ""),
            "user_name": user["name"],
            "user_role": user["role"],
            "dashboard": state_store.generated_dashboard,
            "migration_setup": state_store.questionnaire_data.get("migration_setup", {}),
            "azure_cfg": _azure_cfg_for_display(),
            "knowledge_docs_count": len(state_store.firm_knowledge_docs),
            "in_scope_entities": [entity for entity in P0_ENTITIES if entity in state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])],
            "wave_conditions": CohortConditionEngine.get_condition_catalog(),
            "required_fields_by_entity": {entity: SchemaMapperAgent.required_fields(entity) for entity in P0_ENTITIES},
            "entity_approval_summary": {
                entity: {
                    "total": len(state_store.entity_source_files.get(entity, {})),
                    "approved": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") == "APPROVED"),
                    "pending": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") not in DECIDED_FILE_STATUSES),
                    "rejected": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") == "REJECTED"),
                } for entity in state_store.questionnaire_data.get("migration_setup", {}).get("entities", [])
            }
        }
    )


@app.get("/firm-knowledge", response_class=HTMLResponse)
async def firm_knowledge_studio(request: Request, notice: str = "", error: str = "", q: str = ""):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        chunk_stats = {k.split(":", 1)[-1]: v for k, v in knowledge_chunks.stats(conn, migration_id).items() if k.startswith(migration_id + ":")}
        cf = knowledge_sources.get_config(conn)
        domains = [d["host"] for d in knowledge_sources.list_domains(conn)]
        embedder = embeddings.get_embedder()
        search = {"q": q.strip()[:knowledge_index.MAX_QUERY], "hits": []}
        retrieval: Dict[str, Any] = {"embedder": embedder.name}
        try:
            if search["q"]:
                search["hits"] = knowledge_index.retrieve(conn, migration_id, [search["q"]], embedder, k=5, min_score=embeddings.evidence_min_score(embedder) / 2)[0]
            retrieval.update(knowledge_index.feedback_counts(conn, migration_id))
            retrieval["readiness"] = retrieval_tuning.readiness(conn, migration_id, embedder)
            retrieval["models"] = retrieval_tuning.list_models(conn, migration_id)
            retrieval["decisions"] = decision_model.readiness(conn, migration_id)
            retrieval["decision_models"] = decision_model.list_models(conn, migration_id)
        except Exception as exc:  # noqa: BLE001  advisory panel: never break the page
            retrieval["error"] = str(exc)
        conn.commit()
    finally:
        conn.close()
    return render_template_with_context(
        request=request,
        template_name="firm_knowledge.html",
        extra_context={
            "page_title": "Knowledge Assets",
            "user_name": user["name"],
            "user_role": user["role"],
            "can_edit_knowledge": user["role"] in DISCOVERY_ROLES,
            "form": state_store.questionnaire_data,
            "docs": state_store.firm_knowledge_docs,
            "chunk_stats": chunk_stats, "notice": notice, "error": error, "web_domains": domains, "search": search, "retrieval": retrieval,
            "confluence_connected": bool(cf and cf["status"] == "CONNECTED"), "confluence_spaces": (cf or {}).get("allowed_spaces", []),
        }
    )


@app.get("/platform-knowledge", response_class=HTMLResponse)
async def platform_knowledge_page(request: Request, notice: str = "", error: str = "", view: str = ""):
    """Platform-level knowledge shared by every migration; separate from a workspace's firm knowledge."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        assets = platform_knowledge.list_all(conn)
        viewing = {"asset_id": view, "text": platform_knowledge.text_of(conn, view)} if view else None
        sources = {"confluence": knowledge_sources.get_config(conn), "domains": knowledge_sources.list_domains(conn), "default_env": knowledge_sources.DEFAULT_TOKEN_ENV}
        conn.commit()
    finally:
        conn.close()
    return render_template_with_context(request, "platform_knowledge.html", {
        "page_title": "Platform knowledge", "user_name": user["name"], "user_role": user["role"], "assets": assets,
        "categories": platform_knowledge.CATEGORIES, "can_edit": user["role"] in PLATFORM_KNOWLEDGE_ROLES,
        "viewing": viewing, "notice": notice, "error": error, "sources": sources, "is_admin": user["role"] in PLATFORM_ROLES})


def _pk_redirect(key: str, message: str, anchor: str = ""):
    return RedirectResponse(url=f"/platform-knowledge?{key}=" + quote_plus(message) + anchor, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/platform-knowledge/sources/confluence")
async def save_confluence_connection(request: Request):
    """Platform Admin: where Confluence is and how to sign in. The token is never entered here, only the name of the server variable that holds it."""
    user = get_current_user(request)
    form = await request.form()
    conn = get_db_connection()
    try:
        try:
            cfg = knowledge_sources.save_config(conn, user["name"], {k: str(form.get(k, "")) for k in ("base_url", "auth", "email", "token_env_var", "allowed_spaces", "page_cap")})
        except ValueError as exc:
            return _pk_redirect("error", str(exc), "#sources")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("CONFLUENCE_CONNECTION_SAVED", user, {"base_url": cfg["base_url"], "spaces": cfg["allowed_spaces"]})
    return _pk_redirect("notice", "Confluence settings saved. Test the connection next.", "#sources")


@app.post("/api/platform-knowledge/sources/confluence/test")
async def test_confluence_connection(request: Request):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = knowledge_sources.test_connection(conn)
        except ValueError as exc:
            return _pk_redirect("error", str(exc), "#sources")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("CONFLUENCE_CONNECTION_TESTED", user, {"status": cfg["status"]})
    return _pk_redirect("notice" if cfg["status"] == "CONNECTED" else "error", cfg["check_detail"], "#sources")


@app.post("/api/platform-knowledge/sources/domains")
async def add_web_domain(request: Request, host: str = Form(...)):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            host = knowledge_sources.add_domain(conn, user["name"], host)
        except ValueError as exc:
            return _pk_redirect("error", str(exc), "#sources")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("WEB_SOURCE_APPROVED", user, {"host": host})
    return _pk_redirect("notice", f"{host} approved as a web source.", "#sources")


@app.post("/api/platform-knowledge/sources/domains/remove")
async def remove_web_domain(request: Request, host: str = Form(...)):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            knowledge_sources.remove_domain(conn, host)
        except ValueError as exc:
            return _pk_redirect("error", str(exc), "#sources")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("WEB_SOURCE_REMOVED", user, {"host": host})
    return _pk_redirect("notice", f"{host} removed from the approved web sources.", "#sources")


@app.post("/api/platform-knowledge")
async def add_platform_knowledge(request: Request, category: str = Form(...), file: UploadFile = File(...)):
    user = get_current_user(request)
    content = await file.read()
    conn = get_db_connection()
    try:
        try:
            rec = platform_knowledge.add(conn, file.filename or "", category, content, user["name"])
            try:  # the document is searchable too; one that cannot be cut into chunks is still kept
                parts = knowledge_chunks.chunks_for_file(rec["name"], content)
                knowledge_chunks.index_asset(conn, migration_id="-", scope="platform", asset_id=rec["asset_id"], asset_name=rec["name"], source="file", source_ref=rec["name"],
                                             version=knowledge_chunks.version_of(content), chunks=parts["chunks"], entity_names=list(_canonical_field_lists()))
                knowledge_index.embed_pending(conn, embeddings.get_embedder())
            except ValueError:
                pass
        except ValueError as exc:
            return _pk_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("PLATFORM_KNOWLEDGE_ADDED", user, {"asset_id": rec["asset_id"], "name": rec["name"], "category": rec["category"]})
    return _pk_redirect("notice", f"{rec['name']} added to platform knowledge ({rec['asset_id']}).")


@app.post("/api/platform-knowledge/{asset_id}/retire")
async def retire_platform_knowledge(request: Request, asset_id: str):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            platform_knowledge.retire(conn, asset_id)
            knowledge_chunks.retire_asset(conn, asset_id)
        except ValueError as exc:
            return _pk_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("PLATFORM_KNOWLEDGE_RETIRED", user, {"asset_id": asset_id})
    return _pk_redirect("notice", f"{asset_id} retired.")


@app.get("/entity-selection", response_class=HTMLResponse)
async def entity_selection_view(request: Request):
    # Entity scope is now part of source discovery and human confirmation, not a separate manual pre-step.
    get_current_user(request)
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/entity-sample-files", response_class=HTMLResponse)
async def entity_sample_files_view(request: Request, uploaded: str = "", notice: str = "", error: str = ""):
    """Stage 01 Loader & Inspector: source connection, file loading and the loaded-file list."""
    return _source_stage_page(request, "load", uploaded, notice, error)


@app.get("/discovery", response_class=HTMLResponse)
async def discovery_view(request: Request, uploaded: str = "", notice: str = "", error: str = ""):
    """Stage 02 Discovery: inferred entities and their review, cross-file analysis, knowledge assets."""
    return _source_stage_page(request, "discover", uploaded, notice, error)


def _source_stage_page(request: Request, stage_view: str, uploaded: str, notice: str, error: str):
    user = get_current_user(request)
    source_uploads = []
    migration_setup = state_store.questionnaire_data.get("migration_setup", {})
    in_scope_entities = set(migration_setup.get("entities", []))
    for entity, files in getattr(state_store, "entity_source_files", {}).items():
        for file_id, record in files.items():
            inferred_entity = "Needs clarification" if entity == "Needs clarification" else (record.get("inference", {}).get("entity") or entity)
            selected = inferred_entity in in_scope_entities
            review_status = record.get("inference_approval_status", "PENDING")
            display_status = "NEEDS_CLARIFICATION" if inferred_entity not in set(P0_ENTITIES) else "OUT_OF_SCOPE" if not selected else review_status
            source_uploads.append({**record, "file_id": record.get("file_id", file_id), "inferred_entity": inferred_entity, "display_status": display_status, "scope_eligible": selected})
    source_uploads.sort(key=lambda item: item.get("uploaded_at", ""), reverse=True)
    cross = None
    loaded = []
    for item in source_uploads:
        stored = item.get("stored_path")
        if item.get("inference_approval_status") == "REJECTED" or not stored or not file_store.is_file(stored) or len(loaded) >= 12:
            continue
        try:
            loaded.append({"name": item.get("filename", "source"), "entity": item["inferred_entity"],
                           "rows": read_source_records(item.get("filename", "source.csv"), file_store.read_bytes(stored))})
        except (OSError, ValueError):
            continue
    if len(loaded) >= 2:
        cross = cross_file.analyze(loaded)
        conn = get_db_connection()
        try:
            cross_file.attach_reviews(conn, str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED")), cross)
            conn.commit()
        finally:
            conn.close()
    return render_template_with_context(
        request=request,
        template_name="entity_sample_files.html",
        extra_context={
            "page_title": "Loader & Inspector" if stage_view == "load" else "Discovery",
            "stage_view": stage_view,
            "stage_tab_key": stage_view,
            "setup": migration_setup,
            "azure_cfg": _azure_cfg_for_display(),
            "blob": _blob_panel() if (stage_view == "load" and migration_setup.get("source_acquisition") == "AZURE_CONNECTION") else None,
            "loaded_rows": sum(int(i.get("row_count") or 0) for i in source_uploads),
            "loaded_kb": sum(int(i.get("size_bytes") or 0) for i in source_uploads) / 1024,
            "can_discover": user["role"] in DISCOVERY_ROLES,
            "user_name": user["name"],
            "user_role": user["role"],
            "selected_entities": state_store.get_source_upload_entities(),
            "in_scope_entities": in_scope_entities,
            "source_uploads": source_uploads, "cross_file": cross,
            "file_evidence": _evidence_for_files(source_uploads) if stage_view == "discover" else {},
            "can_give_feedback": user["role"] in DISCOVERY_ROLES,
            "uploaded_entity": uploaded,
            "notice": notice,
            "error": error,
            "acquisition_method": state_store.questionnaire_data.get("migration_setup", {}).get("source_acquisition", "SAMPLE_FILES"),
            "knowledge_docs": state_store.firm_knowledge_docs,
            "entity_approval_summary": {
                entity: {
                    "total": len(state_store.entity_source_files.get(entity, {})),
                    "approved": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") == "APPROVED"),
                    "pending": sum(1 for item in state_store.entity_source_files.get(entity, {}).values() if item.get("inference_approval_status") not in DECIDED_FILE_STATUSES),
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
        selected_scope = [e for e in state_store.questionnaire_data.get("migration_setup", {}).get("entities", []) if e in set(P0_ENTITIES)]
        scope_placeholders = ", ".join("?" for _ in selected_scope) or "''"
        rows = conn.execute(
            f"""
            SELECT s.suggestion_id, s.entity_name, s.source_field, s.canonical_field,
                   s.proposed_rule, s.confidence_score, s.inference_basis
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
        approved_source_mappings = [dict(row) for row in conn.execute(
            """SELECT entity_name, source_field, canonical_field, transformation_expression, created_by AS reviewer
               FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1 ORDER BY entity_name, source_field""",
            (active_migration_id,)).fetchall()]
        # latest decision per source field is a rejection and nothing else is pending or approved for it
        rejected_source_mappings = [dict(row) for row in conn.execute(
            """SELECT s.suggestion_id, s.entity_name, s.source_field, s.canonical_field, s.review_comments, s.reviewer
               FROM mapping_suggestions AS s
               WHERE s.migration_id = ? AND s.status = 'REJECTED'
                 AND s.created_utc = (SELECT MAX(x.created_utc) FROM mapping_suggestions AS x
                                      WHERE x.migration_id = s.migration_id AND x.entity_name = s.entity_name AND x.source_field = s.source_field)
                 AND NOT EXISTS (SELECT 1 FROM mapping_suggestions AS p WHERE p.migration_id = s.migration_id AND p.entity_name = s.entity_name
                                 AND p.source_field = s.source_field AND p.status = 'PENDING')
                 AND NOT EXISTS (SELECT 1 FROM canonical_mappings AS c WHERE c.migration_id = s.migration_id AND c.entity_name = s.entity_name
                                 AND c.source_field = s.source_field AND c.is_confirmed = 1)
               ORDER BY s.entity_name, s.source_field""", (active_migration_id,)).fetchall()]
    finally:
        conn.close()

    source_files = {
        entity: dict(files)
        for entity, files in getattr(state_store, "entity_source_files", {}).items()
        if entity in selected_scope and entity in set(P0_ENTITIES)
    }
    for entity, record in getattr(state_store, "entity_sample_files", {}).items():
        if entity in selected_scope and entity in set(P0_ENTITIES):
            source_files.setdefault(entity, {}).setdefault(record.get("source_role", "ENTITY_MASTER"), record)

    return render_template_with_context(
        request=request,
        template_name="agent_pipeline.html",
        extra_context={
            "page_title": "Schema Mapper · Mapping Studio",
            "user_name": user["name"],
            "user_role": user["role"],
            "can_review_mappings": user["role"] in MAPPING_REVIEW_ROLES,
            "can_discover": user["role"] in DISCOVERY_ROLES,
            "can_answer_rfi": user["role"] in DEFINITION_ROLES,
            "can_reopen_mappings": user["role"] in APPROVAL_ROLES,
            "approved_source_mappings": approved_source_mappings,
            "rejected_source_mappings": rejected_source_mappings,
            "approved_rules": transformation_rules.approved_rules(),
            "builtin_rules": sorted(SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS),
            "canonical_fields": _canonical_field_lists(),
            "firm_context": state_store.questionnaire_data,
            "pipeline_run": latest_run,
            "source_files": source_files,
            "mapping_suggestions": mapping_suggestions,
            "rule_hints": _rule_hints(mapping_suggestions),
            "evidence": _evidence_for(mapping_suggestions),
            "likelihood": _likelihood_for(mapping_suggestions),
            "can_give_feedback": user["role"] in DISCOVERY_ROLES,
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
async def reconciliation_dashboard(request: Request, error: str = "", notice: str = ""):
    return _validation_stage_view(request, "reconcile", error, notice)


def _target_validation_entities(migration_id: str) -> Dict[str, Dict[str, Any]]:
    """What the predefined target checks need, from approved target mappings, the confirmed target model and the canonical rows."""
    try:
        cfg_firm = load_firm_config()
    except FirmConfigError:
        cfg_firm = None
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"] if spec else []
        conn.commit()
    finally:
        conn.close()
    if not spec:
        return {}
    # The same rows the stage writer sent: lookup files (advisors) are not part of an entity's own file.
    conn = get_db_connection()
    try:
        pkg = artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
        rec = artifact_store.read(conn, pkg["artifact_id"]) if pkg else None
        conn.commit()
    finally:
        conn.close()
    lookup_files: set = set()
    if rec:
        try:
            man = json.loads(zipfile.ZipFile(io.BytesIO(rec["data"])).read("manifest.json"))
            lookup_files = {t[4:] + ".csv" for t in (man.get("stagingColumns") or {}) if t.startswith("lkp_")}
        except (KeyError, ValueError, zipfile.BadZipFile):
            pass
    rows, _ = _canonical_rows_for_migration(migration_id, lookup_files)
    out: Dict[str, Dict[str, Any]] = {}
    # A target column counts as required when the target model says so, or when its canonical field is non-nullable in the firm contract
    # or is required for the Orion MCL target. Blanks there are findings; they are never defaulted.
    required_canon: Dict[str, set] = {}
    if cfg_firm:
        for e_name, e_def in cfg_firm.entities.items():
            required_canon[e_name] = {f.name for f in e_def.fields if not f.nullable or any(str(t).startswith("ORION_MCL") for t in f.required_for_targets)}
    for entity, body in spec["entities"].items():
        fields = {f["name"]: f for f in body["fields"]}
        mine = [m for m in approved if m["entity_name"] == entity and m["target_field"] in fields and m.get("canonical_field")]
        if not mine:
            continue
        key = next((f.name for f in cfg_firm.entities[entity].fields if f.is_key), None) if cfg_firm and entity in cfg_firm.entities else None
        tables: Dict[str, Dict[str, Any]] = {}
        for m in mine:
            f = fields[m["target_field"]]
            t = tables.setdefault(f["table"], {"columns": {}, "required": [], "code_lists": {}})
            t["columns"][m["canonical_field"]] = f["column"]
            if f.get("codes"):
                t["code_lists"][f["column"]] = list(f["codes"])
            if f.get("required") or m["canonical_field"] in required_canon.get(entity, set()):
                t["required"].append(f["column"])
        mapped = {m["target_field"] for m in approved if m["entity_name"] == entity}
        out[entity] = {"staging_table": entity, "expected_rows": len(rows.get(entity, [])) if entity in rows else None, "key": key, "tables": tables,
                       "unmapped_required": sorted(n for n, f in fields.items() if f.get("required") and n not in mapped)}
    return out


@app.post("/api/target-validation/run")
async def run_target_validation(request: Request):
    """Run the predefined checks against the loaded target database (read-only, counts only)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        _art, xpkg = _xodus_package(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    # Stable-pipeline package: the checks run on this batch's canonical and mirror tables, before anything reaches the live tables.
    entities = _xodus_validation_entities(migration_id, xpkg["manifest"]) if xpkg else _target_validation_entities(migration_id)
    if not entities:
        return _tv_redirect("error", "Approve target mappings and confirm the target system first.")
    conn = get_db_connection()
    try:
        try:
            result = (target_validation.run(conn, migration_id, user["name"], entities, staging_schema="xodus_canon", schema="xodus_mirror") if xpkg
                      else target_validation.run(conn, migration_id, user["name"], entities))
        except ValueError as exc:
            return _tv_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_VALIDATION_RUN", user, {"run_id": result["run_id"], "status": result["status"], "summary": result["summary"]})
    return _tv_redirect("notice", f"Target validation {result['run_id']}: {result['status']} ({result['summary']['PASS']} passed, {result['summary']['FAIL']} failed, "
                                  f"{result['summary']['WARN'] + result['summary']['NOT_EVALUATED']} to review).")


@app.post("/api/target-validation/signoff")
async def sign_off_target_validation(request: Request, run_id: str = Form(...), comment: str = Form("")):
    """The validation specialist signs off the latest target validation run (must be PASS, with a comment)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            tval = target_validation.latest(conn, migration_id)
            if not tval:
                raise ValueError("Run target validation first.")
            effective = validation_scenarios.effective_status(tval["checks"], validation_scenarios.list_exceptions(conn, migration_id, tval["run_id"]))
            res = validation_signoff.sign_off(conn, migration_id, run_id, effective, comment, user["name"], tval["run_id"])
        except ValueError as exc:
            return _tv_redirect("error", str(exc))
        conn.commit()
        _art, xpkg = _xodus_package(conn, migration_id)
        run = adf_runs.latest(conn, migration_id, "PL_XODUS_Run") if xpkg else None
    finally:
        conn.close()
    _target_setup_audit("TARGET_VALIDATION_SIGNED_OFF", user, {"signoff_id": res["signoff_id"], "run_id": res["run_id"]})
    if not xpkg:
        return _tv_redirect("notice", f"Target validation {run_id} signed off ({res['signoff_id']}).")
    # Record the sign-off where the promotion pipeline checks it: the batch's mirror counts, written to the promotion gate table.
    if not run or not run.get("batch_id"):
        return _tv_redirect("error", f"Signed off ({res['signoff_id']}), but no pipeline batch is recorded, so the promotion gate was not written.")
    conn = get_db_connection()
    try:
        try:
            gate = promotion_gate.record(conn, migration_id, run["batch_id"], run_id, user["name"], res["signed_utc"], xpkg["manifest"])
        except ValueError as exc:
            return _tv_redirect("error", f"Signed off ({res['signoff_id']}), but the promotion gate could not be written: {exc}")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("PROMOTION_GATE_RECORDED", user, {"batch_id": run["batch_id"], "counts": gate["counts"]})
    return _tv_redirect("notice", f"Target validation {run_id} signed off ({res['signoff_id']}). Batch {run['batch_id']} may be promoted: "
                        + ", ".join(f"{t} {n}" for t, n in gate["counts"].items()) + ".")


def _tv_redirect(key: str, message: str):
    return RedirectResponse(url=f"/validation-rules?{key}=" + quote_plus(message) + "#target-validation", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/target-validation/exceptions")
async def propose_target_validation_exception(request: Request, run_id: str = Form(...), check_ref: str = Form(...), reason: str = Form(""), standing: str = Form("")):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            target_validation.ensure_table(conn)
            run = conn.execute("SELECT results_json FROM target_validation_runs WHERE run_id = ? AND migration_id = ?", (run_id, migration_id)).fetchone()
            if not run or not any(c.get("ref") == check_ref and c["status"] in ("FAIL", "WARN", "NOT_EVALUATED") for c in json.loads(run["results_json"])["checks"]):
                raise ValueError("That finding does not exist in the run, or it passed.")
            eid = validation_scenarios.propose_exception(conn, migration_id, validation_scenarios.STANDING if standing else run_id, check_ref, reason, user["name"])
        except ValueError as exc:
            return _tv_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_VALIDATION_EXCEPTION_PROPOSED", user, {"exception_id": eid, "run_id": run_id, "check_ref": check_ref})
    return _tv_redirect("notice", f"Exception {eid} proposed" + (" for every run of this migration" if standing else "") + ". A Migration Analyst must decide it.")


@app.post("/api/target-validation/exceptions/{exception_id}/decision")
async def decide_target_validation_exception(request: Request, exception_id: str, decision: str = Form(...), comments: str = Form("")):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            row = validation_scenarios.decide_exception(conn, exception_id, decision.upper(), user["name"], comments)
        except ValueError as exc:
            return _tv_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_VALIDATION_EXCEPTION_" + row["status"], user, {"exception_id": exception_id, "check_ref": row["check_ref"]})
    return _tv_redirect("notice", f"Exception {exception_id} {row['status'].lower()}.")


def _approved_rules(migration_id: str) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        rows = validation_rules.approved_for_package(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    return rows


def _rule_context(manifest: Optional[Dict[str, Any]]):
    """What a rule is written against: canonical fields per entity, each entity's key field, the package's canonical entities and live tables."""
    fields = _canonical_field_lists()
    keys: Dict[str, str] = {}
    try:
        firm = load_firm_config()
        keys = {e: next((f.name for f in firm.entities[e].fields if f.is_key), "") for e in firm.entities}
    except FirmConfigError:
        pass
    canon = list((manifest or {}).get("canonical") or fields)
    live = [t["live_table"] for t in (manifest or {}).get("liveTables") or []]
    return fields, {e: k for e, k in keys.items() if k}, canon, live


def _rule_redirect(key: str, message: str):
    return RedirectResponse(url=f"/validation-rules?{key}=" + quote_plus(message) + "#validation-rules", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/validation-rules")
async def save_validation_rule(request: Request, entity: str = Form(...), layer: str = Form("canonical"), name: str = Form(...), severity: str = Form("BLOCKING"),
                               english: str = Form(""), sql: str = Form(""), keys_sql: str = Form(""), rule_id: str = Form("")):
    """Save a rule (or a new version of one) as a draft: English is compiled by the fixed grammar, SQL goes through the static check."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            _art, pkg = _xodus_package(conn, migration_id)
            fields, keys, canon, live = _rule_context(pkg["manifest"] if pkg else None)
            rule = validation_rules.create(conn, migration_id, user["name"], entity=entity, layer=layer, name=name, severity=severity, english=english, sql=sql,
                                           keys_sql=keys_sql, fields=fields, keys=keys, canon_entities=canon, mirror_tables=live, rule_id=rule_id or None)
        except ValueError as exc:  # RuleError is a ValueError
            return _rule_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_RULE_SAVED", user, {"rule_id": rule["rule_id"], "version": rule["version"], "entity": entity})
    if rule["problems"]:
        return _rule_redirect("error", f"{rule['rule_id']} v{rule['version']} saved as a draft with problems: " + " ".join(rule["problems"]))
    return _rule_redirect("notice", f"{rule['rule_id']} v{rule['version']} saved as a draft. Run the dry run, then submit it.")


@app.post("/api/validation-rules/{rule_id}/{version}/dry-run")
async def dry_run_validation_rule(request: Request, rule_id: str, version: int):
    """Count the rows that break the draft on the latest batch (read-only, counts only) to prove the query runs."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    state = _data_plane_state(migration_id)
    man = state.get("manifest")
    if not man:
        return _rule_redirect("error", "Generate the pipeline package first (stage 07): the dry run reads the package's tables.")
    conn = get_db_connection()
    try:
        try:
            rule = validation_rules.get(conn, rule_id, version)
            if not rule or rule["migration_id"] != migration_id:
                raise ValueError("That rule does not exist.")
            fields, keys, canon, live = _rule_context(man)
            chk = validation_rules.check_sql(rule["sql_text"], rule["keys_sql"] or "", canon, live)
            count, error = None, ""
            if not chk["ok"]:
                error = " ".join(chk["problems"])
            else:
                cfg = sql_target.get_config(conn)
                statements = [(validation_rules.expand(rule["sql_text"], man["migrationSlug"]), None)]
                if rule["keys_sql"]:
                    statements.append((validation_rules.expand(rule["keys_sql"], man["migrationSlug"]), None))
                try:
                    rows = sql_target.run(cfg, statements)
                    count = int((rows[0][0] or [0])[0] or 0)
                except ValueError as exc:
                    error = str(exc)
            validation_rules.record_dry_run(conn, rule_id, version, user["name"], count, error)
        except ValueError as exc:
            return _rule_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    if error:
        return _rule_redirect("error", f"Dry run of {rule_id} v{version} failed: {error}")
    return _rule_redirect("notice", f"Dry run of {rule_id} v{version}: {count} row(s) break the rule in the latest batch (counts only).")


@app.post("/api/validation-rules/{rule_id}/{version}/submit")
async def submit_validation_rule(request: Request, rule_id: str, version: int):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            validation_rules.submit(conn, rule_id, version, user["name"])
        except ValueError as exc:
            return _rule_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_RULE_SUBMITTED", user, {"rule_id": rule_id, "version": version})
    return _rule_redirect("notice", f"{rule_id} v{version} submitted. A different Migration Analyst must decide it.")


@app.post("/api/validation-rules/{rule_id}/{version}/decision")
async def decide_validation_rule(request: Request, rule_id: str, version: int, decision: str = Form(...), comments: str = Form("")):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            rule = validation_rules.decide(conn, rule_id, version, decision.upper(), user["name"], comments)
        except ValueError as exc:
            return _rule_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_RULE_" + rule["status"], user, {"rule_id": rule_id, "version": version})
    return _rule_redirect("notice", f"{rule_id} v{version} {rule['status'].lower()}." + (" Generate a new package (stage 07) so it runs in the pipeline." if rule["status"] == "APPROVED" else ""))


@app.post("/api/validation-rules/{rule_id}/retire")
async def retire_validation_rule(request: Request, rule_id: str):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            validation_rules.retire(conn, rule_id, user["name"])
        except ValueError as exc:
            return _rule_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_RULE_RETIRED", user, {"rule_id": rule_id})
    return _rule_redirect("notice", f"{rule_id} retired. Generate a new package (stage 07) so it stops running.")


def _wave_redirect(key: str, message: str, page: str = "/execution"):
    page = page if page in ("/execution", "/validation-rules") else "/execution"
    return RedirectResponse(url=f"{page}?{key}=" + quote_plus(message) + "#wave-board", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/wave-validation/exceptions")
async def propose_wave_exception(request: Request, check_ref: str = Form(...), reason: str = Form(""), return_to: str = Form("/execution")):
    """Propose that a failed blocking check of the latest batch be accepted. A different approver decides it; approval records a waiver for the pipeline."""
    user = get_current_user(request)
    if user["role"] not in VALIDATION_ROLES:
        return _wave_redirect("error", "A Validation Specialist proposes exceptions.", return_to)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    state = _data_plane_state(migration_id)
    man, run = state.get("manifest"), state.get("run")
    if not man or not run or not run.get("batch_id"):
        return _wave_redirect("error", "There is no batch to raise an exception on.", return_to)
    conn = get_db_connection()
    try:
        try:
            board = wave_validation.read_board(conn, man["migrationSlug"], migration_id, run["batch_id"])
            if not wave_validation.failing_finding(board, check_ref):
                raise ValueError("That finding does not exist in the latest batch, or it did not fail.")
            eid = validation_scenarios.propose_exception(conn, migration_id, wave_validation.RUN_PREFIX + run["batch_id"], check_ref, reason, user["name"])
        except ValueError as exc:
            return _wave_redirect("error", str(exc), return_to)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("WAVE_EXCEPTION_PROPOSED", user, {"exception_id": eid, "batch_id": run["batch_id"], "check_ref": check_ref})
    return _wave_redirect("notice", f"Exception {eid} proposed. A different Migration Analyst must decide it.", return_to)


@app.post("/api/wave-validation/exceptions/{exception_id}/decision")
async def decide_wave_exception(request: Request, exception_id: str, decision: str = Form(...), comments: str = Form(""), return_to: str = Form("/execution")):
    """Decide a wave exception. Approval writes the waiver (xodus_wave_waiver) the validation procedure reads; the run is then restarted from the wave."""
    user = get_current_user(request)
    if user["role"] not in APPROVAL_ROLES:
        return _wave_redirect("error", "A Migration Analyst decides exceptions.", return_to)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            row = validation_scenarios.decide_exception(conn, exception_id, decision.upper(), user["name"], comments)
            if not str(row["run_id"]).startswith(wave_validation.RUN_PREFIX):
                raise ValueError("That is not a wave exception.")
            if row["status"] == "APPROVED":
                wave_validation.record_waiver(conn, migration_id, str(row["run_id"])[len(wave_validation.RUN_PREFIX):], row["check_ref"], row["reason"], user["name"])
        except ValueError as exc:
            conn.rollback()  # the decision is kept only when the waiver was written
            return _wave_redirect("error", str(exc), return_to)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("WAVE_EXCEPTION_" + row["status"], user, {"exception_id": exception_id, "check_ref": row["check_ref"]})
    return _wave_redirect("notice", f"Exception {exception_id} {row['status'].lower()}." + (" The waiver is recorded; restart from the wave so the gate passes." if row["status"] == "APPROVED" else ""), return_to)


@app.get("/validation-rules", response_class=HTMLResponse)
async def validation_rules_page(request: Request, error: str = "", notice: str = ""):
    """Stage 10: the scenarios that extend the validation run, defined after the data plane has executed."""
    return _validation_stage_view(request, "rules", error, notice)


@app.get("/execution", response_class=HTMLResponse)
async def execution_page(request: Request, error: str = "", notice: str = ""):
    """Stage 09: simulated data-plane execution of the deployment package."""
    return _validation_stage_view(request, "execution", error, notice)


def _wave_exceptions(migration_id: str, dp: Dict[str, Any]) -> Dict[str, Any]:
    """Exceptions proposed for the findings of the latest batch's waves, by finding reference."""
    batch = ((dp or {}).get("run") or {}).get("batch_id")
    if not batch or not (dp or {}).get("board"):
        return {}
    conn = get_db_connection()
    try:
        rows = validation_scenarios.list_exceptions(conn, migration_id, wave_validation.RUN_PREFIX + batch)
        conn.commit()
    finally:
        conn.close()
    return {r["check_ref"]: r for r in rows if str(r["check_ref"]).startswith(wave_validation.REF_PREFIX + "|")}


def _stage_data_plane(migration_id: str, view: str) -> Dict[str, Any]:
    """Data-plane state for stages 09 and 10; the gate is read from the XODUS database only on stage 10 and never breaks the page."""
    try:
        dp = _data_plane_state(migration_id, refresh_runs=True)
    except Exception as exc:  # shown, never raised: the page still works without Azure
        return {"pkg": None, "error": str(exc)}
    if dp.get("pkg") and dp.get("manifest") and dp.get("run") and dp["run"].get("batch_id"):  # what the pipeline's SQL found, wave by wave
        conn = get_db_connection()
        try:
            dp["board"] = wave_validation.read_board(conn, dp["manifest"]["migrationSlug"], migration_id, dp["run"]["batch_id"])
        except ValueError as exc:
            dp["board_error"] = str(exc)
        finally:
            conn.close()
    if view == "rules" and dp.get("pkg") and dp.get("run") and dp["run"].get("batch_id"):
        conn = get_db_connection()
        try:
            dp["gate"] = promotion_gate.status(conn, migration_id, dp["run"]["batch_id"])
        except ValueError as exc:
            dp["gate_error"] = str(exc)
        try:
            dp["live_validation"] = live_validation_status(conn, migration_id)
        finally:
            conn.close()
    return dp


def _validation_stage_view(request: Request, view: str, error: str, notice: str):
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
        # The audit rows keep the status at proposal time; show where each field stands now.
        approved_now = {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT entity_name, source_field, canonical_field FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1",
            (migration_id,)).fetchall()}
        for item in mapping_lineage:
            if item["validation_status"] == "PROPOSED_NEEDS_REVIEW" and (item["entity_name"], item["source_column"], item["canonical_column"]) in approved_now:
                item["validation_status"] = "APPROVED (after proposal)"
        latest = conn.execute(
            "SELECT run_id, status, results_json, executed_by, executed_utc FROM validation_runs "
            "WHERE migration_id = ? ORDER BY executed_utc DESC LIMIT 1", (migration_id,)).fetchone()
        scenarios = validation_scenarios.list_scenarios(conn, migration_id)
        runs = execution_simulator.list_runs(conn, migration_id)
        recs_by_run = {r["run_id"]: gap_recommender.list_for_run(conn, migration_id, r["run_id"]) for r in runs}
        # Stage 09 runs the ADF package built in stage 07; the older generated SQL package also counts.
        has_package = bool(artifact_store.latest(conn, migration_id, "ADF_PACKAGE")) or bool(package_contract.list_packages(conn, migration_id))
        stored_signoff = artifact_store.list_artifacts(conn, migration_id, "SIGNOFF_EVIDENCE")
        adf_status = _adf_status(conn, migration_id)
        run_status = run_monitor.stored(conn, migration_id)
        tval = target_validation.latest(conn, migration_id)
        tval_exceptions = validation_scenarios.list_exceptions(conn, migration_id, tval["run_id"]) if tval else []
        if tval:
            tval["effective_status"] = validation_scenarios.effective_status(tval["checks"], tval_exceptions)
            tval["signoff"] = validation_signoff.current(conn, migration_id, tval["run_id"])
        adf_deployment = adf_simulator.latest(conn, migration_id)
        adf_trigger_blocker = adf_simulator.trigger_blocker(adf_deployment, adf_status["stale_reasons"] if adf_status else None, adf_status["latest"]["artifact_id"] if adf_status else None)
        exceptions = validation_scenarios.list_exceptions(conn, migration_id, latest["run_id"] if latest else "-")
        rules_list = validation_rules.list_rules(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    validation_run = None
    if latest:
        validation_run = {"run_id": latest["run_id"], "executed_by": latest["executed_by"],
                          "executed_utc": latest["executed_utc"], **json.loads(latest["results_json"])}
        validation_run["effective_status"] = validation_scenarios.effective_status(validation_run["checks"], exceptions)
        _c = get_db_connection()
        try:
            validation_run["stale"] = _validation_stale(_c, migration_id, latest["executed_utc"])
        finally:
            _c.close()
    return render_template_with_context(
        request=request,
        template_name="reconciliation.html",
        extra_context={
            "page_title": {"rules": "Target validation", "execution": "ADF Execution"}.get(view, "Mapping Validation Review"),
            "view": view,
            "user_name": user["name"],
            "user_role": user["role"],
            "pipeline_runs": state_store.pipeline_runs,
            "drift_gate": state_store.questionnaire_data.get("aum_drift_gate", "0.50%"),
            "approved_mappings": approved_mappings,
            "pending_mapping_count": pending_count,
            "mapping_runs": mapping_runs,
            "mapping_lineage": mapping_lineage,
            "migration_id": migration_id,
            "validation_run": validation_run, "error": error, "notice": notice, "stored_signoff": stored_signoff, "adf_status": adf_status, "run_status": run_status, "tval": tval, "tval_exceptions": {e["check_ref"]: e for e in tval_exceptions}, "adf_deployment": adf_deployment, "adf_trigger_blocker": adf_trigger_blocker,
            "can_run_validation": user["role"] in VALIDATION_ROLES,
            "can_raise_run_request": user["role"] in RUN_FAILURE_ROLES,
            "in_scope_entities": [e for e in P0_ENTITIES if e in (state_store.questionnaire_data.get("migration_setup", {}) or {}).get("entities", [])],
            "drift": drift_detection.detect(approved_mappings, state_store.entity_source_files),
            "scenarios": scenarios, "check_types": validation_scenarios.CHECK_TYPES, "exec_runs": runs, "recs_by_run": recs_by_run, "has_package": has_package,
            "exceptions": {e["check_ref"]: e for e in exceptions}, "can_decide_exception": user["role"] in APPROVAL_ROLES, "can_review_failures": user["role"] in APPROVAL_ROLES,
            "current_user_name": user["name"],
            "dp": (_dp := _stage_data_plane(str(migration_id), view)),
            "wave_exceptions": _wave_exceptions(str(migration_id), _dp),
            "validation_rules": rules_list, "rule_patterns": validation_rules.PATTERNS,
            "can_promote": user["role"] in APPROVAL_ROLES,
        }
    )


def _run_simulated_execution(user, migration_id: str, outage: str = "", retry_of: str = ""):
    """Run the latest deployment package through the SIMULATED data plane and record telemetry. Returns (run_id, telemetry, package) or (None, error, None)."""
    import time as _time
    conn = get_db_connection()
    try:
        latest = package_contract.list_packages(conn, migration_id)
        pkg_row = package_contract.get_package(conn, latest[0]["package_id"]) if latest else None
        try:
            if not pkg_row:
                raise ValueError("Generate a deployment package first (stage 07 Transformation SQL).")
            if retry_of:
                execution_simulator.retry_allowed(conn, retry_of)
            package = package_contract.validate_text(pkg_row["yaml_text"]).model_dump()
        except ValueError as exc:
            return None, str(exc), None
        conn.commit()
    finally:
        conn.close()
    started = datetime.now(timezone.utc)
    t0 = _time.perf_counter()
    lookups, lookup_files = _lookup_context(package)
    rows = {} if outage == "on" else _canonical_rows_for_migration(migration_id, lookup_files)[0]
    stage_outputs: Dict[str, list] = {}
    telemetry = execution_simulator.simulate(package, rows, outage == "on", lookups, outputs=stage_outputs)
    duration = int((_time.perf_counter() - t0) * 1000)
    conn = get_db_connection()
    try:
        rid = execution_simulator.record_run(conn, migration_id, package, user["name"], telemetry, started, duration, retry_of or None)
        conn.commit()
    finally:
        conn.close()
    if outage != "on":
        _write_run_to_stage(migration_id, rid, package, telemetry, stage_outputs)
    if outage != "on" and telemetry.get("failures"):
        _store_gap_recommendations(migration_id, rid, package, telemetry, rows, lookups)
    return rid, telemetry, package


def _write_run_to_stage(migration_id: str, run_id: str, package: Dict[str, Any], telemetry: Dict[str, Any], outputs: Dict[str, list]) -> None:
    """When the migration reads from Azure Blob storage, put the run's transformed files and the files curation needs in stage/run-<id>/.
    Best effort: a storage problem is recorded on the run and never fails it."""
    setup = state_store.questionnaire_data.get("migration_setup", {})
    stage: Dict[str, Any]
    if setup.get("source_acquisition") != "AZURE_CONNECTION":
        stage = {"written": False, "reason": "The migration does not use Azure source storage, so no stage folder is written."}
    else:
        panel = _blob_panel(with_files=False)
        if not (panel["reachable"] and panel["layout_ready"]):
            stage = {"written": False, "reason": "Blob storage is unreachable or the folder structure is not created yet (Azure connection page)."}
        else:
            hashes = {rec.get("filename"): rec.get("content_sha256") for g in state_store.entity_source_files.values() for rec in g.values()}
            support = {
                "run": {"run_id": run_id, "migration_id": migration_id, "mode": "SIMULATED", "status": telemetry.get("status"), "totals": telemetry.get("totals"),
                        "package_id": package["meta"]["package_id"], "package_version": package["meta"]["package_version"]},
                "package_contract": {"canonical_to_target": package.get("canonical_to_target"), "transformation_rules": package.get("transformation_rules"),
                                     "validation": package.get("validation")},
                "problems": telemetry.get("failures", []),
                "source_files": hashes,
            }
            try:
                stage = blob_source.write_stage_run(panel["container"], run_id, outputs, support)
            except Exception as exc:
                stage = {"written": False, "reason": f"Could not write to blob storage: {type(exc).__name__}"}
    conn = get_db_connection()
    try:
        execution_simulator.attach_stage(conn, run_id, stage)
        conn.commit()
    finally:
        conn.close()
    telemetry["stage"] = stage


def _store_gap_recommendations(migration_id: str, run_id: str, package: Dict[str, Any], telemetry: Dict[str, Any], canonical_rows, lookups) -> None:
    """Best-effort: look at the problems of a run and the source files and store recommendations. A failure here never fails the run."""
    try:
        try:
            key_cfg = load_firm_config()
        except FirmConfigError:
            key_cfg = None
        tables: Dict[str, Dict[str, Any]] = {}
        for entity, group in state_store.entity_source_files.items():
            if entity not in P0_ENTITIES:
                continue
            rules = _get_approved_mapping_rules(migration_id, entity)
            mapper = SchemaMapperAgent(
                custom_catalog={**schema_mapper.catalog, **{src: r["canonical_field"] for src, r in rules.items()}},
                approved_transformations={src: r["transformation_expression"] for src, r in rules.items()})
            for rec in group.values():
                stored = rec.get("stored_path")
                if rec.get("inference_approval_status") != "APPROVED" or not stored or not file_store.is_file(stored):
                    continue
                records = read_source_records(rec.get("filename", "source.csv"), file_store.read_bytes(stored))
                lineage = {l["canonical_field"]: l["source_field"] for l in mapper.map_source_to_canonical(entity, records[:200]).get("field_lineages", []) if l.get("canonical_field")}
                tables[str(rec.get("filename", "source.csv"))] = {"entity": entity, "rows": records, "lineage": lineage}
        keys = {}
        if key_cfg:
            for e in P0_ENTITIES:
                if e in key_cfg.entities:
                    k = next((f.name for f in key_cfg.entities[e].fields if f.is_key), None)
                    if k:
                        keys[e] = k
        rules_by_expr = {r["expression"]: r for r in package["transformation_rules"]}
        recs = gap_recommender.recommend(migration_id, telemetry["failures"], package["canonical_to_target"], rules_by_expr, canonical_rows, tables,
                                         lookups, {"entity_keys": keys})
        conn = get_db_connection()
        try:
            gap_recommender.save(conn, migration_id, run_id, recs)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # recommendations are advisory; never block a run
        state_store.audit_log.append({"event": "GAP_RECOMMENDER_FAILED", "actor": "system", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                      "details": {"run_id": run_id, "error": type(exc).__name__}})


@app.post("/api/execution/recommendations/{rec_id}/decide")
async def decide_gap_recommendation(request: Request, rec_id: str, decision: str = Form(...), reason: str = Form("")):
    """Accept (creates a DRAFT rule when the proposal has one), dismiss, or send a data-provider request. Recorded with the session's reviewer."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            res = gap_recommender.decide(conn, migration_id, rec_id, decision, reason, user["name"])
        except ValueError as exc:
            return RedirectResponse(url="/execution?error=" + quote_plus(str(exc)), status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    r = res["rec"]
    _target_setup_audit("GAP_RECOMMENDATION_" + res["status"], user, {"rec_id": rec_id, "kind": r["kind"], "entity": r["entity"], "column": r["column"], "draft": res["draft"]})
    msg = f"Recommendation {rec_id} marked {res['status'].lower().replace('_', ' ')}."
    if res["draft"]:
        msg += f" Draft rule {res['draft']} created; it needs approval on Business Rules before any mapping can use it."
    return RedirectResponse(url="/execution?notice=" + quote_plus(msg), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/execution/recommendations/bulk-decide")
async def bulk_decide_gap_recommendations(request: Request):
    """Apply one decision to the ticked recommendations; each row may carry its own comment (comment_<rec_id>), else the shared one."""
    user = get_current_user(request)
    form = await request.form()
    decision = str(form.get("decision", ""))
    shared = str(form.get("reason", "")).strip()
    rec_ids = [str(v) for v in form.getlist("rec_id")]
    if not rec_ids:
        return RedirectResponse(url="/execution?error=" + quote_plus("Tick at least one recommendation first."), status_code=status.HTTP_303_SEE_OTHER)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    done, errors, drafts = 0, [], []
    conn = get_db_connection()
    try:
        for rid in rec_ids:
            reason = str(form.get("comment_" + rid, "")).strip() or shared
            try:
                res = gap_recommender.decide(conn, migration_id, rid, decision, reason, user["name"])
            except ValueError as exc:
                errors.append(f"{rid}: {exc}")
                continue
            done += 1
            if res["draft"]:
                drafts.append(res["draft"])
            r = res["rec"]
            _target_setup_audit("GAP_RECOMMENDATION_" + res["status"], user, {"rec_id": rid, "kind": r["kind"], "entity": r["entity"], "column": r["column"], "draft": res["draft"]})
        conn.commit()
    finally:
        conn.close()
    msg = f"{done} recommendation(s) marked {decision.lower().replace('_', ' ')}."
    if drafts:
        msg += " Draft rule(s) " + ", ".join(drafts) + " created; they need approval on Business Rules."
    url = "/execution?notice=" + quote_plus(msg)
    if errors:
        url += "&error=" + quote_plus("; ".join(errors)[:400])
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/execution/runs/{run_id}/failures/bulk-review")
async def bulk_review_execution_failures(request: Request, run_id: str):
    """Acknowledge or resolve the ticked problems; each row may carry its own comment (comment_<idx>), else the shared one."""
    user = get_current_user(request)
    form = await request.form()
    decision = str(form.get("decision", ""))
    shared = str(form.get("reason", "")).strip()
    idxs = [int(v) for v in form.getlist("idx") if str(v).isdigit()]
    if not idxs:
        return RedirectResponse(url="/execution?error=" + quote_plus("Tick at least one problem first."), status_code=status.HTTP_303_SEE_OTHER)
    done, errors = 0, []
    conn = get_db_connection()
    try:
        for i in idxs:
            reason = str(form.get(f"comment_{i}", "")).strip() or shared
            try:
                res = execution_simulator.review_failure(conn, run_id, i, decision, reason, user["name"])
            except ValueError as exc:
                errors.append(f"#{i + 1}: {exc}")
                continue
            done += 1
            f = res["failure"]
            _target_setup_audit("EXECUTION_PROBLEM_" + res["status"], user, {"run_id": run_id, "entity": f["entity"], "table": f["table"], "column": f["column"], "rule": f["rule"]})
        conn.commit()
    finally:
        conn.close()
    url = "/execution?notice=" + quote_plus(f"{done} problem(s) marked {decision.lower()}.")
    if errors:
        url += "&error=" + quote_plus("; ".join(errors)[:400])
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/execution/runs/{run_id}/promote")
async def promote_run_to_curated(request: Request, run_id: str):
    """Copy a reviewed run's stage tables to curated/ (Analyst/Admin). The gate is deterministic; the approver comes from the session."""
    user = get_current_user(request)
    def back(key: str, msg: str):
        return RedirectResponse(url=f"/execution?{key}=" + quote_plus(msg), status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db_connection()
    try:
        gate = execution_simulator.curation_gate(conn, run_id)
        if not gate["ok"]:
            return back("error", "Not curated: " + " ".join(gate["reasons"]))
        panel = _blob_panel(with_files=False)
        if not (panel["reachable"] and panel["layout_ready"]):
            return back("error", "Blob storage is unreachable or the folder structure is not created yet.")
        try:
            res = blob_source.promote_to_curated(panel["container"], run_id, user["name"], gate["reviews"])
        except Exception as exc:
            return back("error", f"Could not write to curated: {type(exc).__name__}: {str(exc)[:120]}")
        execution_simulator.attach_curated(conn, run_id, res)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("EXECUTION_RUN_CURATED", user, {"run_id": run_id, "files": len(res["files"])})
    return back("notice", f"{run_id} curated: {len(res['files'])} table(s) written to {res['container']}/curated/.")


@app.post("/api/execution/run")
async def run_simulated_execution(request: Request, outage: str = Form(""), retry_of: str = Form("")):
    """Run the latest deployment package through the SIMULATED data plane and record telemetry. Nothing leaves this platform."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    rid, telemetry, package = _run_simulated_execution(user, migration_id, outage, retry_of)
    if rid is None:
        return RedirectResponse(url="/execution?error=" + quote_plus(telemetry), status_code=status.HTTP_303_SEE_OTHER)
    _target_setup_audit("EXECUTION_SIMULATED_RUN", user, {"run_id": rid, "package_id": package["meta"]["package_id"], "status": telemetry["status"], "retry_of": retry_of or None})
    return RedirectResponse(url="/execution?notice=" + quote_plus(f"SIMULATED run {rid}: {telemetry['status']}. No data left this platform."),
                            status_code=status.HTTP_303_SEE_OTHER)


def _execution_redirect(key: str, message: str):
    return RedirectResponse(url=f"/execution?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/adf/simulated-deploy")
async def adf_simulated_deploy(request: Request):
    """Deploy the latest stored ADF package into a fictitious factory. Nothing is sent to Azure."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        art = artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
        if not art:
            return _execution_redirect("error", "Save an ADF package first (stage 07 Transformation SQL).")
        try:
            data = artifact_store.read(conn, art["artifact_id"])["data"]
        except ValueError as exc:
            return _execution_redirect("error", str(exc))
        dep = adf_simulator.deploy(conn, migration_id, art, data, user["name"])
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("ADF_SIMULATED_DEPLOY", user, {"deployment_id": dep["deployment_id"], "artifact_id": art["artifact_id"], "status": dep["status"], "errors": len(dep["errors"])})
    if dep["status"] != "DEPLOYED_SIMULATED":
        return _execution_redirect("error", f"Simulated deployment {dep['deployment_id']} rejected: " + " ".join(dep["errors"][:3]))
    return _execution_redirect("notice", f"Package v{art['artifact_version']} deployed to the SIMULATED factory {dep['factory_name']} ({dep['deployment_id']}); {len(dep['resources'])} resources. Nothing reached Azure.")


@app.post("/api/adf/simulated-trigger")
async def adf_simulated_trigger(request: Request):
    """Trigger the deployed pipeline in the simulated factory; it runs on the simulated data plane."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        dep = adf_simulator.latest(conn, migration_id)
        status_info = _adf_status(conn, migration_id)
        blocker = adf_simulator.trigger_blocker(dep, status_info["stale_reasons"] if status_info else None, status_info["latest"]["artifact_id"] if status_info else None)
    finally:
        conn.close()
    if blocker:
        return _execution_redirect("error", blocker)
    rid, telemetry, package = _run_simulated_execution(user, migration_id)
    if rid is None:
        return _execution_redirect("error", telemetry)
    conn = get_db_connection()
    try:
        adf_run = adf_simulator.record_trigger(conn, dep["deployment_id"], rid, user["name"])
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("ADF_SIMULATED_TRIGGER", user, {"deployment_id": dep["deployment_id"], "adf_run_id": adf_run, "run_id": rid, "status": telemetry["status"]})
    return _execution_redirect("notice", f"Pipeline triggered in the SIMULATED factory ({adf_run}): {telemetry['status']}. No data left this platform.")


@app.post("/api/execution/runs/{run_id}/failures/{idx}/review")
async def review_execution_failure(request: Request, run_id: str, idx: int, decision: str = Form(...), reason: str = Form("")):
    """Acknowledge or resolve a problem found by a simulated run. Analyst/Admin only; recorded with the reviewer from the session."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            res = execution_simulator.review_failure(conn, run_id, idx, decision, reason, user["name"])
        except ValueError as exc:
            return RedirectResponse(url="/execution?error=" + quote_plus(str(exc)), status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    f = res["failure"]
    _target_setup_audit("EXECUTION_PROBLEM_" + res["status"], user, {"run_id": run_id, "entity": f["entity"], "table": f["table"], "column": f["column"], "rule": f["rule"]})
    return RedirectResponse(url="/execution?notice=" + quote_plus(f"Problem on {f['entity']}.{f['table']}.{f['column']} marked {res['status'].lower()}."),
                            status_code=status.HTTP_303_SEE_OTHER)


def _validation_redirect(key: str, message: str):
    return RedirectResponse(url=f"/reconciliation?{key}=" + quote_plus(message) + "#validation-results", status_code=status.HTTP_303_SEE_OTHER)


def _scenario_redirect(key: str, message: str):
    return RedirectResponse(url=f"/validation-rules?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/validation/scenarios")
async def create_validation_scenario(request: Request, entity_name: str = Form(...), check_type: str = Form(...), field_name: str = Form(""),
                                     stage: str = Form("PRE"), description: str = Form(""), values: str = Form(""), min_value: str = Form(""),
                                     max_value: str = Form(""), formats: str = Form("")):
    """A Validation Specialist defines an allow-listed validation scenario. Evaluated on the next run; nothing is executed as code."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))

    def num(text: str):
        t = text.strip()
        return None if not t else (int(t) if re.fullmatch(r"-?\d+", t) else float(t))
    try:
        params: Dict[str, Any] = {}
        if check_type == "ALLOWED_VALUES":
            params["values"] = [v for v in re.split(r"[,\n]+", values) if v.strip()]
        elif check_type == "MAX_LENGTH":
            params["max"] = num(max_value)
        elif check_type == "ROW_COUNT_MIN":
            params["min"] = num(min_value)
        elif check_type == "NUMERIC_RANGE":
            params["min"], params["max"] = num(min_value), num(max_value)
        elif check_type == "DATE_PARSEABLE":
            params["input_formats"] = [f.strip() for f in formats.split(",") if f.strip()]
        conn = get_db_connection()
        try:
            created = validation_scenarios.create_scenario(conn, migration_id, entity_name, check_type, field_name, params, stage, description, user["name"])
            conn.commit()
        finally:
            conn.close()
    except ValueError as exc:
        return _scenario_redirect("error", str(exc))
    _target_setup_audit("VALIDATION_SCENARIO_DEFINED", user, {"scenario_id": created["scenario_id"], "check_type": check_type, "entity": entity_name, "stage": stage})
    return _scenario_redirect("notice", f"Scenario {created['scenario_id']} saved. It runs with the next validation run.")


def _adf_status(conn, migration_id: str):
    """Latest stored ADF package with the reasons (if any) it is stale; None when no package was saved."""
    stored = artifact_store.list_artifacts(conn, migration_id, "ADF_PACKAGE")
    if not stored:
        return None
    spec = target_connector.spec(conn)
    approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"]
    reasons = artifact_store.staleness(stored[0], artifact_store.build_provenance(conn, migration_id, spec, approved))
    _, xpkg = _xodus_package(conn, migration_id)
    if xpkg:
        reasons += _code_map_stale(conn, migration_id, xpkg["manifest"])
    return {"latest": stored[0], "stale_reasons": reasons}


def _validation_stale(conn, migration_id: str, executed_utc: str) -> bool:
    """A mapping validation run no longer stands once a source-to-canonical mapping was approved or changed after it."""
    row = conn.execute("SELECT MAX(updated_utc) FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchone()
    return bool(row and row[0] and str(row[0]) > str(executed_utc or ""))


def _code_map_stale(conn, migration_id: str, manifest: Dict[str, Any]) -> List[str]:
    """Why a stable-pipeline package no longer carries the approved code values or the target system's code values (canonical model v2)."""
    cs = manifest.get("codeSets") or {}
    reasons = []
    approved = sorted([r["code_set"], r["source_value"], r["canonical_code"]] for r in code_mappings.approved_source_map(conn, migration_id))
    if (cs.get("sourceMap") or []) != approved:
        reasons.append("Code values were decided on Business Rules → Code sets after this package was built.")
    # Entity resolution and the entities themselves (canonical model phase 2): match rules, derived account roles, entities in scope.
    canon = manifest.get("canonical") or {}
    er_now = entity_resolution.approved_rules(conn, migration_id)
    if any(sorted(map(tuple, (canon.get(e) or {}).get("resolution") or [])) != sorted(map(tuple, er_now.get(e, []))) for e in set(canon) | set(er_now)):
        reasons.append("Match rules were decided on Entity resolution after this package was built.")
    scope = _in_scope_entities()
    wanted = {e for e in scope if _has_entity_file(e) and _get_approved_mapping_rules(migration_id, e)}
    for derived_entity in entity_resolution.recipes():
        if derived_entity in scope and not _has_entity_file(derived_entity) and (entity_resolution.derivation(conn, migration_id, derived_entity) or {}).get("status") == "APPROVED":
            wanted.add(derived_entity)
    if wanted - set(canon):
        reasons.append("Entities now in scope are not in this package: " + ", ".join(sorted(wanted - set(canon))) + ".")
    now_rules = sorted((r["rule_id"], r["version"]) for r in validation_rules.approved_for_package(conn, migration_id))
    if now_rules != sorted((r["id"], r["version"]) for r in manifest.get("rules") or []):
        reasons.append("Validation rules were approved or retired after this package was built.")
    system = (target_connector.spec(conn) or {}).get("target_system") or ""
    if system and (cs.get("targetMap") or []) != sorted([r["code_set"], r["canonical_code"], r["target_value"]] for r in code_mappings.target_map(conn, system)):
        reasons.append("The target system's code values changed (Platform setup → Target code values).")
    return reasons


MIN_STALE_REASON = 10


def _stale_gate(conn, migration_id: str, reason: str):
    """Sign-off evidence may not be saved while the latest ADF package is stale unless the reviewer states why.
    Returns (error_message_or_None, override_record_or_None)."""
    status = _adf_status(conn, migration_id)
    if not status or not status["stale_reasons"]:
        return None, None
    reason = " ".join(str(reason or "").split())
    if len(reason) < MIN_STALE_REASON:
        return ("The latest ADF package " + status["latest"]["artifact_id"] + " is stale: " + " ".join(status["stale_reasons"]) +
                " Generate a new package, or give a reason (at least 10 characters) for signing off against it."), None
    return None, {"package_artifact_id": status["latest"]["artifact_id"], "package_version": status["latest"]["artifact_version"],
                  "stale_reasons": status["stale_reasons"], "reviewer_reason": reason[:500]}


def _adf_reference(conn, migration_id: str, run_id: str):
    spec = target_connector.spec(conn)
    approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"]
    return artifact_store.package_reference(conn, migration_id, artifact_store.build_provenance(conn, migration_id, spec, approved), run_id)


@app.get("/api/validation/signoff-evidence")
async def download_signoff_evidence(request: Request):
    """Download the validation sign-off evidence for the latest validation run (counts, ids and reasons only)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            approved = [dict(r) for r in conn.execute("SELECT entity_name, source_field, canonical_field FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchall()]
            blocked, override = _stale_gate(conn, migration_id, request.query_params.get("stale_reason", ""))
            if blocked:
                return _scenario_redirect("error", blocked)
            doc = signoff_evidence.build(conn, migration_id, user["name"], drift_detection.detect(approved, state_store.entity_source_files))
            doc["adf_package"] = _adf_reference(conn, migration_id, doc["validation_run"]["run_id"])
            if override:
                doc["stale_package_override"] = {**override, "reviewer": user["name"]}
        except ValueError as exc:
            return _scenario_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("SIGNOFF_EVIDENCE_EXPORTED", user, {"run_id": doc["validation_run"]["run_id"]})
    return Response(content=json.dumps(doc, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="signoff_evidence_{migration_id}.json"'})


@app.post("/api/validation/scenarios/suggest")
async def suggest_validation_scenarios(request: Request):
    """Draft default scenarios from the confirmed target contract. They stay DRAFT until a Validation Specialist adopts each one."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        if not spec:
            return _scenario_redirect("error", "Confirm the target system first; suggestions come from the target contract.")
        maps = target_mapping.list_mappings(conn, migration_id, spec)
        found = validation_scenarios.suggest_defaults(spec, maps, validation_scenarios.list_scenarios(conn, migration_id))
        count = validation_scenarios.save_suggestions(conn, migration_id, found, user["name"])
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_SCENARIOS_SUGGESTED", user, {"count": count})
    return _scenario_redirect("notice", f"{count} suggested scenario(s) added as drafts. Adopt the ones you want; drafts are not evaluated." if count else "No new suggestions: approved required fields and keys are already covered.")


@app.post("/api/validation/scenarios/{scenario_id}/adopt")
async def adopt_validation_scenario(request: Request, scenario_id: str):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            validation_scenarios.adopt_scenario(conn, scenario_id)
        except ValueError as exc:
            return _scenario_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_SCENARIO_ADOPTED", user, {"scenario_id": scenario_id})
    return _scenario_redirect("notice", f"Scenario {scenario_id} adopted. It runs with the next validation run.")


@app.post("/api/validation/scenarios/{scenario_id}/retire")
async def retire_validation_scenario(request: Request, scenario_id: str):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            validation_scenarios.retire_scenario(conn, scenario_id)
        except ValueError as exc:
            return _scenario_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_SCENARIO_RETIRED", user, {"scenario_id": scenario_id})
    return _scenario_redirect("notice", f"Scenario {scenario_id} retired.")


@app.post("/api/validation/exceptions")
async def propose_validation_exception(request: Request, run_id: str = Form(...), check_ref: str = Form(...), reason: str = Form(""), standing: str = Form("")):
    """A Validation Specialist proposes accepting a finding; a Migration Analyst or Platform Admin decides."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            run = conn.execute("SELECT results_json FROM validation_runs WHERE run_id = ? AND migration_id = ?", (run_id, migration_id)).fetchone()
            if not run or not any(c.get("ref") == check_ref and c["status"] in ("FAIL", "WARN", "NOT_EVALUATED") for c in json.loads(run["results_json"])["checks"]):
                raise ValueError("That finding does not exist in the run, or it passed.")
            eid = validation_scenarios.propose_exception(conn, migration_id, validation_scenarios.STANDING if standing else run_id, check_ref, reason, user["name"])
        except ValueError as exc:
            return _validation_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_EXCEPTION_PROPOSED", user, {"exception_id": eid, "run_id": run_id, "check_ref": check_ref})
    return _validation_redirect("notice", f"Exception {eid} proposed" + (" for every run of this migration" if standing else "") + ". A Migration Analyst must decide it.")


@app.post("/api/validation/exceptions/{exception_id}/decision")
async def decide_validation_exception(request: Request, exception_id: str, decision: str = Form(...), comments: str = Form("")):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            row = validation_scenarios.decide_exception(conn, exception_id, decision.upper(), user["name"], comments)
        except ValueError as exc:
            return _validation_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("VALIDATION_EXCEPTION_" + row["status"], user, {"exception_id": exception_id, "check_ref": row["check_ref"]})
    return _validation_redirect("notice", f"Exception {exception_id} {row['status'].lower()}.")


@app.post("/api/validation/run")
async def run_validation(request: Request):
    """Execute deterministic source-to-canonical checks on approved mappings and confirmed files."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    try:
        cfg = load_firm_config()
    except FirmConfigError as exc:
        raise HTTPException(status_code=409, detail=f"Firm configuration is required before validation. {exc}")
    scope = _in_scope_entities()
    contract = {
        e: {"required": cfg.required_fields(e), "keys": [f.name for f in cfg.entities[e].fields if f.is_key]}
        for e in migration_validation.ENTITIES if e in cfg.entities and e in scope}
    conn = get_db_connection()
    try:
        mappings: Dict[str, Dict[str, str]] = {}
        for row in conn.execute(
                "SELECT entity_name, source_field, canonical_field FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1",
                (migration_id,)).fetchall():
            mappings.setdefault(row["entity_name"], {})[row["source_field"]] = row["canonical_field"]
        scenarios = validation_scenarios.list_scenarios(conn, migration_id, active_only=True)
        derivation_rows = canonical_derivations.list_derivations(conn, migration_id)
        approved_derivations = {e: entity_resolution.text(e) for e in entity_resolution.recipes()
                                if (entity_resolution.derivation(conn, migration_id, e) or {}).get("status") == "APPROVED"}
        conn.commit()
    finally:
        conn.close()
    derived_entities = {e: t for e, t in approved_derivations.items() if not _has_entity_file(e)}
    derivations: Dict[str, List[Dict[str, Any]]] = {}
    for d in derivation_rows:
        derivations.setdefault(d["entity_name"], []).append(d)
    if not mappings:
        return RedirectResponse(url="/reconciliation?error=" + quote_plus("Approve at least one mapping before running validation."),
                                status_code=status.HTTP_303_SEE_OTHER)
    files: Dict[str, List[Dict[str, Any]]] = {}
    for entity, group in state_store.entity_source_files.items():
        for rec in group.values():
            if rec.get("inference_approval_status") != "APPROVED" or entity not in migration_validation.ENTITIES:
                continue
            stored = rec.get("stored_path")
            if not stored or not file_store.is_file(stored):
                continue
            try:
                records = read_source_records(rec.get("filename", "source.csv"), file_store.read_bytes(stored))
            except (OSError, ValueError):
                continue
            files.setdefault(entity, []).append({"filename": rec.get("filename", "source"), "records": records})
    results = migration_validation.run_checks(contract, mappings, files, scenarios, derivations, derived_entities)
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
            "message": "Target-side ADF execution is not run from this endpoint. Generate and review deployment artifacts separately.",
        },
        status_code=409,
    )


@app.get("/api/adf/status/{adf_run_id}")
async def check_adf_pipeline_status(adf_run_id: str):
    """Queries Azure Data Factory pipeline execution status."""
    status_res = adf_client.get_pipeline_run_status(adf_run_id)
    return JSONResponse(status_res)


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
CHANGE_ENTITIES = P0_ENTITIES
CHANGE_DECISIONS = {"APPROVED", "REJECTED", "DEFERRED"}


# ---------------------------------------------------------------------------
# Canonical-to-target mapping (forward only, demo target spec, no target loading)
# ---------------------------------------------------------------------------
def _typed_rule_ok(expression: str) -> bool:
    """A rule a reviewer picked or typed must be exactly a built-in transformation or an APPROVED pinned rule version.
    Free text is never executed: anything else has to be authored and approved on the Business rules page."""
    expr = str(expression or "").strip().upper()
    if transformation_rules.parse_expression(expr):
        return transformation_rules.approved_rule_for_expression(expr) is not None
    return expr in SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS


_RULE_NOT_REGISTERED = ("'{rule}' is not a registered transformation. Pick one from the list, or author and approve it first on the "
                        "Business rules page; typed text is never executed.")


def _canonical_field_lists() -> Dict[str, List[str]]:
    try:
        cfg = load_firm_config()
    except FirmConfigError:
        return {}
    return {e: cfg.canonical_field_names(e) + canonical_overlay.active_added_fields(e)
            for e in P0_ENTITIES if e in cfg.entities}


def _canonical_types() -> Dict[str, Dict[str, str]]:
    """{entity: {field: type}} for inference; fields added by approved change requests have no declared type."""
    try:
        cfg = load_firm_config()
    except FirmConfigError:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for e in P0_ENTITIES:
        if e in cfg.entities:
            out[e] = {f.name: f.type for f in cfg.entities[e].fields}
            out[e].update({n: "" for n in canonical_overlay.active_added_fields(e)})  # type unknown until defined
    return out


def _transformation_sql(migration_id: str, spec, mappings, generated_by: str):
    """Transformation SQL built from the APPROVED target mappings and approved rules. Returns (package or None, message)."""
    approved = [m for m in mappings if m["status"] == "APPROVED"]
    if not spec or not approved:
        return None, ""
    rules: Dict[str, Any] = {}
    for m in approved:
        expr = m["transformation_expression"]
        if expr != "DIRECT_COPY" and expr not in rules:
            rules[expr] = transformation_rules.approved_rule_for_expression(expr)
    try:
        return sql_package.build(migration_id, spec, approved, rules, azure_simulated.simulated_environment(migration_id), generated_by), ""
    except ValueError as exc:
        return None, str(exc)


def _target_mapping_context(request: Request, user: Dict[str, str], preview_result=None, notice: str = "", error: str = "") -> Dict[str, Any]:
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        cfg = target_connector.get_config(conn)
        spec = target_connector.spec(conn)
        mappings = target_mapping.list_mappings(conn, migration_id, spec)
        approved_src = conn.execute(
            "SELECT COUNT(*) FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchone()[0]
        packages = package_contract.list_packages(conn, migration_id)
        stored_adf = artifact_store.list_artifacts(conn, migration_id, "ADF_PACKAGE")
        adf_status = _adf_status(conn, migration_id)
        adapter_info = None
        if spec:
            _sys = spec.get("target_system") or ""
            _lat = target_adapter.latest(conn, _sys)
            adapter_info = {"latest": {"version": _lat["version"], "fields": len(_lat["fields"]), "spec_version": _lat["spec_version"]} if _lat else None,
                            "adopted": target_adapter.adopted(conn, migration_id, _sys)}
        ccr = {(r["entity_name"], r["canonical_field"]): r for r in conn.execute(
            "SELECT entity_name, canonical_field, request_id, status FROM canonical_change_requests WHERE migration_id = ? "
            "AND change_type = 'ADD_FIELD' AND status != 'REJECTED' ORDER BY created_utc", (migration_id,)).fetchall()}
        conn.commit()
    finally:
        conn.close()
    gaps = target_mapping.gap_report(spec, _canonical_field_lists()) if spec else {"missing": [], "unused": [], "code_lists": []}
    for g in gaps["missing"]:
        existing = ccr.get((g["entity"], g["suggested_canonical_field"]))
        g["change_request"] = dict(existing) if existing else None
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for m in mappings:
        grouped.setdefault(m["entity_name"], []).append(m)
    inferred = None
    if cfg and cfg.get("model"):
        inferred = target_inference.to_spec(cfg["model"], "INFERRED_CONFIRMED" if target_connector.is_confirmed(cfg) else "INFERRED_PENDING", include_rejected=True)
    return {
        "page_title": "Target mapping", "user_name": user["name"], "user_role": user["role"],
        "spec": spec, "target_config": cfg, "inferred": inferred, "gaps": gaps,
        "can_raise_change": user["role"] in CHANGE_REQUEST_ROLES, "grouped": grouped, "has_mappings": bool(mappings), "approved_source_mappings": approved_src,
        "approved_rules": transformation_rules.approved_rules(), "builtin_rules": sorted(SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS), "preview": preview_result, "coverage": target_mapping.coverage(spec, mappings), "packages": packages, "stored_adf": stored_adf, "adf_status": adf_status,
        "can_propose": user["role"] in DISCOVERY_ROLES, "can_setup_target": user["role"] in PLATFORM_ROLES, "can_review": user["role"] in APPROVAL_ROLES,
        "can_preview": user["role"] in TARGET_PREVIEW_ROLES, "notice": notice, "error": error,
        "canonical_fields": {e: sorted(fs) for e, fs in _canonical_types().items()},
        "pending_reviews": target_connector.pending_reviews(cfg["model"]) if cfg and cfg.get("model") else 0,
        "adapter": adapter_info,
    }


def _transformation_sql_context(user: Dict[str, str], notice: str = "", error: str = "") -> Dict[str, Any]:
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        cfg = target_connector.get_config(conn)
        spec = target_connector.spec(conn)
        mappings = target_mapping.list_mappings(conn, migration_id, spec)
        packages = package_contract.list_packages(conn, migration_id)
        stored_adf = artifact_store.list_artifacts(conn, migration_id, "ADF_PACKAGE")
        adf_status = _adf_status(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    can_preview = user["role"] in TARGET_PREVIEW_ROLES
    tsql, tsql_error = _transformation_sql(migration_id, spec, mappings, user["name"]) if can_preview else (None, "")
    conn = get_db_connection()
    try:
        _xart, xpkg = _xodus_package(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    return {"page_title": "Transformation SQL", "user_name": user["name"], "user_role": user["role"], "spec": spec, "target_config": cfg,
            "tsql": tsql, "tsql_error": tsql_error, "packages": packages, "stored_adf": stored_adf, "adf_status": adf_status,
            "can_preview": can_preview, "notice": notice, "error": error, "xpkg": xpkg, "procedures": _package_procedures(xpkg) if xpkg else []}


def _package_procedures(pkg: Dict[str, Any]) -> List[Dict[str, str]]:
    """The generated stored procedures of a stable-pipeline package, one per entry, for reading on stage 07."""
    out = []
    for path, text in sorted(pkg["files"].items()):
        if not (path.startswith("sql/") and path.endswith(".sql")):
            continue
        where = "Simplicity" if path.startswith("sql/simplicity_") else "XODUS"
        for batch in text.split("\n\nGO\n\n"):
            m = re.match(r"\s*CREATE OR ALTER PROCEDURE (\[[^\]]+\]\.\[[^\]]+\])", batch)
            if m and "usp_xodus_" not in m.group(1):  # the shared helpers are the same for every migration; show the generated ones
                out.append({"name": m.group(1), "database": where, "sql": batch.strip()})
    return out


@app.get("/transformation-sql", response_class=HTMLResponse)
async def transformation_sql_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    return render_template_with_context(request, "transformation_sql.html", _transformation_sql_context(user, notice=notice, error=error))


@app.get("/api/target-mapping/transformation-sql")
async def download_transformation_sql(request: Request):
    """The transformation SQL as one script: staging tables, one INSERT per target table, then the load procedure."""
    user = get_current_user(request)
    if user["role"] not in TARGET_PREVIEW_ROLES:
        raise HTTPException(status_code=403, detail="Your role cannot generate the transformation SQL.")
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        mappings = target_mapping.list_mappings(conn, migration_id, spec)
        conn.commit()
    finally:
        conn.close()
    package, message = _transformation_sql(migration_id, spec, mappings, user["name"])
    if not package:
        raise HTTPException(status_code=409, detail=message or "Approve at least one target mapping first.")
    files = package["files"]
    order = ["sql/00_staging_tables.sql"] + sorted(k for k in files if k.startswith("sql/") and k not in ("sql/00_staging_tables.sql", "sql/usp_load_all.sql")) + ["sql/usp_load_all.sql"]
    text = "\n".join(f"-- ===== {k} =====\n{files[k]}" for k in order)
    _target_setup_audit("TRANSFORMATION_SQL_DOWNLOADED", user, {"migration_id": migration_id, "files": len(order)})
    return Response(content=text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="transformation_{migration_id}.sql"'})


def _sql_redirect(key: str, message: str):
    return RedirectResponse(url=f"/transformation-sql?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


def _target_redirect(key: str, message: str):
    return RedirectResponse(url=f"/target-mapping?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


def _target_setup_redirect(key: str, message: str):
    return RedirectResponse(url=f"/target-setup?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/target-mapping", response_class=HTMLResponse)
async def target_mapping_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    return render_template_with_context(request, "target_mapping.html", _target_mapping_context(request, user, notice=notice, error=error))


@app.get("/target-setup", response_class=HTMLResponse)
async def target_setup_page(request: Request, notice: str = "", error: str = ""):
    """Platform setup: connect, infer, review and confirm the target system (Platform Admin). Migration users see only the inferred model on Target mapping."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        states = target_connector.schema_states(conn)
        live = live_target.get_config(conn)
        conn.commit()
    finally:
        conn.close()
    catalog: List[str] = []
    if live:
        try:
            catalog = live_target.list_schemas(live)
        except Exception:
            catalog = []
    offered = sorted(set(catalog) | set(states) | ({live["schema_name"]} if live and live.get("schema_name") else set()))
    return render_template_with_context(request, "target_setup.html", {
        **_target_mapping_context(request, user, notice=notice, error=error), "page_title": "Target system setup",
        "schema_options": [{"name": n, "status": states.get(n, "NOT SET UP")} for n in offered], "working_schema": live["schema_name"] if live else ""})


def _target_setup_audit(event: str, user: Dict[str, str], details: Dict[str, Any]) -> None:
    state_store.audit_log.append({"event": event, "actor": user["name"], "timestamp_utc": datetime.now(timezone.utc).isoformat(), "details": details})


@app.post("/api/target-system/schema")
async def choose_target_schema(request: Request, schema: str = Form("")):
    """A Platform Admin picks which live schema the target setup below (configure, infer, review, confirm) is about."""
    session = session_mgr.get_session(request)
    get_current_user(request)
    if session is not None:
        session["target_schema"] = schema.strip()
    return _target_setup_redirect("notice", f"Target setup now works on schema {schema.strip() or '(the schema saved with the connection)'}.")


@app.post("/api/target-system/configure")
async def configure_target_system(request: Request, target_name: str = Form(...), endpoint: str = Form(...),
                                  scope_tables: str = Form(""), include_code_lists: str = Form(""), owner: str = Form("")):
    """One-time setup, step 1: save the connection configuration and test the connection."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = target_connector.configure(conn, user["name"], target_name, endpoint, [t for t in re.split(r"[,\s]+", scope_tables) if t],
                                             include_code_lists == "on", owner)
        except ValueError as exc:
            return _target_setup_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_SYSTEM_CONFIGURED", user, {"endpoint": cfg["endpoint"], "status": cfg["status"]})
    if cfg["status"] != "CONNECTED":
        return _target_setup_redirect("error", cfg["connection_detail"])
    return _target_setup_redirect("notice", cfg["connection_detail"] + " Next: infer the target system.")


@app.post("/api/target-system/configure-azure-sql")
async def configure_target_azure_sql(request: Request, owner: str = Form(""), scope_tables: str = Form("")):
    """One-time setup, step 1 (Azure SQL): use the target database from the Azure connection page and test it."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = target_connector.configure_azure_sql(conn, user["name"], owner, [t for t in re.split(r"[,\s]+", scope_tables) if t])
        except ValueError as exc:
            return _target_setup_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_SYSTEM_CONFIGURED", user, {"endpoint": cfg["endpoint"], "status": cfg["status"]})
    if cfg["status"] != "CONNECTED":
        return _target_setup_redirect("error", cfg["connection_detail"])
    return _target_setup_redirect("notice", cfg["connection_detail"] + " Next: infer the target system.")


@app.post("/api/target-system/infer")
async def infer_target_system(request: Request):
    """One-time setup, step 2: read the target's schema and infer entities and fields (a proposal, not a decision)."""
    user = get_current_user(request)
    canonical_types = _canonical_types()
    conn = get_db_connection()
    try:
        try:
            cfg = target_connector.infer_model(conn, canonical_types)
        except Exception as exc:
            return _target_setup_redirect("error", f"Could not infer the target system: {exc}")
        conn.commit()
    finally:
        conn.close()
    n = sum(len(b["fields"]) for b in cfg["model"]["entities"].values())
    _target_setup_audit("TARGET_SYSTEM_INFERRED", user, {"schema_hash": cfg["schema_hash"], "fields": n})
    return _target_setup_redirect("notice", f"Inferred {len(cfg['model']['entities'])} entities and {n} target fields. Review them, then confirm.")


@app.post("/api/target-system/confirm")
async def confirm_target_system(request: Request):
    """One-time setup, step 3: a Platform Admin confirms the inferred model. Locks the setup."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = target_connector.confirm(conn, user["name"])
        except ValueError as exc:
            return _target_setup_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_SYSTEM_CONFIRMED", user, {"schema_hash": cfg["schema_hash"]})
    return _target_setup_redirect("notice", "Target system confirmed. Setup is complete and locked; mapping can start.")


@app.post("/api/target-system/review-field")
async def review_target_field(request: Request, table: str = Form(...), column: str = Form(...), decision: str = Form(...),
                              canonical_field: str = Form("")):
    """Setup review: mark one inferred target field correct, change its canonical field, or reject it."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            target_connector.review_field(conn, user["name"], table, column, decision, canonical_field, _canonical_types())
        except ValueError as exc:
            return _target_setup_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_FIELD_REVIEWED", user, {"field": f"{table}.{column}", "decision": decision.upper(), "canonical_field": canonical_field.strip().upper()})
    return _target_setup_redirect("notice", f"{table}.{column}: decision recorded.")


@app.post("/api/target-system/review-all")
async def review_all_target_fields(request: Request):
    """Setup review: accept every still-undecided inferred field as inferred."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            target_connector.review_all(conn, user["name"])
        except ValueError as exc:
            return _target_setup_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_FIELDS_ACCEPTED", user, {})
    return _target_setup_redirect("notice", "Remaining inferred fields accepted as inferred.")


@app.post("/api/target-system/move-table")
async def move_target_table(request: Request, table: str = Form(...), entity: str = Form("")):
    """Setup review: put a target table under another entity, or exclude it (blank entity)."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            target_connector.move_table(conn, user["name"], table, entity.strip() or None, _canonical_types())
        except ValueError as exc:
            return _target_setup_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_TABLE_REASSIGNED", user, {"table": table, "entity": entity.strip() or "EXCLUDED"})
    return _target_setup_redirect("notice", f"{table}: moved to {entity.strip() or 'excluded'}. Review its fields again.")


@app.post("/api/target-system/reopen")
async def reopen_target_system(request: Request, reason: str = Form("")):
    """Platform Admin unlocks a confirmed target setup to reconfigure it. Audited; target mapping pauses until re-confirmed."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            target_connector.reopen(conn, user["name"], reason)
        except ValueError as exc:
            return _target_setup_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_SYSTEM_REOPENED", user, {"reason": reason.strip()[:200]})
    return _target_setup_redirect("notice", "Target setup reopened. Change the configuration, infer and confirm again; target mapping is paused until then.")


@app.post("/api/target-mapping/gaps/raise")
async def raise_gap_change_requests(request: Request, entity_name: str = Form(...), target_field: str = Form(...)):
    """Turn a target-vs-canonical gap into an ordinary canonical change request for the Domain SME to decide.

    The field name and definition come from the server-side gap report, never from the form.
    target_field '*' raises every required gap that has no open request.
    """
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn0 = get_db_connection()
    try:
        spec0 = target_connector.spec(conn0)
    finally:
        conn0.close()
    if not spec0:
        return _target_redirect("error", "Confirm the target system setup first.")
    gaps = [g for g in target_mapping.gap_report(spec0, _canonical_field_lists())["missing"]
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


def _apply_business_hints(conn, migration_id: str, spec: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Target mappings follow the business-rules statements ("Target Fund Name comes from Account CustodianName"). Rows are
    left PROPOSED for a person to approve; a decided row that disagrees is reopened with the statement as the reason."""
    text = chr(10).join(str(d.get("full_text", "")) for d in state_store.firm_knowledge_docs if d.get("category") == "business_rules")
    hints = rule_inference.split_hints(text)
    if not hints:
        return {"applied": 0, "reopened": 0, "skipped": []}
    source_map = {(r["entity_name"].lower(), r["source_field"].lower()): r["canonical_field"] for r in conn.execute(
        "SELECT entity_name, source_field, canonical_field FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchall()}
    approved = {}
    for r in transformation_rules.list_rules(conn):
        if r["status"] == "APPROVED" and r["rule_id"] not in approved:
            approved[r["rule_id"]] = r["version"]  # list is newest version first

    def rule_expression(statement: str):
        hit = next((x for x in rule_inference.infer(statement, _confirmed_relationships(conn)) if x["recognised"]), None)
        if not hit:
            return None
        rid = rule_inference.rule_id_for(hit["rule_type"], hit["params"])
        return transformation_rules.make_expression(rid, approved[rid]) if rid in approved else None

    known = _canonical_field_lists()

    def resolve(entity: str, field: str):
        """The canonical field a source field maps to in this migration; a name that already is a canonical field of the entity is itself."""
        return source_map.get((entity.lower(), field.lower())) or next((k for k in known.get(entity, []) if k.upper() == field.upper()), None)

    return target_mapping.apply_hints(conn, migration_id, spec, hints, resolve, rule_expression, actor, known)


@app.post("/api/target-mapping/propose")
async def propose_target_mappings(request: Request):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        if spec is None:
            return _target_redirect("error", "Complete and confirm the one-time target system setup first.")
        unresolved_before = sum(1 for m in target_mapping.list_mappings(conn, migration_id, spec) if not m["canonical_field"])
        try:
            created = target_mapping.propose(conn, migration_id, _canonical_field_lists(), spec)
        except ValueError as exc:
            return _target_redirect("error", str(exc))
        unresolved_after = sum(1 for m in target_mapping.list_mappings(conn, migration_id, spec) if not m["canonical_field"])
        resolved = max(0, unresolved_before - unresolved_after)
        hinted = _apply_business_hints(conn, migration_id, spec, user["name"])
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "TARGET_MAPPINGS_PROPOSED", "actor": user["name"],
                                  "timestamp_utc": datetime.now(timezone.utc).isoformat(), "details": {"created": created, "resolved": resolved}})
    parts = []
    if created:
        parts.append(f"{created} target mapping proposal(s) created")
    if resolved:
        parts.append(f"{resolved} unresolved field(s) now have a canonical source")
    if hinted["applied"]:
        parts.append(f"{hinted['applied']} followed the business rules" + (f" ({hinted['reopened']} reopened)" if hinted["reopened"] else ""))
    if hinted["skipped"]:
        parts.append(f"{len(hinted['skipped'])} business-rule mapping statement(s) not applied: " + "; ".join(h["reason"] for h in hinted["skipped"][:3]))
    if parts:
        return _target_redirect("notice", "; ".join(parts) + ". Each needs human approval.")
    return _target_redirect("notice", "Nothing to refresh: every target field already has a proposal and a canonical source.")


@app.post("/api/packages/generate")
async def generate_deployment_package(request: Request):
    """Generate a versioned, schema-validated deployment package (DRAFT contract) from approved configuration only."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    setup = state_store.questionnaire_data.get("migration_setup", {})
    try:
        cfg_firm = load_firm_config()
    except FirmConfigError as exc:
        return _sql_redirect("error", f"Firm configuration is required: {exc}")
    conn = get_db_connection()
    try:
        tcfg = target_connector.get_config(conn)
        spec = target_connector.spec(conn)
        if not spec or not tcfg:
            return _sql_redirect("error", "Confirm the target system before generating a deployment package.")
        approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"]
        source_maps = [{"entity": r["entity_name"], "source_field": r["source_field"], "canonical_field": r["canonical_field"],
                        "transformation": r["transformation_expression"] or "DIRECT_COPY"} for r in conn.execute(
            "SELECT entity_name, source_field, canonical_field, transformation_expression FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1",
            (migration_id,)).fetchall()]
        rules = {r["expression"]: r for r in transformation_rules.list_rules(conn)}
        scenarios = validation_scenarios.list_scenarios(conn, migration_id, active_only=True)
        files = [{"filename": rec.get("filename", "source"), "entity": entity} for entity, group in state_store.entity_source_files.items()
                 for rec in group.values() if rec.get("inference_approval_status") == "APPROVED"]
        try:
            body = package_contract.build_body(
                migration_id=migration_id, source_system=str(setup.get("source_firm") or ""), target_system=str(setup.get("target_system") or tcfg["target_name"]),
                firm={"firm_id": cfg_firm.firm_id, "config_version": str(cfg_firm.config_version), "canonical_contract_ref": str(cfg_firm.canonical_contract_ref)},
                target={"name": tcfg["target_name"], "model_version": str(tcfg["schema_hash"]), "confirmed_by": tcfg["confirmed_by"] or "", "confirmed_utc": tcfg["confirmed_utc"] or ""},
                source_files=files, source_mappings=source_maps, spec=spec, approved_target=approved, rules=rules, scenarios=scenarios,
                environment=azure_simulated.simulated_environment(migration_id),
                derivations=canonical_derivations.list_derivations(conn, migration_id))
            result = package_contract.generate(conn, migration_id, user["name"], body)
        except ValueError as exc:
            return _sql_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    if result["new"]:
        _target_setup_audit("DEPLOYMENT_PACKAGE_GENERATED", user, {"package_id": result["package_id"], "version": result["package_version"], "schema": package_contract.SCHEMA_VERSION})
        return _sql_redirect("notice", f"Package v{result['package_version']} ({result['package_id']}) generated against the DRAFT contract. Download it below.")
    return _sql_redirect("notice", f"Nothing changed since package v{result['package_version']} ({result['package_id']}); no new version was created.")


@app.get("/api/packages/{package_id}/download")
async def download_deployment_package(request: Request, package_id: str):
    get_current_user(request)
    conn = get_db_connection()
    try:
        pkg = package_contract.get_package(conn, package_id)
        conn.commit()
    finally:
        conn.close()
    if not pkg or pkg["migration_id"] != str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED")):
        raise HTTPException(status_code=404, detail="Package not found for this migration.")
    return Response(content=pkg["yaml_text"], media_type="application/x-yaml",
                    headers={"Content-Disposition": f'attachment; filename="{package_id}_v{pkg["package_version"]}_package.yaml"'})


@app.post("/api/target-mapping/adf-package")
async def download_adf_package(request: Request):
    """Generate the ADF package zip from approved target mappings with simulated Azure values. Nothing is deployed."""
    user = get_current_user(request)
    back = _validation_redirect if (await request.form()).get("return_to") == "reconciliation" else _sql_redirect  # fixed allow-list, never a free URL
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"]
        conn.commit()
    finally:
        conn.close()
    try:
        rules = {}
        for m in approved:
            expr = m["transformation_expression"]
            if expr != "DIRECT_COPY" and expr not in rules:
                rules[expr] = transformation_rules.approved_rule_for_expression(expr)
        conn = get_db_connection()
        try:
            provenance = artifact_store.build_provenance(conn, migration_id, spec, approved)
        finally:
            conn.close()
        conn = get_db_connection()
        try:
            azure_cfg = azure_connection.get_config(conn)
        finally:
            conn.close()
        conn = get_db_connection()
        try:
            sqlt = sql_target.get_config(conn)
        finally:
            conn.close()
        if azure_cfg and sqlt:  # the target database is set up once, under Target database; the package uses those values
            azure_cfg = {**azure_cfg, "sql_server": sqlt["server"], "sql_database": sqlt["database_name"], "sql_schema": sqlt["schema_name"]}
        blob = blob_source.status(_blob_container())  # account name and emulator flag only; no key is read
        env = adf_environment.resolve(migration_id, azure_cfg, blob.get("account", ""), _blob_container(), blob.get("emulator", True))
        if env.get("simulated", True):
            package = adf_package.build(migration_id, spec, approved, rules, env, user["name"], provenance)
        else:  # the configured Azure environment: stable pipelines plus the generated SQL (raw -> canonical -> mirror -> live)
            built = xodus_pipelines.build(migration_id, *_xodus_package_inputs(migration_id), generated_by=user["name"], provenance=provenance,
                                          codes=_xodus_code_inputs(migration_id), depends={e: sorted(d) for e, d in _entity_depends().items()},
                                          rules_approved=_approved_rules(migration_id))
            package = {"files": built["files"], "manifest": {**built["manifest"], "simulated": False, "pipeline": built["manifest"]["setupPipeline"],
                                                             "entities": built["manifest"]["canonical"], "warnings": []}}
    except ValueError as exc:
        return back("error", str(exc))
    _target_setup_audit("ADF_PACKAGE_GENERATED", user, {"migration_id": migration_id, "simulated": True, "pipeline": package["manifest"]["pipeline"],
                                                     "entities": sorted(package["manifest"]["entities"]), "warnings": len(package["manifest"]["warnings"])})
    conn = get_db_connection()
    try:
        stored = artifact_store.save(conn, migration_id, "ADF_PACKAGE", adf_package.to_zip(package), user["name"], provenance=provenance)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("ADF_PACKAGE_STORED", user, {"artifact_id": stored["artifact_id"], "version": stored["artifact_version"], "sha256": stored["content_sha256"]})
    return back("notice", f"ADF package v{stored['artifact_version']} saved with this migration ({stored['artifact_id']}, {'simulated Azure values' if package['manifest']['simulated'] else 'configured Azure environment'}, not deployed).")


def _write_canonical_stage(migration_id: str):
    """Map the raw files to canonical names with the approved source mappings and write them under stage/ for the ADF pipeline.
    Returns (result, error, package_record); exactly one of result/error is set."""
    import io
    import zipfile
    conn = get_db_connection()
    try:
        stored = artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
        rec = artifact_store.read(conn, stored["artifact_id"]) if stored else None
        conn.commit()
    except ValueError as exc:
        return None, str(exc), None
    finally:
        conn.close()
    if not rec:
        return None, "Generate the ADF package first: it lists the staging columns the canonical files must have.", None
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(rec["data"])).read("manifest.json"))
    env = manifest.get("environment", {})
    if manifest.get("simulated") or not env.get("stagingFolder"):
        return None, "The latest ADF package uses the simulated environment; there is no real stage folder to write.", rec
    cols = manifest.get("stagingColumns") or {}
    lookup_tables = {t for t in cols if t.startswith("lkp_")}
    rows, _ = _canonical_rows_for_migration(migration_id, {t[4:] + ".csv" for t in lookup_tables})
    tables = {e: (cols[e], rows.get(e, [])) for e in manifest.get("entities", []) if e in cols}
    empty = [e for e, (_, r) in tables.items() if not r]
    if not tables or empty:
        return None, "No canonical rows could be built for " + (", ".join(empty) if empty else "any entity") + ". Check that the source files are approved and mapped.", rec
    lookups: Dict[str, bytes] = {}
    for t in sorted(lookup_tables):
        name, data = t[4:] + ".csv", None
        for group in state_store.entity_source_files.values():
            for r in group.values():
                if str(r.get("filename", "")).strip().lower() == name and r.get("stored_path") and file_store.is_file(r["stored_path"]):
                    data = file_store.read_bytes(r["stored_path"])
        if data is None:
            try:
                data = blob_source.read_blob(_blob_container(), blob_source.source_prefix(_source_firm()) + name)
            except Exception:
                return None, f"The lookup file {name} was not found in the migration files or in raw storage.", rec
        try:
            lookups[t[4:]] = blob_source.project_csv(data, cols[t])  # only the columns the staging table holds
        except ValueError as exc:
            return None, f"The lookup file {name}: {exc}", rec
    try:
        return blob_source.write_canonical_stage(_blob_container(), env["stagingFolder"], tables, lookups), None, rec
    except Exception as exc:
        return None, f"Could not write to blob storage: {type(exc).__name__}", rec


def _stage_summary(result) -> str:
    return ", ".join(f"{f['name']} ({f['rows']} rows)" if f["rows"] is not None else f["name"] for f in result["files"])


@app.post("/api/target-mapping/stage-canonical")
async def stage_canonical_files(request: Request):
    """Write the canonical files to stage/ on their own (the stage-09 start action does this too, behind its gate)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    back = _execution_redirect if (await request.form()).get("return_to") == "execution" else _sql_redirect  # fixed allow-list, never a free URL
    result, error, _ = _write_canonical_stage(migration_id)
    if error:
        return back("error", error)
    _target_setup_audit("CANONICAL_STAGE_WRITTEN", user, {"migration_id": migration_id, "path": result["path"], "files": [f["name"] for f in result["files"]],
                                                          "rows": {f["name"]: f["rows"] for f in result["files"] if f["rows"] is not None}})
    return back("notice", f"Canonical files written to {result['path']}: " + _stage_summary(result) + ".")


@app.post("/api/execution/raise-request")
async def raise_request_from_failed_step(request: Request, step: str = Form(...), entity: str = Form(...), note: str = Form("")):
    """Turn a failed pipeline step into an information request for the source firm or vendor (for example a corrected file).
    The question is built from the stored run diagnosis, not from the page, and the request is answered like any other."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))

    def back(key: str, message: str):
        return RedirectResponse(url=f"/execution?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)
    in_scope = [e for e in P0_ENTITIES if e in (state_store.questionnaire_data.get("migration_setup", {}) or {}).get("entities", [])]
    if entity not in in_scope:
        return back("error", "Pick an entity that is in scope for this migration.")
    conn = get_db_connection()
    try:
        summary = run_monitor.stored(conn, migration_id) or {}
        conn.commit()
    finally:
        conn.close()
    failure = next((f for f in summary.get("failures", []) if f.get("step") == step), None)
    if summary.get("status") != "FAILED" or not failure:
        return back("error", "That step did not fail in the latest pipeline run. Check the run status again.")
    field = f"PIPELINE:{summary.get('run_id')}:{step}"
    requests = state_store.questionnaire_data.setdefault("mapping_information_requests", [])
    if any(r.get("canonical_field") == field and str(r.get("migration_id", migration_id)) == migration_id for r in requests):
        return back("error", "An information request for this failed step already exists.")
    question = (f"Pipeline run {summary.get('run_id')} failed at {step}: {failure['cause']} {failure['message'][:300]} "
                f"Please supply a corrected file or explain the data. {str(note or '').strip()[:500]}").strip()
    request_id = f"RFI-{uuid.uuid4().hex[:10].upper()}"
    requests.append({"request_id": request_id, "migration_id": migration_id, "entity": entity, "canonical_field": field,
                     "source_file": summary.get("trigger_file") or "", "status": "OPEN", "question": question, "requested_by": user["name"],
                     "created_utc": datetime.now(timezone.utc).isoformat(), "response": ""})
    _sync_mapping_information_requests(migration_id)
    publish_event("INFORMATION_REQUEST_RAISED", migration_id, "information_request", request_id,
                  {"entity": entity, "source": "pipeline_failure", "step": step, "run_id": summary.get("run_id")}, user["name"])
    return back("notice", f"Information request {request_id} raised for {entity}. It is answered in stage 04 like any other request.")


@app.get("/api/execution/run-status")
async def execution_run_status(request: Request):
    """Read the pipeline's run log (read-only SQL sign-in), store the latest summary and return it. The Execution page calls this every 30 seconds."""
    get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        summary = run_monitor.refresh(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    return JSONResponse(summary)


@app.post("/api/execution/start-pipeline")
async def start_adf_pipeline(request: Request):
    """Stage 09 gate (approver): the package must be real and current and mapping validation must pass; then the canonical files are written to
    stage/ and a .ready marker is placed in curated/, which fires the factory's blob-event trigger. The approver comes from the session."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    def back(key: str, msg: str):
        return RedirectResponse(url=f"/execution?{key}=" + quote_plus(msg), status_code=status.HTTP_303_SEE_OTHER)
    conn = get_db_connection()
    try:
        status_info = _adf_status(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    reasons = []
    if not status_info:
        reasons.append("There is no ADF package: generate one in stage 07.")
    elif status_info["stale_reasons"]:
        reasons.append("The ADF package is stale (" + " ".join(status_info["stale_reasons"]) + ") Generate a new one in stage 07 and publish it.")
    if workflow_progress().get("validation_status") != "PASS":
        reasons.append("Mapping validation has not passed (stage 05).")
    if reasons:
        return back("error", "Not started: " + " ".join(reasons))
    result, error, rec = _write_canonical_stage(migration_id)
    if error:
        return back("error", "Not started: " + error)
    panel = _blob_panel(with_files=False)
    if not (panel["reachable"] and panel["layout_ready"]):
        return back("error", "Blob storage is unreachable or the folder structure is not created yet.")
    now = datetime.now(timezone.utc)
    name = f"curated/{now.strftime('%Y%m%dT%H%M%SZ')}-{re.sub(r'[^a-z0-9]', '', migration_id.lower())[:12]}.ready"
    setup = state_store.questionnaire_data.get("migration_setup", {}) or {}
    trace = {"batch_id": name.rsplit("/", 1)[-1][: -len(".ready")],  # also recorded in the run log as trigger_file, so a run ties back to this marker
             "acquisition": {"source_firm": setup.get("source_firm", ""), "target_system": setup.get("target_system", "")}}
    marker = {"migration_id": migration_id, **trace, "approved_by": user["name"], "approved_utc": now.isoformat(), "adf_package": rec["artifact_id"],
              "adf_package_version": rec["artifact_version"], "adf_package_sha256": rec["content_sha256"], "stage_path": result["path"],
              "files": [{"name": f["name"], "rows": f["rows"]} for f in result["files"]]}
    try:
        blob_source.write_ready_marker(panel["container"], name, marker)
    except Exception as exc:
        return back("error", f"Canonical files were written but the ready marker could not be: {type(exc).__name__}. Nothing was started.")
    _target_setup_audit("ADF_PIPELINE_STARTED", user, {"migration_id": migration_id, "ready_file": name, "batch_id": trace["batch_id"], "acquisition": trace["acquisition"], "adf_package": rec["artifact_id"], "stage_path": result["path"]})
    return back("notice", f"Canonical files written ({_stage_summary(result)}) and {name} placed. The factory trigger starts the pipeline; check its run in Data Factory.")


@app.post("/api/validation/signoff-evidence/save")
async def save_signoff_evidence(request: Request):
    """Save the sign-off evidence for the latest validation run with the migration's data (counts, ids and reasons only)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    form = await request.form()
    conn = get_db_connection()
    try:
        try:
            approved = [dict(r) for r in conn.execute("SELECT entity_name, source_field, canonical_field FROM canonical_mappings WHERE migration_id = ? AND is_confirmed = 1", (migration_id,)).fetchall()]
            blocked, override = _stale_gate(conn, migration_id, form.get("stale_reason", ""))
            if blocked:
                return _validation_redirect("error", blocked)
            doc = signoff_evidence.build(conn, migration_id, user["name"], drift_detection.detect(approved, state_store.entity_source_files))
            doc["adf_package"] = _adf_reference(conn, migration_id, doc["validation_run"]["run_id"])
            if override:
                doc["stale_package_override"] = {**override, "reviewer": user["name"]}
        except ValueError as exc:
            return _validation_redirect("error", str(exc))
        stored = artifact_store.save(conn, migration_id, "SIGNOFF_EVIDENCE", json.dumps(doc, indent=2, default=str).encode("utf-8"), user["name"])
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("SIGNOFF_EVIDENCE_STORED", user, {"artifact_id": stored["artifact_id"], "version": stored["artifact_version"], "run_id": doc["validation_run"]["run_id"],
                                                          "stale_package_override": bool(override)})
    return _validation_redirect("notice", f"Sign-off evidence v{stored['artifact_version']} saved with this migration ({stored['artifact_id']}).")


@app.get("/api/artifacts/{artifact_id}/download")
async def download_stored_artifact(artifact_id: str, request: Request):
    """Optional copy of a stored artifact; the stored file stays in the migration's data either way."""
    get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            rec = artifact_store.read(conn, artifact_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    finally:
        conn.close()
    if not rec:
        raise HTTPException(status_code=404, detail="Stored artifact not found.")
    return Response(content=rec["data"], media_type=rec["media_type"], headers={"Content-Disposition": f'attachment; filename="{rec["filename"]}"'})


def _confirmed_relationships(conn=None) -> List[Dict[str, str]]:
    """Human-confirmed file relationships of the active migration (child and parent as file.column)."""
    own = conn is None
    conn = conn or get_db_connection()
    try:
        cross_file._DDL and conn.execute(cross_file._DDL)
        mid = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
        rows = conn.execute("SELECT child, parent FROM relationship_reviews WHERE migration_id = ? AND status = 'CONFIRMED'", (mid,)).fetchall()
        return [{"child": r["child"], "parent": r["parent"]} for r in rows]
    finally:
        if own:
            conn.close()


def _lookup_context(package: Dict[str, Any]):
    """Lookup tables for the package's LOOKUP rules, built from the migration's approved source files. Returns (tables, parent file names)."""
    wanted = {transformation_rules.lookup_key(r["params"]): r["params"] for r in package.get("transformation_rules", []) if r.get("rule_type") == "LOOKUP"}
    if not wanted:
        return {}, set()
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for group in state_store.entity_source_files.values():
        for rec in group.values():
            stored = rec.get("stored_path")
            if not stored or not file_store.is_file(stored):
                continue
            try:
                by_name.setdefault(str(rec.get("filename", "")).strip().lower(), read_source_records(rec.get("filename", "source.csv"), file_store.read_bytes(stored)))
            except (OSError, ValueError):
                continue
    tables = {}
    for key, params in wanted.items():
        if key[0] in by_name:
            tables[key] = transformation_rules.build_lookup(by_name[key[0]], params)
    return tables, {k[0] for k in wanted}


def _derivations_for(migration_id: str, entity: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        found = canonical_derivations.list_derivations(conn, migration_id, entity)
        conn.commit()
    finally:
        conn.close()
    return found


def _canonical_rows_for_migration(migration_id: str, skip_files=(), resolve: bool = True):
    """Canonical-shaped rows per entity, derived in memory from approved source files and mappings (files joined on the entity key)."""
    canonical_rows: Dict[str, List[Dict[str, Any]]] = {}
    join_info: Dict[str, Dict[str, int]] = {}
    try:
        key_cfg = load_firm_config()
    except FirmConfigError:
        key_cfg = None
    for entity, group in state_store.entity_source_files.items():
        if entity not in P0_ENTITIES:
            continue
        file_rows: List[List[Dict[str, Any]]] = []
        rules = _get_approved_mapping_rules(migration_id, entity)
        mapper = SchemaMapperAgent(
            custom_catalog={**schema_mapper.catalog, **{s: r["canonical_field"] for s, r in rules.items()}},
            approved_transformations={s: r["transformation_expression"] for s, r in rules.items()}, infer=False)  # guesses never fill the rows
        for rec in group.values():
            stored = rec.get("stored_path")
            if rec.get("inference_approval_status") != "APPROVED" or not stored or not file_store.is_file(stored):
                continue
            if str(rec.get("filename", "")).strip().lower() in skip_files:
                continue
            try:
                records = read_source_records(rec.get("filename", "source.csv"), file_store.read_bytes(stored))
            except (OSError, ValueError):
                continue
            file_rows.append(mapper.map_source_to_canonical(entity, records)["mapped_records"])
        key_field = next((f.name for f in key_cfg.entities[entity].fields if f.is_key), None) if key_cfg and entity in key_cfg.entities else None
        joined = target_mapping.merge_by_key(file_rows, key_field)
        derived = _derivations_for(migration_id, entity)
        canonical_rows[entity] = canonical_derivations.apply_to_rows(joined["rows"], derived)
        join_info[entity] = {"key": key_field, "files": len(file_rows), "keyed_rows": joined["keyed_rows"],
                             "unkeyed_rows": joined["unkeyed_rows"], "conflicts": joined["conflicts"]}
    if resolve:
        _resolve_in_memory(migration_id, canonical_rows, join_info)
    return canonical_rows, join_info


def _resolve_in_memory(migration_id: str, rows: Dict[str, List[Dict[str, Any]]], join_info: Dict[str, Dict[str, Any]]) -> None:
    """The same steps as the generated canonical procedures: references point at survivors, approved match rules merge duplicates,
    and (when approved) account roles are derived from the households."""
    conn = get_db_connection()
    try:
        er_rules = entity_resolution.approved_rules(conn, migration_id)
        derivs = {e: entity_resolution.derivation(conn, migration_id, e) for e in entity_resolution.recipes()}
        conn.commit()
    finally:
        conn.close()
    for derived_entity, deriv in derivs.items():
        if (deriv or {}).get("status") == "APPROVED" and derived_entity in _in_scope_entities() and not rows.get(derived_entity):
            rows[derived_entity] = []
            join_info[derived_entity] = {"key": None, "files": 0, "derived": entity_resolution.method(derived_entity)}
    xrefs: Dict[str, Dict[str, str]] = {}
    for entity in _load_order(list(rows)):
        if join_info.get(entity, {}).get("derived") and entity_resolution.recipe_for(entity):
            rows[entity] = entity_resolution.derive_rows(entity, rows)
        for field, (ref, _) in (entity_resolution.references().get(entity) or {}).items():
            if ref in xrefs:
                entity_resolution.repoint(rows[entity], field, xrefs[ref])
        key = (join_info.get(entity) or {}).get("key")
        if er_rules.get(entity) and key:
            rows[entity], xrefs[entity], stats = entity_resolution.resolve(rows[entity], key, er_rules[entity])
            join_info[entity]["resolution"] = {k: v for k, v in stats.items() if k != "examples"}


@app.post("/api/target-mapping/preview", response_class=HTMLResponse)
async def preview_target_mapping(request: Request):
    """Read-only dry run: canonical rows are derived in memory; only counts and findings are returned."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"]
        conn.commit()
    finally:
        conn.close()
    if not approved:
        return _target_redirect("error", "Approve at least one target mapping before running a preview.")
    canonical_rows, join_info = _canonical_rows_for_migration(migration_id)
    result = target_mapping.preview(spec, approved, canonical_rows, join_info)
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


def _bulk_summary(done: int, failed: List[str], verb: str) -> tuple:
    """One notice or error line for a table-style bulk review."""
    if failed and not done:
        return "error", "Nothing was " + verb + ". " + "; ".join(failed[:4]) + (" …" if len(failed) > 4 else "")
    msg = f"{done} {verb}."
    if failed:
        msg += f" {len(failed)} not changed: " + "; ".join(failed[:4]) + (" …" if len(failed) > 4 else "")
    return "notice", msg


@app.post("/api/target-system/review-selected")
async def review_selected_target_fields(request: Request):
    """Table review of inferred target fields: every ticked row is approved as inferred, or changed when a different
    canonical field (or none / reject) is picked on its row. The row note is kept with the decision."""
    user = get_current_user(request)
    form = await request.form()
    keys = [str(k) for k in form.getlist("rows")]
    if not keys:
        return _target_setup_redirect("error", "Tick at least one field to approve.")
    common = str(form.get("common_note", "")).strip()
    canonical = _canonical_types()
    done, failed = 0, []
    conn = get_db_connection()
    try:
        for key in keys:
            table, _, column = key.partition("|")
            change = str(form.get("change::" + key, "")).strip()
            note = str(form.get("note::" + key, "")).strip() or common
            if change == "":
                decision, canonical_field = "correct", ""
            elif change == "__reject__":
                decision, canonical_field = "reject", ""
            elif change == "__none__":
                decision, canonical_field = "change", ""
            else:
                decision, canonical_field = "change", change
            try:
                target_connector.review_field(conn, user["name"], table, column, decision, canonical_field, canonical, note)
                done += 1
                _target_setup_audit("TARGET_FIELD_REVIEWED", user, {"field": f"{table}.{column}", "decision": decision.upper(),
                                                                    "canonical_field": canonical_field.upper(), "note": note})
            except ValueError as exc:
                failed.append(f"{table}.{column}: {exc}")
        conn.commit()
    finally:
        conn.close()
    kind, msg = _bulk_summary(done, failed, "recorded")
    return _target_setup_redirect(kind, msg)


@app.post("/api/target-mapping/review-selected")
async def review_selected_target_mappings(request: Request):
    """Table review of target mapping proposals: approve or reject every ticked row, with a note per row."""
    user = get_current_user(request)
    form = await request.form()
    ids = [str(i) for i in form.getlist("ids")]
    decision = str(form.get("decision", "")).strip().upper()
    if decision not in {"APPROVED", "REJECTED"}:
        return _target_redirect("error", "Choose approve or reject.")
    if not ids:
        return _target_redirect("error", "Tick at least one mapping first.")
    common = str(form.get("common_note", "")).strip()
    done, failed = 0, []
    conn = get_db_connection()
    try:
        _tm_rows = conn.execute("SELECT target_mapping_id, entity_name, target_field FROM target_mappings").fetchall()
        labels = {r["target_mapping_id"]: f"{r['entity_name']}.{r['target_field']}" for r in _tm_rows}
        labels_entity = {r["target_mapping_id"]: r["entity_name"] for r in _tm_rows}
        for tid in ids:
            note = str(form.get("note::" + tid, "")).strip() or common
            rule = str(form.get("rule::" + tid, "")).strip()
            canon = str(form.get("canonical::" + tid, "")).strip()
            try:
                row = target_mapping.review(conn, tid, decision, user["name"], note, rule if decision == "APPROVED" else "",
                                            canon if decision == "APPROVED" else "",
                                            _canonical_field_lists().get(labels_entity.get(tid, ""), []))
            except (LookupError, ValueError) as exc:
                failed.append(f"{labels.get(tid, tid[-6:])}: {exc}")
                continue
            done += 1
            state_store.audit_log.append({"event": "TARGET_MAPPING_" + row["status"], "actor": user["name"],
                                          "timestamp_utc": row["reviewed_utc"],
                                          "details": {"entity": row["entity_name"], "target_field": row["target_field"],
                                                      "canonical_field": row["canonical_field"], "rule": row["transformation_expression"]}})
        conn.commit()
    finally:
        conn.close()
    kind, msg = _bulk_summary(done, failed, decision.lower())
    return _target_redirect(kind, msg)


@app.post("/api/target-mapping/revise-selected")
async def revise_selected_target_mappings(request: Request):
    """Reopen decided (approved or rejected) target mappings for a new decision. A reason is required; the earlier
    decision stays in the history, and any saved ADF package becomes stale because the approved set changed."""
    user = get_current_user(request)
    form = await request.form()
    ids = [str(i) for i in form.getlist("revise_ids")]
    if not ids:
        return _target_redirect("error", "Tick at least one approved or rejected mapping to reopen.")
    common = str(form.get("common_note", "")).strip()
    done, failed = 0, []
    conn = get_db_connection()
    try:
        labels = {r["target_mapping_id"]: f"{r['entity_name']}.{r['target_field']}" for r in conn.execute(
            "SELECT target_mapping_id, entity_name, target_field FROM target_mappings").fetchall()}
        for tid in ids:
            reason = str(form.get("note::" + tid, "")).strip() or common
            try:
                row = target_mapping.revise(conn, tid, user["name"], reason)
            except (LookupError, ValueError) as exc:
                failed.append(f"{labels.get(tid, tid[-6:])}: {exc}")
                continue
            done += 1
            state_store.audit_log.append({"event": "TARGET_MAPPING_REOPENED", "actor": user["name"],
                                          "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                          "details": {"entity": row["entity_name"], "target_field": row["target_field"], "reason": reason}})
        conn.commit()
    finally:
        conn.close()
    kind, msg = _bulk_summary(done, failed, "reopened")
    return _target_redirect(kind, msg)


@app.post("/api/mapping-suggestions/review-selected")
async def review_selected_source_mappings(request: Request):
    """Table review of source-to-canonical proposals: approve or reject every ticked row, with a note per row.
    Each row goes through the same single-item review, so validation, rules and audit trail are unchanged."""
    user = get_current_user(request)
    if user.get("role") not in APPROVAL_ROLES:
        raise HTTPException(status_code=403, detail="Only Migration Analyst or Platform Admin may approve or reject mappings.")
    form = await request.form()
    ids = [str(i) for i in form.getlist("ids")]
    decision = str(form.get("decision", "")).strip().upper()
    back = "/agent-pipeline?{}=" 
    if decision not in {"APPROVED", "REJECTED"}:
        return RedirectResponse(url=back.format("error") + quote_plus("Choose approve or reject."), status_code=status.HTTP_303_SEE_OTHER)
    if not ids:
        return RedirectResponse(url=back.format("error") + quote_plus("Tick at least one mapping first."), status_code=status.HTTP_303_SEE_OTHER)
    common = str(form.get("common_note", "")).strip()
    done, failed = 0, []
    conn = get_db_connection()
    try:
        labels = {r["suggestion_id"]: f"{r['entity_name']}.{r['source_field']}" for r in conn.execute(
            "SELECT suggestion_id, entity_name, source_field FROM mapping_suggestions").fetchall()}
    finally:
        conn.close()
    for sid in ids:
        note = str(form.get("note::" + sid, "")).strip() or common
        rule = str(form.get("rule::" + sid, "")).strip()
        try:
            await review_source_mapping_suggestion(
                sid, decision, user["name"], note, rule if decision == "APPROVED" else "", request=None,
                canonical_override=str(form.get("canonical::" + sid, "")).strip() if decision == "APPROVED" else "")
            done += 1
        except HTTPException as exc:
            failed.append(f"{labels.get(sid, sid[-6:])}: {exc.detail}")
    kind, msg = _bulk_summary(done, failed, decision.lower())
    return RedirectResponse(url=back.format(kind) + quote_plus(msg), status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Transformation rules: governed, versioned, human-approved, registered types only
# ---------------------------------------------------------------------------
def _rules_redirect(key: str, message: str):
    return RedirectResponse(url=f"/transformation-rules?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


def _source_columns(limit_files: int = 12) -> List[Dict[str, Any]]:
    """Observed values per source column from the uploaded sample files (read-only; used for rule suggestions)."""
    cols: List[Dict[str, Any]] = []
    done = 0
    for entity, group in state_store.entity_source_files.items():
        for rec in group.values():
            stored = rec.get("stored_path")
            if done >= limit_files or rec.get("inference_approval_status") == "REJECTED" or not stored or not file_store.is_file(stored):
                continue
            try:
                rows = read_source_records(rec.get("filename", "source.csv"), file_store.read_bytes(stored))
            except (OSError, ValueError):
                continue
            done += 1
            for field in (rows[0].keys() if rows else []):
                cols.append({"entity": entity, "file": rec.get("filename", "source"), "field": str(field), "values": [r.get(field) for r in rows]})
    return cols


def _rule_hints(mapping_suggestions) -> Dict[str, Dict[str, Any]]:
    """Best transformation suggestion per pending mapping row, keyed 'Entity|SOURCE_FIELD' (shown beside the rule input)."""
    if not mapping_suggestions:
        return {}
    wanted = {(m["entity_name"], str(m["source_field"]).upper()) for m in mapping_suggestions}
    hints: Dict[str, Dict[str, Any]] = {}
    try:
        for sg in _rule_suggestions():
            k = (sg["entity"], str(sg["source_field"]).upper())
            if k in wanted and f"{k[0]}|{k[1]}" not in hints:
                hints[f"{k[0]}|{k[1]}"] = sg
    except Exception:  # suggestions are advisory; never block the review page
        return {}
    return hints


def _rule_suggestions() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        rules = transformation_rules.list_rules(conn)
        conn.commit()
    finally:
        conn.close()
    return rule_suggestions.suggest(_source_columns(), rules, {})  # no value maps are known to the platform: they come from the business rules


def _rules_page(request: Request, notice: str = "", error: str = "", inferred=None, inferred_label: str = "", inferred_source=None):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        rules = transformation_rules.list_rules(conn)
        usage = {}
        for row in conn.execute(
                "SELECT UPPER(transformation_expression) AS expr, COUNT(*) AS n FROM canonical_mappings "
                "WHERE is_confirmed = 1 AND UPPER(transformation_expression) LIKE 'RULE:%' GROUP BY UPPER(transformation_expression)").fetchall():
            usage[row["expr"]] = row["n"]
        conn.commit()
    finally:
        conn.close()
    for rule in rules:
        rule["params_text"] = json.dumps(rule["params"], indent=1, sort_keys=True)
        rule["used_by"] = usage.get(rule["expression"], 0)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    derivation_list = _derivations_for(migration_id)
    derivable = {}
    try:
        cfg = load_firm_config()
        derivable = {e: [f.name for f in cfg.entities[e].fields] for e in P0_ENTITIES if e in cfg.entities}
    except FirmConfigError:
        pass
    for item in inferred or []:
        if item.get("recognised"):
            item["params_text"] = json.dumps(item["params"], sort_keys=True)
    rule_sugs = _rule_suggestions() if user["role"] in DISCOVERY_ROLES else []
    return render_template_with_context(request, "transformation_rules.html", {
        "page_title": "Business rules", "user_name": user["name"], "user_role": user["role"],
        "evidence": _evidence_for_rule_suggestions(rule_sugs), "can_give_feedback": user["role"] in DISCOVERY_ROLES,
        "rules": rules, "rule_types": transformation_rules.RULE_TYPES,
        "derivations": derivation_list, "derivable": derivable, "migration_id": migration_id,
        "builtin_rules": sorted(SchemaMapperAgent.SUPPORTED_APPROVED_TRANSFORMS),
        "date_formats": list(transformation_rules.DATE_FORMATS),
        "can_author": user["role"] in DISCOVERY_ROLES, "can_approve": user["role"] in APPROVAL_ROLES,
        "rule_assets": [d for d in state_store.firm_knowledge_docs if d.get("category") in ("business_rules", "transformation_rule") and d.get("full_text")],
        "suggestions": rule_sugs,
        "inferred": inferred, "inferred_label": inferred_label, "inferred_source": inferred_source or {},
        "notice": notice, "error": error,
    })


@app.post("/api/derivations/set")
async def set_derivation(request: Request, entity_name: str = Form(...), canonical_field: str = Form(...),
                         input_field: str = Form(...), rule_expression: str = Form(...), reason: str = Form(...)):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    if entity_name not in P0_ENTITIES:
        return _rules_redirect("error", "Unsupported entity.")
    conn = get_db_connection()
    try:
        try:
            canonical_derivations.set_derivation(conn, migration_id, entity_name, canonical_field.strip().upper(), input_field.strip().upper(),
                                                 rule_expression.strip().upper(), user["name"], reason)
        except ValueError as exc:
            conn.rollback()
            return _rules_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "CANONICAL_DERIVATION_APPROVED", "actor": user["name"], "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                  "details": {"migration_id": migration_id, "entity": entity_name, "field": canonical_field.strip().upper(),
                                              "rule": rule_expression.strip().upper()}})
    return _rules_redirect("notice", f"{canonical_field.strip().upper()} will be derived from {input_field.strip().upper()}. Re-run validation.")


@app.post("/api/derivations/remove")
async def remove_derivation(request: Request, entity_name: str = Form(...), canonical_field: str = Form(...)):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        canonical_derivations.remove_derivation(conn, migration_id, entity_name, canonical_field)
        conn.commit()
    finally:
        conn.close()
    state_store.audit_log.append({"event": "CANONICAL_DERIVATION_REMOVED", "actor": user["name"], "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                  "details": {"migration_id": migration_id, "entity": entity_name, "field": canonical_field}})
    return _rules_redirect("notice", f"Derivation of {canonical_field} removed.")


@app.get("/transformation-rules", response_class=HTMLResponse)
async def transformation_rules_page(request: Request, notice: str = "", error: str = ""):
    return _rules_page(request, notice, error)


def _rule_text_and_label(rule_text: str, asset_id: str):
    """Resolve pasted text or a registered asset to (text, label, error)."""
    if asset_id:
        doc = next((d for d in state_store.firm_knowledge_docs if d.get("id") == asset_id), None)
        if not doc or not doc.get("full_text"):
            return "", "", "That knowledge asset has no readable text. Register the business rules as a Markdown or text file."
        return doc["full_text"], f"asset {doc['id']}, {doc['filename']}", ""
    if not rule_text.strip():
        return "", "", "Paste at least one business rule or choose a business-rules knowledge asset."
    return rule_text, "", ""


@app.post("/api/transformation-rules/infer-preview", response_class=HTMLResponse)
async def preview_inferred_rules(request: Request, rule_text: str = Form(""), asset_id: str = Form("")):
    """Show, line by line, what the inference recognised (or why not) without creating anything."""
    get_current_user(request)
    text, label, err = _rule_text_and_label(rule_text, asset_id)
    if err:
        return _rules_page(request, error=err)
    return _rules_page(request, inferred=rule_inference.infer(text, _confirmed_relationships()), inferred_label=label or "pasted text",
                       inferred_source={"asset_id": asset_id, "text": "" if asset_id else rule_text})


@app.post("/api/transformation-rules/suggestion")
async def create_suggested_rule(request: Request):
    """Turn one reviewed suggestion into a DRAFT rule. Lookup targets are exactly what the reviewer typed."""
    user = get_current_user(request)
    form = await request.form()
    rule_type, rule_id = str(form.get("rule_type", "")), str(form.get("rule_id", ""))
    field, evidence = str(form.get("source_field", "")), str(form.get("evidence", ""))
    try:
        if rule_type == "VALUE_MAP":
            pairs = {}
            for key, val in form.multi_items():
                if key.startswith("value::"):
                    target = str(form.get("target::" + key[7:], "")).strip()
                    if target:
                        pairs[str(val).strip()] = target
            if not pairs:
                return _rules_redirect("error", "Enter the canonical value for at least one source value before saving the lookup.")
            params = {"map": pairs, "case_insensitive": True}
        else:
            params = json.loads(str(form.get("params_json", "{}")))
        desc = f"Suggested from the data in {field or 'a source column'}: {evidence}"[:900]
        conn = get_db_connection()
        try:
            rule = transformation_rules.create_rule_version(conn, rule_id, rule_type, desc, params, user["name"])
            conn.commit()
        finally:
            conn.close()
    except (ValueError, TypeError) as exc:
        return _rules_redirect("error", str(exc))
    state_store.audit_log.append({"event": "TRANSFORMATION_RULE_SUGGESTED", "actor": user["name"], "timestamp_utc": rule["created_utc"],
                                  "details": {"rule": rule["expression"], "type": rule_type, "field": field}})
    return _rules_redirect("notice", f"{rule['expression']} saved as a draft from the suggestion for {field}. It needs approval before a mapping can use it.")


@app.post("/api/transformation-rules/preview")
async def preview_transformation_rule(request: Request, rule_type: str = Form(...), params_json: str = Form(...), samples: str = Form("")):
    get_current_user(request)
    try:
        params = json.loads(params_json)
        rows = transformation_rules.preview(rule_type, params, [line for line in samples.splitlines() if line.strip() != ""] or [""])
    except (ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "rows": rows})


def _shorten(text: str, limit: int) -> str:
    """Cut at a word boundary and mark the cut, so notices never end mid-word."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


@app.post("/api/transformation-rules/infer")
async def infer_transformation_rules(request: Request, rule_text: str = Form(""), asset_id: str = Form("")):
    """Turn business-rule text into DRAFT governed rules. Each still needs Analyst/Admin approval; unmatched sentences are reported, not guessed."""
    user = get_current_user(request)
    rule_text, label, err = _rule_text_and_label(rule_text, asset_id)
    if err:
        return _rules_redirect("error", err)
    conn = get_db_connection()
    try:
        result = rule_inference.save_drafts(conn, rule_text, user["name"], label, _confirmed_relationships(conn))
        conn.commit()
    finally:
        conn.close()
    for item in result["created"]:
        state_store.audit_log.append({"event": "TRANSFORMATION_RULE_INFERRED", "actor": user["name"], "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                      "details": {"rule": item["rule"], "source_text": item["statement"][:300]}})
    parts = [f"{len(result['created'])} draft rule(s) inferred ({', '.join(i['rule'] for i in result['created']) or 'none'}); each needs approval before use"]
    if result["skipped"]:
        parts.append(f"{len(result['skipped'])} already exist")
    if result["unrecognised"]:
        examples = " | ".join(_shorten(u["statement"], 60) for u in result["unrecognised"][:2])
        parts.append(f"{len(result['unrecognised'])} line(s) not recognised, e.g. {examples}")
    return _rules_redirect("error" if not result["created"] and not result["skipped"] else "notice", "; ".join(parts) + ".")


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
        releases = canonical_overlay.releases(conn)
        unreleased = canonical_overlay.unreleased(conn)
        pinned = canonical_overlay.pinned_release(conn, migration_id) if user["role"] not in PLATFORM_ROLES else None
        conn.commit()
    finally:
        conn.close()
    for row in rows:
        row["applied_utc"] = applied.get(row["request_id"])
        if row["applied_utc"] and row["change_type"] == "ADD_FIELD":
            row["yaml_snippet"] = canonical_overlay.yaml_snippet(row["entity_name"], row["canonical_field"].strip().upper(), row["proposed_change"])
    return render_template_with_context(request, "canonical_changes.html", {
        "space": "gov",
        "page_title": "Canonical Change Requests", "user_name": user["name"], "user_role": user["role"],
        "requests": rows, "change_types": CHANGE_TYPES, "entities": CHANGE_ENTITIES,
        "can_raise": user["role"] in CHANGE_REQUEST_ROLES, "can_decide": user["role"] in CANONICAL_ROLES,
        "releases": releases, "unreleased": unreleased, "pinned_release": pinned, "can_adopt": user["role"] in APPROVAL_ROLES,
        "notice": notice, "error": error,
    })


@app.post("/api/canonical-releases")
async def publish_canonical_release(request: Request, notes: str = Form("")):
    """Publish every applied, unreleased change as the next canonical release. Workspaces are told; none moves until it adopts."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            rel = canonical_overlay.publish_release(conn, user["name"], notes)
        except ValueError as exc:
            return _changes_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("CANONICAL_RELEASE_PUBLISHED", user, {"release_no": rel["release_no"], "changes": len(rel["changes"])})
    return _changes_redirect("notice", f"Canonical release {rel['release_no']} published with {len(rel['changes'])} change(s). Each migration workspace adopts it when ready.")


@app.post("/api/canonical-releases/adopt")
async def adopt_canonical_release(request: Request, return_to: str = Form("/canonical-changes")):
    """The open workspace moves to the latest canonical release; its new fields become mapping targets from now on."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    dest = return_to if return_to in ("/", "/canonical-changes") else "/canonical-changes"
    conn = get_db_connection()
    try:
        try:
            n = canonical_overlay.adopt_latest(conn, migration_id, user["name"])
        except ValueError as exc:
            return RedirectResponse(url=f"{dest}?error=" + quote_plus(str(exc)), status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    publish_event("CANONICAL_RELEASE_ADOPTED", migration_id, "migration", migration_id, {"release_no": n}, user["name"])
    return RedirectResponse(url=f"{dest}?notice=" + quote_plus(f"This workspace now uses canonical release {n}. Regenerate target mapping proposals to pick up new fields."),
                            status_code=status.HTTP_303_SEE_OTHER)


def _changes_redirect(key: str, message: str):
    return RedirectResponse(url=f"/canonical-changes?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


# ---- Canonical model v2: the model and its releases, code-set mapping (stage 03), target code values (platform setup) ----

def _model_redirect(path: str, key: str, message: str):
    return RedirectResponse(url=f"{path}?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/canonical-model", response_class=HTMLResponse)
async def canonical_model_page(request: Request, notice: str = "", error: str = ""):
    """The universal canonical model (v2): entities, typed fields and code sets, read from the definition file as it is. Read-only."""
    user = get_current_user(request)
    try:
        model, def_error = canonical_model.current(), ""
    except (OSError, ValueError) as exc:
        return render_template_with_context(request, "canonical_model.html", {
            "page_title": "Canonical model", "user_name": user["name"], "user_role": user["role"], "model": None, "coded": {},
            "notice": notice, "error": error or f"The definition file does not validate: {exc}"})
    return render_template_with_context(request, "canonical_model.html", {
        "page_title": "Canonical model", "user_name": user["name"], "user_role": user["role"],
        "model": model, "coded": canonical_model.coded_fields(model), "notice": notice, "error": error or def_error,
    })


@app.get("/code-sets", response_class=HTMLResponse)
async def code_sets_page(request: Request, notice: str = "", error: str = ""):
    """Stage 03: the source values of every coded field, each mapped to a canonical code (suggested from the code set, approved by a person)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        model = canonical_model.current()
        rows = code_mappings.list_mappings(conn, migration_id)
        totals = code_mappings.counts(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    coded = canonical_model.coded_fields(model)
    sets = []
    for name, cs in model["code_sets"].items():
        values = [r for r in rows if r["code_set"] == name]
        sets.append({"name": name, "description": cs.get("description", ""), "codes": cs["codes"], "values": values,
                     "fields": [f"{e}.{f}" for e, fs in coded.items() for f, s in fs.items() if s == name],
                     "pending": sum(1 for v in values if v["status"] == "PROPOSED")})
    return render_template_with_context(request, "code_sets.html", {
        "page_title": "Code sets", "user_name": user["name"], "user_role": user["role"], "migration_id": migration_id,
        "model": model, "sets": sets, "totals": totals,
        "can_profile": user["role"] in DISCOVERY_ROLES, "can_approve": user["role"] in APPROVAL_ROLES,
        "notice": notice, "error": error,
    })


@app.post("/api/code-sets/profile")
async def profile_code_sets(request: Request):
    """Read the distinct values of the coded fields from the approved files and mappings (in memory) and propose a canonical code for each new one."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    rows, _ = _canonical_rows_for_migration(migration_id)
    if not any(rows.values()):
        return _model_redirect("/code-sets", "error", "No canonical rows yet: confirm the source files and approve their mappings first.")
    conn = get_db_connection()
    try:
        model = canonical_model.current()
        res = code_mappings.propose(conn, migration_id, model, rows)
        conn.commit()
    finally:
        conn.close()
    publish_event("CODE_VALUES_PROFILED", migration_id, "migration", migration_id, res, user["name"])
    return _model_redirect("/code-sets", "notice", f"{res['values']} distinct value(s) read; {res['new']} new, {res['suggested']} with a suggested code. "
                                                   "Approve or change each one.")


@app.post("/api/code-sets/decide")
async def decide_code_value(request: Request, code_set: str = Form(...), source_value: str = Form(...), decision: str = Form(...),
                            canonical_code: str = Form("")):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        model = canonical_model.current()
        try:
            rec = code_mappings.decide(conn, migration_id, model, code_set, source_value, decision, canonical_code, user["name"])
        except ValueError as exc:
            conn.rollback()
            return _model_redirect("/code-sets", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    publish_event("CODE_VALUE_DECIDED", migration_id, "code_set", code_set,
                  {"source_value": source_value, "status": rec["status"], "canonical_code": rec.get("canonical_code")}, user["name"])
    what = {"APPROVED": f"mapped to {rec.get('canonical_code')}", "REJECTED": "rejected (it stays unmapped and fails conformance)",
            "PROPOSED": "reopened"}[rec["status"]]
    return _model_redirect("/code-sets", "notice", f"{code_set}: '{source_value}' {what}. Regenerate the package (stage 07) to deploy the change.")


@app.post("/api/code-sets/approve-suggested")
async def approve_suggested_code_values(request: Request):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        model = canonical_model.current()
        n = code_mappings.approve_suggested(conn, migration_id, model, user["name"])
        conn.commit()
    finally:
        conn.close()
    publish_event("CODE_VALUES_APPROVED", migration_id, "migration", migration_id, {"approved": n}, user["name"])
    return _model_redirect("/code-sets", "notice", f"{n} suggested code(s) approved. Values without a suggestion still need a person to choose the code.")


@app.get("/target-setup/code-values", response_class=HTMLResponse)
async def target_code_values_page(request: Request, notice: str = "", error: str = ""):
    """Platform setup: the target system's value for each canonical code (the target adapter's crosswalk, reused by every migration)."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        model = canonical_model.current()
        system = (target_connector.spec(conn) or {}).get("target_system") or ""
        cw = code_mappings.crosswalk(conn, system) if system else {}
        conn.commit()
    finally:
        conn.close()
    sets = [{"name": n, "description": cs.get("description", ""), "codes": cs["codes"], "values": cw.get(n, {}),
             "unmapped": sum(1 for c in cs["codes"] if c["code"] not in cw.get(n, {}))} for n, cs in model["code_sets"].items()]
    return render_template_with_context(request, "target_code_values.html", {
        "page_title": "Target code values", "user_name": user["name"], "user_role": user["role"], "model": model,
        "target_system": system, "sets": sets, "can_edit": user["role"] in PLATFORM_ROLES and bool(system), "notice": notice, "error": error,
    })


@app.post("/api/target-system/code-values")
async def save_target_code_values(request: Request):
    user = get_current_user(request)
    form = await request.form()
    code_set = str(form.get("code_set") or "")
    values = {k[len("v_"):]: str(v) for k, v in form.items() if k.startswith("v_")}
    conn = get_db_connection()
    try:
        model = canonical_model.current()
        system = (target_connector.spec(conn) or {}).get("target_system") or ""
        try:
            kept = code_mappings.save_crosswalk(conn, system, model, code_set, values, user["name"])
        except ValueError as exc:
            conn.rollback()
            return _model_redirect("/target-setup/code-values", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_CODE_VALUES_SAVED", user, {"target_system": system, "code_set": code_set, "entries": kept})
    return _model_redirect("/target-setup/code-values", "notice", f"{code_set}: {kept} target value(s) saved for {system}. "
                                                                   "Codes without a value pass to the target unchanged. Each migration picks this up when its package is regenerated.")


# ---- Target adapter: canonical -> target, reviewed once per target system (Platform Admin), adopted by each migration on stage 06 ----

def _adapter_redirect(key: str, message: str):
    return RedirectResponse(url=f"/target-setup/adapter?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/target-setup/adapter", response_class=HTMLResponse)
async def target_adapter_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        system = (spec or {}).get("target_system") or ""
        rows = target_adapter.fields(conn, system) if system else []
        tallies = target_adapter.counts(conn, system) if system else {}
        vers = target_adapter.versions(conn, system) if system else []
        _last = target_adapter.latest(conn, system) if system else None
        draft_changed = not _last or _last["spec_version"] != str((spec or {}).get("spec_version")) or _last["fields"] != [
            {k: r[k] for k in ("entity_name", "target_field", "canonical_field", "transformation_expression")} for r in rows if r["status"] == "APPROVED"]
        migrations = [m for m in migration_registry.list_migrations(conn) if not m.get("archived")]
        conn.commit()
    finally:
        conn.close()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["entity_name"], []).append(r)
    return render_template_with_context(request, "target_adapter.html", {
        "page_title": "Target adapter", "user_name": user["name"], "user_role": user["role"], "target_system": system, "spec": spec,
        "grouped": grouped, "counts": tallies, "versions": vers, "migrations": migrations, "draft_changed": draft_changed,
        "canonical_fields": {e: sorted(fs) for e, fs in _canonical_types().items()},
        "can_edit": user["role"] in PLATFORM_ROLES and bool(spec), "notice": notice, "error": error,
    })


@app.post("/api/target-system/adapter/draft")
async def draft_target_adapter(request: Request, from_migration: str = Form("")):
    """Add the target fields the adapter does not hold yet, proposed from one migration's approved target mappings or from the target model's hints."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        if not spec:
            return _adapter_redirect("error", "Confirm the target system first.")
        seed = None
        if from_migration:
            seed = [dict(m, migration_id=from_migration) for m in target_mapping.list_mappings(conn, from_migration, spec)]
        res = target_adapter.draft(conn, spec, _canonical_field_lists(), seed)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_ADAPTER_DRAFTED", user, {"target_system": spec["target_system"], "from_migration": from_migration, **res})
    return _adapter_redirect("notice", f"{res['added']} field(s) added to the draft, {res['with_source']} with a proposed canonical field"
                                       + (f" (from {from_migration}'s approved target mappings)." if from_migration else " (from the target model's hints)."))


@app.post("/api/target-system/adapter/decide")
async def decide_target_adapter_field(request: Request, entity_name: str = Form(...), target_field: str = Form(...), decision: str = Form(...),
                                      canonical_field: str = Form("")):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn) or {}
        try:
            rec = target_adapter.decide(conn, spec.get("target_system", ""), entity_name, target_field, decision, user["name"], canonical_field,
                                        sorted(_canonical_types().get(entity_name, {})))
        except ValueError as exc:
            conn.rollback()
            return _adapter_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    return _adapter_redirect("notice", f"{target_field}: " + {"APPROVED": "approved for the adapter.", "REJECTED": "left to each migration.",
                                                              "PROPOSED": "reopened."}[rec["status"]])


@app.post("/api/target-system/adapter/approve-proposed")
async def approve_proposed_target_adapter(request: Request):
    """Approve every proposed field that has a canonical field; fields with none stay open for a decision."""
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn) or {}
        system = spec.get("target_system", "")
        rows = [r for r in target_adapter.fields(conn, system) if r["status"] == "PROPOSED" and r["canonical_field"]]
        for r in rows:
            target_adapter.decide(conn, system, r["entity_name"], r["target_field"], "APPROVE", user["name"])
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_ADAPTER_APPROVED", user, {"target_system": system, "approved": len(rows)})
    return _adapter_redirect("notice", f"{len(rows)} proposed field(s) approved. Fields without a canonical field need a decision (approve with a field, or leave to each migration).")


@app.post("/api/target-system/adapter/confirm")
async def confirm_target_adapter(request: Request, notes: str = Form("")):
    user = get_current_user(request)
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        if not spec:
            return _adapter_redirect("error", "Confirm the target system first.")
        try:
            ver = target_adapter.confirm(conn, spec, user["name"], notes)
        except ValueError as exc:
            conn.rollback()
            return _adapter_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("TARGET_ADAPTER_CONFIRMED", user, {"target_system": ver["target_system"], "version": ver["version"], "fields": len(ver["fields"])})
    return _adapter_redirect("notice", f"Target adapter version {ver['version']} confirmed with {len(ver['fields'])} field(s). Each migration adopts it on stage 06.")


@app.post("/api/target-mapping/adopt-adapter")
async def adopt_target_adapter(request: Request):
    """Stage 06: take every field the confirmed target adapter maps; the migration reviews only what the adapter leaves open."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        if not spec:
            return _target_redirect("error", "The target system is not confirmed yet.")
        try:
            res = target_adapter.adopt(conn, migration_id, spec, user["name"])
        except ValueError as exc:
            conn.rollback()
            return _target_redirect("error", str(exc))
        hinted = _apply_business_hints(conn, migration_id, spec, user["name"])
        conn.commit()
    finally:
        conn.close()
    publish_event("TARGET_ADAPTER_ADOPTED", migration_id, "migration", migration_id, res, user["name"])
    extra = f" The business rules then changed {hinted['applied']} mapping(s) (each needs approval)." if hinted["applied"] else ""
    return _target_redirect("notice", f"Target adapter version {res['version']} adopted: {res['applied']} mapping(s) set from the adapter, "
                                      f"{res['unchanged']} already matched, {res['open']} left for this migration to review." + extra)


# ---- Entity resolution (canonical model phase 2): match rules, survivorship preview, the derived account roles ----

def _er_redirect(key: str, message: str):
    return RedirectResponse(url=f"/entity-resolution?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


def _in_scope_entities() -> List[str]:
    return [e for e in (state_store.questionnaire_data.get("migration_setup", {}) or {}).get("entities", []) if e in P0_ENTITIES]


def _has_entity_file(entity: str) -> bool:
    return any(r.get("inference_approval_status") == "APPROVED" for r in state_store.entity_source_files.get(entity, {}).values())


def _filled_canonical_fields(migration_id: str, entity: str) -> List[str]:
    return sorted({r["canonical_field"] for r in _get_approved_mapping_rules(migration_id, entity).values()}
                  | {d["canonical_field"] for d in _derivations_for(migration_id, entity)})


def _er_context(request: Request, user: Dict[str, str], preview=None, notice: str = "", error: str = "") -> Dict[str, Any]:
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    scope = _in_scope_entities()
    conn = get_db_connection()
    try:
        all_rules = entity_resolution.rules(conn, migration_id)
        derivs = {e: entity_resolution.derivation(conn, migration_id, e) for e in entity_resolution.recipes()}
        conn.commit()
    finally:
        conn.close()
    derivable = [{"name": e, "text": entity_resolution.text(e), "decision": derivs.get(e)} for e in entity_resolution.recipes()
                 if e in scope and not _has_entity_file(e)]
    entities = []
    for e in scope:
        suggested = entity_resolution.suggested_rules().get(e, [])
        if not suggested:
            continue
        entities.append({"name": e, "rules": [r for r in all_rules if r["entity_name"] == e], "suggested": suggested,
                         "filled": _filled_canonical_fields(migration_id, e)})
    return {"page_title": "Entity resolution", "user_name": user["name"], "user_role": user["role"], "migration_id": migration_id,
            "entities": entities, "scope": scope, "preview": preview, "derivable": derivable,
            "can_suggest": user["role"] in DISCOVERY_ROLES, "can_approve": user["role"] in APPROVAL_ROLES,
            "can_preview": user["role"] in TARGET_PREVIEW_ROLES, "notice": notice, "error": error}


@app.get("/entity-resolution", response_class=HTMLResponse)
async def entity_resolution_page(request: Request, notice: str = "", error: str = ""):
    user = get_current_user(request)
    return render_template_with_context(request, "entity_resolution.html", _er_context(request, user, notice=notice, error=error))


@app.post("/api/entity-resolution/suggest")
async def suggest_match_rules(request: Request):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        added = sum(entity_resolution.suggest(conn, migration_id, e, _filled_canonical_fields(migration_id, e)) for e in _in_scope_entities())
        conn.commit()
    finally:
        conn.close()
    publish_event("MATCH_RULES_SUGGESTED", migration_id, "migration", migration_id, {"added": added}, user["name"])
    return _er_redirect("notice", f"{added} match rule(s) proposed (only rules whose fields this migration fills). Preview them, then approve or reject each one."
                        if added else "No new match rule: the suggested rules are already listed, or their fields are not mapped in this migration.")


@app.post("/api/entity-resolution/decide")
async def decide_match_rule(request: Request, entity_name: str = Form(...), rule_id: str = Form(...), decision: str = Form(...)):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            st = entity_resolution.decide(conn, migration_id, entity_name, rule_id, decision, user["name"])
        except ValueError as exc:
            conn.rollback()
            return _er_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    publish_event("MATCH_RULE_DECIDED", migration_id, "entity", entity_name, {"rule_id": rule_id, "status": st}, user["name"])
    return _er_redirect("notice", f"{entity_name} {rule_id}: {st.lower()}. Regenerate the package (stage 07) to deploy the change.")


@app.post("/api/entity-resolution/account-roles")
async def decide_entity_derivation(request: Request, decision: str = Form(...), entity: str = Form("")):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            st = entity_resolution.decide_derivation(conn, migration_id, decision, user["name"], entity)
        except ValueError as exc:
            conn.rollback()
            return _er_redirect("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    target = entity or next(iter(entity_resolution.recipes()), "")
    publish_event("ENTITY_DERIVATION_DECIDED", migration_id, "entity", target, {"method": entity_resolution.method(target), "status": st}, user["name"])
    return _er_redirect("notice", f"{target} will be derived: " + entity_resolution.text(target) if st == "APPROVED"
                        else f"{target} will not be derived; it needs a source file.")


@app.post("/api/entity-resolution/preview", response_class=HTMLResponse)
async def preview_entity_resolution(request: Request):
    """Read-only: run the approved and proposed match rules in memory on the canonical rows. Counts and keys only, never values."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    raw, _ = _canonical_rows_for_migration(migration_id, resolve=False)
    conn = get_db_connection()
    try:
        candidates = [r for r in entity_resolution.rules(conn, migration_id) if r["status"] in ("APPROVED", "PROPOSED")]
        conn.commit()
    finally:
        conn.close()
    key_of = {}
    try:
        firm = load_firm_config()
        key_of = {e: next((f.name for f in firm.entities[e].fields if f.is_key), None) for e in firm.entities}
    except FirmConfigError:
        pass
    out = []
    for e in sorted({r["entity_name"] for r in candidates}):
        if not raw.get(e) or not key_of.get(e):
            continue
        per_rule = []
        for r in [x for x in candidates if x["entity_name"] == e]:
            _, _, st = entity_resolution.resolve(raw[e], key_of[e], [r["fields"]])
            per_rule.append({"rule_id": r["rule_id"], "status": r["status"], "merged": st["merged"], "clusters": st["clusters"], "examples": st["examples"][:5]})
        _, _, both = entity_resolution.resolve(raw[e], key_of[e], [x["fields"] for x in candidates if x["entity_name"] == e])
        out.append({"entity": e, "key": key_of[e], "rows": both["rows"], "records": both["records"], "merged": both["merged"], "rules": per_rule})
    return render_template_with_context(request, "entity_resolution.html", _er_context(request, user, preview=out))


def _xodus_code_inputs(migration_id: str) -> Dict[str, Any]:
    """The code maps a stable-pipeline package carries (canonical model v2): approved source values and the target system's values."""
    conn = get_db_connection()
    try:
        model = canonical_model.current()
        system = (target_connector.spec(conn) or {}).get("target_system") or ""
        out = {"coded": canonical_model.coded_fields(model), "source_map": code_mappings.approved_source_map(conn, migration_id),
               "target_map": code_mappings.target_map(conn, system) if system else [], "target_system": system,
               "code_lists": {cs: canonical_model.codes(model, cs) for cs in (model.get("code_sets") or {})}}  # the pipeline's blocking checks use them
        conn.commit()
    finally:
        conn.close()
    return out


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
    scopes_map = {entity: "OUT_OF_SCOPE" for entity in P0_ENTITIES if entity not in configured_scope}
    for key, value in form_data.items():
        if key.startswith("scope_"):
            entity_name = key.replace("scope_", "")
            if entity_name in configured_scope and entity_name in set(P0_ENTITIES):
                scopes_map[entity_name] = str(value)

    state_store.save_entity_scopes(scopes_map)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    publish_event("ENTITY_SCOPES_UPDATED", migration_id, "migration", migration_id, {"scopes": scopes_map}, "migration-analyst")
    return RedirectResponse(url="/entity-sample-files", status_code=status.HTTP_303_SEE_OTHER)


def _blob_container() -> str:
    """The container the user named on the Azure connection page, else the server default (so the emulator works before anything is entered)."""
    setup = state_store.questionnaire_data.get("migration_setup", {})
    return str(setup.get("blob_container") or "").strip() or os.environ.get("AZURE_BLOB_SOURCE_CONTAINER", "")


def _source_firm() -> str:
    return str(state_store.questionnaire_data.get("migration_setup", {}).get("source_firm") or "")


def _blob_panel(with_files: bool = True) -> dict:
    """Status, folder tree and the sample-raw/<firm>/ listing (the sample files stage 01 loads) for pages that show blob storage."""
    container, firm = _blob_container(), _source_firm()
    st = blob_source.status(container)
    prefix = blob_source.sample_prefix(firm)
    st.update({"container": container, "firm_folder": prefix, "blobs": [], "tree": [], "layout_ready": False, "zones": blob_source.ZONES})
    if st["reachable"] and st["container_exists"]:
        try:
            st["tree"] = blob_source.tree(container, folders_only=True)
            have = {r["path"] for r in st["tree"] if r["kind"] == "folder"}
            st["layout_ready"] = all(p in have for p in blob_source.layout_prefixes(firm))
            if with_files:
                recs = [rec for group in getattr(state_store, "entity_source_files", {}).values() for rec in group.values()]
                by_name: Dict[str, set] = {}
                for rec in recs:
                    by_name.setdefault(rec.get("filename"), set()).add(rec.get("content_sha256"))
                blobs = []
                for b in blob_source.list_source_blobs(container, prefix=prefix):
                    fname = b["name"].split("/")[-1]
                    state = "new"
                    if fname in by_name:
                        # Same name is not enough: compare content, so a replaced file shows as changed rather than loaded.
                        digest = hashlib.sha256(blob_source.read_blob(container, b["name"])).hexdigest()
                        state = "loaded" if digest in by_name[fname] else "changed"
                    blobs.append(dict(b, state=state, loaded=state == "loaded"))
                st["blobs"] = blobs
        except Exception as exc:
            st["detail"] = f"Could not read the container: {type(exc).__name__}"
    return st


def _blob_back(key: str, msg: str):
    return RedirectResponse(url="/azure-setup?" + urlencode({key: msg}) + "#storage", status_code=status.HTTP_303_SEE_OTHER)


def _remember_container(user, container: str) -> None:
    """Keep the container name with the migration setup; published like the other setup fields so it survives a restart."""
    setup = state_store.questionnaire_data.setdefault("migration_setup", {})
    setup["blob_container"] = container
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    publish_event("MIGRATION_CONTEXT_UPDATED", migration_id, "migration", migration_id, {
        "migration_id": migration_id, "firm_name": setup.get("migration_name", ""), "migration_name": setup.get("migration_name", ""),
        "source_systems": setup.get("source_details", ""), "target_system": setup.get("target_system", ""), "migration_setup": setup}, user["name"])


@app.post("/api/blob/container")
async def save_blob_container(request: Request, container: str = Form("")):
    """Save the container name only: nothing is created or contacted."""
    user = get_current_user(request)
    container = container.strip().lower()
    if not blob_source.NAME_OK.match(container):
        return _blob_back("error", "Container names use lowercase letters, digits and '-' (3 to 63 characters).")
    _remember_container(user, container)
    state_store.audit_log.append({"event": "BLOB_CONTAINER_SAVED", "actor": user["name"], "timestamp_utc": datetime.now(timezone.utc).isoformat(), "details": {"container": container}})
    return _blob_back("notice", f"Container name '{container}' saved. Test it next, then create the folders.")


@app.post("/api/blob/test")
async def test_blob_storage(request: Request):
    """Read-only: can the server reach the storage account, does the container exist and are the raw/stage/curated folders in place."""
    get_current_user(request)
    st = _blob_panel(with_files=False)
    if not st["configured"] or not st["reachable"]:
        return _blob_back("error", st["detail"] or "The storage account is not reachable.")
    if not st["container_exists"]:
        return _blob_back("error", f"Storage account {st['account']} is reachable, but container '{st['container']}' does not exist. Use 'Create folders' to create it.")
    if not st["layout_ready"]:
        return _blob_back("error", f"Container '{st['container']}' exists but the raw/stage/curated folders are not all in place. Use 'Create folders'.")
    return _blob_back("notice", f"Storage account {st['account']} reachable; container '{st['container']}' and its folders are in place.")


@app.post("/api/blob/layout")
async def create_blob_layout(request: Request, container: str = Form("")):
    """Create the container if needed and raw/, stage/, curated/ and raw/<source firm>/ in it (never deletes). Saves the name too."""
    user = get_current_user(request)
    container = container.strip().lower()
    try:
        res = blob_source.ensure_layout(container, _source_firm())
    except Exception as exc:
        return _blob_back("error", str(exc)[:300])
    _remember_container(user, container)
    state_store.audit_log.append({"event": "BLOB_LAYOUT_CREATED", "actor": user["name"], "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                  "details": {"container": container, "folders_created": res["folders_created"]}})
    made = ", ".join(res["folders_created"]) or "nothing new (already in place)"
    return _blob_back("notice", f"Container '{container}' ready. Folders created: {made}.")


@app.post("/api/blob/load")
async def load_blob_source_files(request: Request):
    """Read the ticked blobs from raw/<source firm>/ and run them through the same intake as an upload."""
    get_current_user(request)
    form = await request.form()
    names = [str(n) for n in form.getlist("blob")]
    if not names:
        return RedirectResponse(url="/entity-sample-files?error=" + quote_plus("Tick at least one blob to load."), status_code=status.HTTP_303_SEE_OTHER)
    container, prefix = _blob_container(), blob_source.sample_prefix(_source_firm())
    files = []
    try:
        for name in names:
            if not name.startswith(prefix):
                raise ValueError("Files are loaded from " + prefix + " only.")
            files.append(UploadFile(file=io.BytesIO(blob_source.read_blob(container, name)), filename=name.split("/")[-1]))
    except Exception as exc:
        return RedirectResponse(url="/entity-sample-files?error=" + quote_plus(f"Could not read from blob storage: {exc}"), status_code=status.HTTP_303_SEE_OTHER)
    return await upload_entity_sample_file(request, files=files, source_role="ENTITY_MASTER", source_context="", data_dictionary_notes="")


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

        if inference.get("status") == "NEEDS_CLARIFICATION" or inference.get("entity") not in set(P0_ENTITIES):
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
    return RedirectResponse(url="/entity-sample-files?notice=" + quote_plus(notice) + "#loaded-files", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/confirm-entity")
async def confirm_entity_scope_bulk(request: Request, entity_name: str = Form(...), comments: str = Form("")):
    """Approve all files for an entity in one action only when every file has clear inference evidence."""
    user = get_current_user(request)
    if entity_name not in set(P0_ENTITIES):
        raise HTTPException(status_code=400, detail="Only Account, Household and Client are supported here.")
    entity_files = state_store.entity_source_files.get(entity_name, {})
    if not entity_files:
        return RedirectResponse(url="/discovery?notice=" + quote_plus(f"No source files are registered for {entity_name}.") + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)
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
        return RedirectResponse(url="/discovery?notice=" + quote_plus(message) + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)
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
    return RedirectResponse(url="/discovery?notice=" + quote_plus(message) + "#discovered-source-inventory", status_code=status.HTTP_303_SEE_OTHER)


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
    return _apply_inference_review(user, file_id, entity_name, decision, comments, target_entity)


@app.post("/api/entities/review-selected")
async def review_selected_source_files(request: Request):
    """Table review of discovered source files: confirm or reject every ticked file, with a note and an optional
    reclassification per row. Each file goes through the same single-file review."""
    user = get_current_user(request)
    form = await request.form()
    ids = [str(i) for i in form.getlist("files")]
    decision = str(form.get("decision", "")).strip().upper()
    if decision not in {"APPROVE", "REJECT"}:
        return RedirectResponse(url="/discovery?error=" + quote_plus("Choose confirm or reject."), status_code=status.HTTP_303_SEE_OTHER)
    if not ids:
        return RedirectResponse(url="/discovery?error=" + quote_plus("Tick at least one file first."), status_code=status.HTTP_303_SEE_OTHER)
    common = str(form.get("common_note", "")).strip()
    done, failed = 0, []
    for fid in ids:
        label, current = fid, ""
        for bucket, files in state_store.entity_source_files.items():
            for item in files.values():
                if item.get("file_id") == fid:
                    label, current = str(item.get("filename") or fid), str(item.get("inferred_entity") or bucket)
        note = str(form.get("note::" + fid, "")).strip() or common
        entity = str(form.get("entity::" + fid, "")).strip() or current
        target = str(form.get("change::" + fid, "")).strip() or entity
        try:
            res = _apply_inference_review(user, fid, entity, decision, note, target)
        except HTTPException as exc:
            failed.append(f"{label}: {exc.detail}")
            continue
        loc = res.headers.get("location", "")
        if "error=" in loc:
            from urllib.parse import parse_qs, urlparse
            failed.append(f"{label}: " + parse_qs(urlparse(loc).query).get("error", ["not changed"])[0])
        else:
            done += 1
    kind, msg = _bulk_summary(done, failed, "confirmed" if decision == "APPROVE" else "rejected")
    return RedirectResponse(url=f"/discovery?{kind}=" + quote_plus(msg), status_code=status.HTTP_303_SEE_OTHER)


def _apply_inference_review(user: Dict[str, Any], file_id: str, entity_name: str, decision: str, comments: str, target_entity: str):
    normalized_decision = decision.strip().upper()
    if normalized_decision not in {"APPROVE", "REJECT"}:
        return RedirectResponse(url="/discovery?error=" + quote_plus("Choose Confirm classification or Reject file.") + "", status_code=status.HTTP_303_SEE_OTHER)
    if normalized_decision == "REJECT" and not comments.strip():
        return RedirectResponse(url="/discovery?error=" + quote_plus("Enter a rejection reason before rejecting this file.") + "", status_code=status.HTTP_303_SEE_OTHER)

    record = None
    current_entity = None
    for bucket_entity, files in state_store.entity_source_files.items():
        for item in files.values():
            if item.get("file_id") == file_id:
                record, current_entity = item, bucket_entity
                break
        if record:
            break
    if record is None and entity_name in set(P0_ENTITIES):
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
                return RedirectResponse(url="/discovery?error=" + quote_plus("Enter a note explaining the relationship disposition (what the link joins and why it is deferred).") + "", status_code=status.HTTP_303_SEE_OTHER)
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
            if selected_entity not in set(P0_ENTITIES):
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
    return RedirectResponse(url=f"/discovery?notice={notice}", status_code=status.HTTP_303_SEE_OTHER)

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


def _template_mapping_seeds(migration_id: str, entity_name: str) -> Dict[str, Any]:
    """Source-to-canonical names approved in the template migration this one was started from (names only, never approvals).
    Returns {"template_id", "template_name", "seeds": {source_field: canonical_field}}; empty seeds when there is no template."""
    template_id = str((state_store.questionnaire_data.get("migration_template") or {}).get("template_migration_id") or "")
    if not template_id or template_id == str(migration_id):
        return {"template_id": "", "template_name": "", "seeds": {}}
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT source_field, canonical_field FROM canonical_mappings WHERE migration_id = ? AND entity_name = ? AND is_confirmed = 1",
                            (template_id, entity_name)).fetchall()
        name = next((m["name"] for m in migration_registry.list_migrations(conn, str(migration_id)) if m["id"] == template_id), template_id)
    finally:
        conn.close()
    return {"template_id": template_id, "template_name": name, "seeds": {r["source_field"]: r["canonical_field"] for r in rows}}


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
    scope_entity = entity_name
    if scope_entity not in set(P0_ENTITIES):
        raise HTTPException(status_code=400, detail=f"{entity_name} is not part of the MCL workflow.")
    if state_store.entity_scopes.get(scope_entity) != "IN_SCOPE":
        raise HTTPException(status_code=409, detail=f"{entity_name} maps to {scope_entity}, which is not explicitly IN_SCOPE.")
    entity_files = state_store.entity_source_files.get(entity_name, {})
    uploaded = next((item for item in entity_files.values() if item.get("file_id") == file_id), None) if file_id else state_store.entity_sample_files.get(entity_name)
    if uploaded and uploaded.get("inference") and uploaded.get("inference_approval_status") != "APPROVED":
        raise HTTPException(status_code=409, detail=f"Entity inference for {entity_name} must be approved before mapping.")
    if not uploaded or uploaded.get("status") != "UPLOADED" or not uploaded.get("stored_path"):
        raise HTTPException(status_code=409, detail=f"No valid uploaded source file is available for {entity_name}.")
    if not file_store.is_file(uploaded["stored_path"]):
        raise HTTPException(status_code=409, detail=f"The uploaded source file for {entity_name} is no longer available; upload it again.")

    run_id = f"RUN-{uuid.uuid4().hex}"
    content = file_store.read_bytes(uploaded["stored_path"])
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
    template_seed = _template_mapping_seeds(str(migration_id), entity_name)
    # Names approved in the template migration seed PROPOSALS only (no transformation, no approval); this migration's own approvals win.
    approved_catalog = {
        **template_seed["seeds"],
        **{source: rule["canonical_field"] for source, rule in approved_rules.items()},
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
    mapping["canonical_entity"] = entity_name
    mapping["run_id"] = run_id
    if template_seed["seeds"]:
        for lineage in mapping.get("field_lineages", []):
            if template_seed["seeds"].get(str(lineage.get("source_field"))) == lineage.get("canonical_field") and str(lineage.get("source_field")) not in approved_rules:
                lineage["basis_note"] = f"Matches a mapping approved in {template_seed['template_name']}. Confirm it fits this firm."
                lineage["from_template"] = template_seed["template_id"]
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
    if entity_name not in set(P0_ENTITIES):
        raise HTTPException(status_code=409, detail="Uploaded-file mapping currently supports MCL: Account, Household, and Client.")
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
        if entity_name not in configured_scope or entity_name not in set(P0_ENTITIES):
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
        destination = "/discovery?notice=" + quote_plus(message) + "#discovered-source-inventory"
    state_store.audit_log.append({"event":"BATCH_SOURCE_MAPPING_PROPOSED","actor":user.get("name","Unknown reviewer"),"timestamp_utc":datetime.now(timezone.utc).isoformat(),"details":{"completed_files":completed,"failures":failures}})
    return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/entities/map-mcl")
async def map_mcl_uploaded_files():
    """Run the MCL source-to-canonical flow across Account, Household, and Client."""
    mcl_run_id = f"MCL-{uuid.uuid4().hex}"
    source_entities = [e for e in P0_ENTITIES if state_store.entity_scopes.get(e) == "IN_SCOPE" and e in state_store.entity_sample_files]
    # Validate all prerequisites before starting any profile/mapping work.
    for entity_name in source_entities:
        scope_entity = entity_name
        if state_store.entity_scopes.get(scope_entity) != "IN_SCOPE":
            raise HTTPException(status_code=409, detail=f"MCL run blocked: {scope_entity} is not explicitly IN_SCOPE.")
        uploaded = state_store.entity_sample_files.get(entity_name)
        if not uploaded or uploaded.get("status") != "UPLOADED" or not uploaded.get("stored_path"):
            raise HTTPException(status_code=409, detail=f"MCL run blocked: upload a valid {entity_name} source file first.")
        if not file_store.is_file(uploaded["stored_path"]):
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
            for entity in P0_ENTITIES
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
    canonical_override: str = Form(""),
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
        canonical_field = row["canonical_field"]
        change_note = ""
        new_canonical = canonical_override.strip() if isinstance(canonical_override, str) else ""
        if normalized == "APPROVED" and new_canonical and new_canonical != canonical_field:
            if new_canonical not in _canonical_field_lists().get(row["entity_name"], []):
                conn.rollback()
                raise HTTPException(status_code=400, detail=f"'{new_canonical}' is not a canonical {row['entity_name']} field.")
            conn.execute("UPDATE mapping_suggestions SET canonical_field = ? WHERE suggestion_id = ? AND status = 'PENDING'",
                         (new_canonical, suggestion_id))
            change_note = f"Canonical field changed from {canonical_field or 'none'} to {new_canonical}."
            canonical_field = new_canonical
        comments = (change_note + " " + comments).strip() if change_note else comments
        proposed_expression = str(row["proposed_rule"] or "DIRECT_COPY").strip().upper()
        override = rule_override.strip().upper() if isinstance(rule_override, str) else ""
        if normalized == "APPROVED" and override:
            if not _typed_rule_ok(override):
                conn.rollback()
                raise HTTPException(status_code=400, detail=_RULE_NOT_REGISTERED.format(rule=override))
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
        if normalized == "APPROVED" and canonical_field:
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
                    canonical_field, proposed_expression,
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
        publish_event("MAPPING_REVIEWED", str(row["migration_id"]), "mapping_suggestion", suggestion_id, {"entity": row["entity_name"], "source_field": row["source_field"], "canonical_field": canonical_field, "decision": normalized}, reviewer_name)
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


@app.post("/api/mappings/reopen")
async def reopen_approved_mapping(request: Request, entity_name: str = Form(...), source_field: str = Form(...), reason: str = Form("")):
    """Reopen one approved source-to-canonical mapping for a new decision. The mapping stops counting as approved, the earlier
    decision stays on record, and a pending proposal (same field and rule) is created so the normal review can change the canonical
    field or rule. Nothing is changed in place: approval is still a separate human decision."""
    user = get_current_user(request)
    reason = reason.strip()
    if len(reason) < MIN_STALE_REASON:
        return RedirectResponse(url="/agent-pipeline?error=" + quote_plus(f"Give a reason of at least {MIN_STALE_REASON} characters to reopen an approved mapping.") + "#approved-mappings",
                                status_code=status.HTTP_303_SEE_OTHER)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM canonical_mappings WHERE migration_id = ? AND entity_name = ? AND source_field = ? AND is_confirmed = 1",
            (migration_id, entity_name, source_field)).fetchone()
        if current is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="No approved mapping for that source field in this migration.")
        if conn.execute("SELECT 1 FROM mapping_suggestions WHERE migration_id = ? AND entity_name = ? AND source_field = ? AND status = 'PENDING'",
                        (migration_id, entity_name, source_field)).fetchone():
            conn.rollback()
            raise HTTPException(status_code=409, detail="A proposal for that source field is already waiting for a decision.")
        earlier = conn.execute(
            "SELECT run_id FROM mapping_suggestions WHERE migration_id = ? AND entity_name = ? AND source_field = ? ORDER BY created_utc DESC LIMIT 1",
            (migration_id, entity_name, source_field)).fetchone()
        run_id = earlier["run_id"] if earlier else f"REVISION-{migration_id}"
        suggestion_id = f"MAPSUG-REV-{uuid.uuid4().hex[:12]}"
        conn.execute("UPDATE canonical_mappings SET is_confirmed = 0, updated_utc = ? WHERE mapping_id = ?", (now, current["mapping_id"]))
        conn.execute(
            """INSERT INTO mapping_suggestions (suggestion_id, run_id, migration_id, entity_name, source_field, canonical_field, proposed_rule,
                   confidence_score, status, reviewer, review_comments, created_utc, reviewed_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, ?, ?, NULL)""",
            (suggestion_id, run_id, migration_id, entity_name, source_field, current["canonical_field"],
             current["transformation_expression"] or "DIRECT_COPY", float(current["confidence_score"] or 0.0),
             f"Reopened by {user['name']}: {reason}", now))
        conn.execute(
            "INSERT INTO approvals (id, run_id, artifact_id, stage, decision, reviewer, comments, created_utc) VALUES (?, ?, ?, 'SOURCE_TO_CANONICAL_MAPPING', 'REOPENED', ?, ?, ?)",
            (f"APR-{uuid.uuid4().hex}", run_id, suggestion_id, user["name"], reason, now))
        conn.commit()
    except HTTPException:
        raise
    finally:
        conn.close()
    _sync_mapping_information_requests(migration_id)
    _target_setup_audit("SOURCE_MAPPING_REOPENED", user, {"entity": entity_name, "source_field": source_field, "was": current["canonical_field"],
                                                          "rule": current["transformation_expression"], "reason": reason[:200]})
    publish_event("MAPPING_REOPENED", migration_id, "mapping_suggestion", suggestion_id,
                  {"entity": entity_name, "source_field": source_field, "canonical_field": current["canonical_field"], "reason": reason[:200]}, user["name"])
    return RedirectResponse(url="/agent-pipeline?notice=" + quote_plus(f"{entity_name}.{source_field} reopened. Change the canonical field or rule in the pending list, then approve it again.") + "#pending-mappings",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/mappings/reopen-rejected")
async def reopen_rejected_mapping(request: Request, suggestion_id: str = Form(...), reason: str = Form("")):
    """Reopen a rejected source-to-canonical proposal for a new decision (for example after the canonical model gained the
    field it needed). The rejection stays on record; a new pending proposal is created so the normal review can change the
    canonical field or rule. Nothing is approved here."""
    user = get_current_user(request)
    reason = reason.strip()
    if len(reason) < MIN_STALE_REASON:
        return RedirectResponse(url="/agent-pipeline?error=" + quote_plus(f"Give a reason of at least {MIN_STALE_REASON} characters to reopen a rejected mapping.") + "#rejected-mappings",
                                status_code=status.HTTP_303_SEE_OTHER)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM mapping_suggestions WHERE suggestion_id = ? AND migration_id = ? AND status = 'REJECTED'",
                           (suggestion_id, migration_id)).fetchone()
        if old is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="No rejected proposal with that id in this migration.")
        entity_name, source_field = old["entity_name"], old["source_field"]
        if conn.execute("SELECT 1 FROM mapping_suggestions WHERE migration_id = ? AND entity_name = ? AND source_field = ? AND status = 'PENDING'",
                        (migration_id, entity_name, source_field)).fetchone():
            conn.rollback()
            raise HTTPException(status_code=409, detail="A proposal for that source field is already waiting for a decision.")
        if conn.execute("SELECT 1 FROM canonical_mappings WHERE migration_id = ? AND entity_name = ? AND source_field = ? AND is_confirmed = 1",
                        (migration_id, entity_name, source_field)).fetchone():
            conn.rollback()
            raise HTTPException(status_code=409, detail="That source field already has an approved mapping; reopen that one instead.")
        new_id = f"MAPSUG-REV-{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO mapping_suggestions (suggestion_id, run_id, migration_id, entity_name, source_field, canonical_field, proposed_rule,
                   confidence_score, status, reviewer, review_comments, created_utc, reviewed_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, ?, ?, NULL)""",
            (new_id, old["run_id"], migration_id, entity_name, source_field, old["canonical_field"], old["proposed_rule"] or "DIRECT_COPY",
             float(old["confidence_score"] or 0.0), f"Reopened rejected proposal by {user['name']}: {reason}", now))
        conn.execute(
            "INSERT INTO approvals (id, run_id, artifact_id, stage, decision, reviewer, comments, created_utc) VALUES (?, ?, ?, 'SOURCE_TO_CANONICAL_MAPPING', 'REOPENED', ?, ?, ?)",
            (f"APR-{uuid.uuid4().hex}", old["run_id"], new_id, user["name"], reason, now))
        conn.commit()
    except HTTPException:
        raise
    finally:
        conn.close()
    _sync_mapping_information_requests(migration_id)
    _target_setup_audit("SOURCE_MAPPING_REJECTION_REOPENED", user, {"entity": entity_name, "source_field": source_field, "reason": reason[:200]})
    publish_event("MAPPING_REOPENED", migration_id, "mapping_suggestion", new_id,
                  {"entity": entity_name, "source_field": source_field, "from": "REJECTED", "reason": reason[:200]}, user["name"])
    return RedirectResponse(url="/agent-pipeline?notice=" + quote_plus(f"{entity_name}.{source_field} reopened. Change the canonical field or rule in the pending list, then approve it.") + "#pending-mappings",
                            status_code=status.HTTP_303_SEE_OTHER)


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
    if entity_name not in set(P0_ENTITIES):
        raise HTTPException(status_code=400, detail="Only Account, Household and Client are supported here.")
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


def _chunk_asset_id(migration_id: str, doc_id: Any) -> str:
    return f"{migration_id}:{doc_id}"


def _index_firm_asset(migration_id: str, doc: Dict[str, Any], *, content: Optional[bytes] = None, text: Optional[str] = None, source: str,
                      source_ref: str, version: str) -> str:
    """Cut the asset into chunks and store them for search. Never raises: an asset that cannot be cut is still registered, with the reason as its note."""
    try:
        parts = knowledge_chunks.chunks_for_file(doc["filename"], content) if content is not None else knowledge_chunks.chunks_for_text(text or "")
    except ValueError as exc:
        return str(exc)
    conn = get_db_connection()
    try:
        knowledge_chunks.index_asset(conn, migration_id=migration_id, scope="firm", asset_id=_chunk_asset_id(migration_id, doc["id"]), asset_name=doc["filename"],
                                     source=source, source_ref=source_ref, version=version, chunks=parts["chunks"], entity_names=list(_canonical_field_lists()))
        conn.commit()
        try:  # the vectors are made now, so the first search needs no wait; if the embedder is unavailable they are made at the first search instead
            knowledge_index.embed_pending(conn, embeddings.get_embedder(), migration_id)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding is not available yet: %s", exc)
    finally:
        conn.close()
    return parts["note"]


def _retire_firm_chunks(migration_id: str, doc_id: Any) -> None:
    conn = get_db_connection()
    try:
        knowledge_chunks.retire_asset(conn, _chunk_asset_id(migration_id, doc_id))
        conn.commit()
    finally:
        conn.close()


def _remove_firm_doc(migration_id: str, doc: Dict[str, Any], by: str) -> None:
    state_store.firm_knowledge_docs[:] = [d for d in state_store.firm_knowledge_docs if str(d.get("id")) != str(doc.get("id"))]
    publish_event("KNOWLEDGE_ASSET_REMOVED", migration_id, "knowledge_asset", str(doc.get("id")),
                  {"filename": doc.get("filename"), "category": doc.get("category"), "removed_by": by}, by)
    _retire_firm_chunks(migration_id, doc.get("id"))


def _register_text_asset(migration_id: str, category: str, title: str, text: str, source: str, extra: Dict[str, Any], replaces: Optional[Dict[str, Any]], by: str) -> Dict[str, Any]:
    """A page read from the web or Confluence becomes a knowledge asset (Markdown text). A page already imported is replaced, never duplicated."""
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", title).strip()[:80] or source + "-page"
    doc = state_store.add_firm_file(f"{safe}.md", category, text.encode("utf-8"))
    doc.update(extra)
    doc["indexing_note"] = _index_firm_asset(migration_id, doc, text=text, source=source, source_ref=str(extra.get("source_url", "")), version=str(extra.get("source_version", "")))
    if replaces:
        _remove_firm_doc(migration_id, replaces, by)
    publish_event("KNOWLEDGE_ASSET_REGISTERED", migration_id, "knowledge_asset", str(doc["id"]), {**doc, "source": source}, "knowledge-curator")
    return doc


def _knowledge_back(key: str, message: str, destination: str = "/firm-knowledge"):
    destination = destination if destination in {"/migration-setup", "/firm-knowledge"} else "/firm-knowledge"
    return RedirectResponse(url=f"{destination}?{key}=" + quote_plus(message), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/knowledge/file-upload")
async def upload_firm_file(
    category: str = Form(...),
    file: UploadFile = File(...),
    return_to: str = Form("/firm-knowledge"),
):
    filename = file.filename or ""
    allowed_extensions = {".md", ".txt", ".csv", ".tsv", ".json", ".xlsx", ".xlsm", ".xls", ".docx", ".pptx", ".pdf"}
    if Path(filename).suffix.lower() not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Knowledge assets must be Markdown, text, CSV/TSV, JSON, Excel, Word, PowerPoint, or PDF files.")
    content = await file.read()
    try:
        doc_record = state_store.add_firm_file(filename, category, content)
        migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
        doc_record["indexing_note"] = _index_firm_asset(migration_id, doc_record, content=content, source="file", source_ref=filename, version=knowledge_chunks.version_of(content))
        publish_event("KNOWLEDGE_ASSET_REGISTERED", migration_id, "knowledge_asset", str(doc_record["id"]),
                      doc_record, "knowledge-curator")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    destination = return_to if return_to in {"/migration-setup", "/firm-knowledge"} else "/firm-knowledge"
    return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/knowledge/remove")
async def remove_firm_knowledge_asset(request: Request):
    """Take a firm (migration) knowledge asset out of this migration. Platform knowledge is not touched here. Rules already
    inferred from the asset stay in the rule register (they are platform-wide and are retired on the Business rules page)."""
    user = get_current_user(request)
    form = await request.form()
    doc_id = str(form.get("doc_id", "")).strip()
    doc = next((d for d in state_store.firm_knowledge_docs if str(d.get("id")) == doc_id), None)
    if not doc:
        return RedirectResponse(url="/firm-knowledge", status_code=status.HTTP_303_SEE_OTHER)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    _remove_firm_doc(migration_id, doc, user["name"])
    return RedirectResponse(url="/firm-knowledge", status_code=status.HTTP_303_SEE_OTHER)


def _import_confluence(migration_id: str, category: str, pages: List[Dict[str, Any]], by: str) -> Dict[str, int]:
    """Register Confluence pages: a page already imported is skipped when its Confluence version is unchanged, replaced when it changed."""
    counts = {"added": 0, "updated": 0, "unchanged": 0}
    for page in pages:
        old = next((d for d in state_store.firm_knowledge_docs if d.get("source") == "confluence" and d.get("source_page_id") == page["page_id"]), None)
        if old and str(old.get("source_version")) == page["version"]:
            counts["unchanged"] += 1
            continue
        _register_text_asset(migration_id, category, page["title"], page["text"], "confluence",
                             {"source": "confluence", "source_url": page["url"], "source_page_id": page["page_id"], "source_version": page["version"], "source_space": page["space"]}, old, by)
        counts["updated" if old else "added"] += 1
    return counts


@app.post("/api/knowledge/confluence")
async def import_confluence_page(
    request: Request,
    category: str = Form(...),
    link: str = Form(...),
    mode: str = Form("page"),
    return_to: str = Form("/firm-knowledge"),
):
    """Import from Confluence: one page, a page with its children, or a whole space. Uses the connection a Platform Admin set up; a
    server with only the older CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN settings can still import single Cloud pages."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        connected = knowledge_sources.is_connected(conn)
        pages: List[Dict[str, Any]] = []
        truncated = False
        try:
            if connected:
                found = knowledge_sources.fetch_pages(conn, link, mode)
                pages, truncated = found["pages"], found["truncated"]
            else:
                if mode != "page":
                    raise ValueError("Confluence is not connected. Ask a Platform Admin to connect it (Platform knowledge, Knowledge sources).")
                try:
                    page = confluence_source.fetch_page(link)
                except confluence_source.ConfluenceError as exc:
                    raise ValueError(str(exc) + " A Platform Admin can connect Confluence under Platform knowledge, Knowledge sources.") from exc
                pages = [{"page_id": page.get("page_id", ""), "title": page["title"], "text": page["text"], "url": page["url"], "space": "", "version": page.get("version", "")}]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.commit()
    finally:
        conn.close()
    counts = _import_confluence(migration_id, category, pages, user["name"])
    _target_setup_audit("KNOWLEDGE_CONFLUENCE_IMPORTED", user, {"mode": mode, "pages": len(pages), **counts})
    return _knowledge_back("notice", f"Confluence: {counts['added']} added, {counts['updated']} updated, {counts['unchanged']} unchanged." + (" Stopped at the page cap." if truncated else ""), return_to)


@app.post("/api/knowledge/web-link")
async def import_web_link(request: Request, category: str = Form(...), url: str = Form(...), return_to: str = Form("/firm-knowledge")):
    """Read an approved https page (a Platform Admin approves hosts) and register it as a knowledge asset."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            page = knowledge_sources.fetch_web(conn, url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn.commit()
    finally:
        conn.close()
    old = next((d for d in state_store.firm_knowledge_docs if d.get("source") == "web" and d.get("source_url") == page["url"]), None)
    if old and old.get("source_version") == page["version"]:
        return _knowledge_back("notice", "That page is already imported and has not changed.", return_to)
    _register_text_asset(migration_id, category, page["title"], page["text"], "web", {"source": "web", "source_url": page["url"], "source_version": page["version"]}, old, user["name"])
    _target_setup_audit("KNOWLEDGE_WEB_IMPORTED", user, {"url": page["url"]})
    return _knowledge_back("notice", f"{page['title']} imported" + (" (replacing the earlier copy)." if old else "."), return_to)


def _suggestion_query(m: Dict[str, Any]) -> str:
    # Only the distinguishing words: filler such as "field" or "canonical" appears in every document and would match everything.
    return " ".join(str(x) for x in (m.get("entity_name"), m.get("source_field"), m.get("canonical_field") or "") if x)


def _evidence(items: List[Dict[str, Any]], limit: int = 40) -> Dict[str, Dict[str, Any]]:
    """The two best-matching knowledge chunks for each item {key, subject, query, entity (optional)}, keyed by the item's key. Advisory: a failure shows
    no evidence, never a broken page."""
    if not items:
        return {}
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    out: Dict[str, Dict[str, Any]] = {}
    conn = get_db_connection()
    try:
        embedder = embeddings.get_embedder()
        by_entity: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for it in items[:limit]:
            by_entity.setdefault(it.get("entity"), []).append(it)
        for entity, rows in by_entity.items():
            for it, hits in zip(rows, knowledge_index.retrieve(conn, migration_id, [r["query"] for r in rows], embedder, k=2, entity=entity, min_score=embeddings.evidence_min_score(embedder))):
                if hits:
                    out[it["key"]] = {"query": it["query"], "subject": it["subject"], "hits": hits}
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Evidence retrieval failed: %s", exc)
        return {}
    finally:
        conn.close()
    return out


def _likelihood_for(mapping_suggestions: List[Dict[str, Any]]) -> Dict[str, float]:
    """What this migration's past mapping decisions suggest about each pending suggestion (only when a decision model has earned its place). Advisory."""
    if not mapping_suggestions:
        return {}
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        out = decision_model.likelihood(conn, migration_id, embeddings.get_embedder(), mapping_suggestions)
        conn.commit()
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Decision model unavailable: %s", exc)
        return {}
    finally:
        conn.close()


def _evidence_for(mapping_suggestions: List[Dict[str, Any]], limit: int = 40) -> Dict[str, Dict[str, Any]]:
    """Evidence for each pending mapping suggestion, keyed 'Entity|SOURCE_FIELD'."""
    return _evidence([{"key": f"{m['entity_name']}|{str(m['source_field']).upper()}", "subject": f"{m['entity_name']}.{m['source_field']}", "query": _suggestion_query(m),
                       "entity": m["entity_name"]} for m in mapping_suggestions or []], limit)


def _evidence_for_rule_suggestions(suggestions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Evidence for each suggested transformation rule, keyed 'Entity|source_field'. The query is the field's name and the kind of rule, never sample values."""
    return _evidence([{"key": f"{s['entity']}|{s['source_field']}", "subject": f"{s['entity']}.{s['source_field']} rule",
                       "query": f"{s['entity']} {s['source_field']}", "entity": s["entity"]}
                      for s in suggestions or []])


def _evidence_for_files(source_uploads: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Evidence for each loaded file's entity classification, keyed by file id. The query is the file name and its column names, never rows."""
    items = []
    for f in source_uploads or []:
        cols = [str(c) for c in (f.get("columns") or f.get("column_names") or [])][:25]
        items.append({"key": str(f.get("file_id")), "subject": str(f.get("filename")), "query": f"{f.get('filename')} {' '.join(cols)}", "entity": None})
    return _evidence(items)


def _feedback_back(key: str, message: str, return_to: str = "/firm-knowledge", anchor: str = "", q: str = ""):
    page = return_to if return_to in {"/discovery", "/firm-knowledge", "/transformation-rules"} else "/firm-knowledge"
    keep = ("&q=" + quote_plus(q[:knowledge_index.MAX_QUERY])) if q and page == "/firm-knowledge" else ""
    return RedirectResponse(url=f"{page}?{key}=" + quote_plus(message) + keep + (anchor or ("#retrieval" if keep else "")), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/knowledge/feedback")
async def knowledge_feedback(request: Request, subject: str = Form(...), query: str = Form(...), chunk_id: str = Form(...), verdict: str = Form(...),
                             return_to: str = Form("/firm-knowledge"), return_q: str = Form("")):
    """A reviewer says whether a piece of retrieved evidence helped. It is training data for this migration's retrieval and names the chunk only."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            knowledge_index.record_feedback(conn, migration_id, subject, query, chunk_id, 1 if verdict == "helpful" else -1, user["name"])
        except ValueError as exc:
            return _feedback_back("error", str(exc), return_to, q=return_q)
        conn.commit()
    finally:
        conn.close()
    return _feedback_back("notice", "Thanks: recorded as " + ("helpful." if verdict == "helpful" else "not helpful."), return_to, q=return_q)


@app.post("/api/knowledge/retrieval/tune")
async def tune_retrieval(request: Request):
    """Train a candidate from this migration's feedback; it is used only if it beats untuned retrieval on feedback held back from training."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            res = retrieval_tuning.tune(conn, migration_id, user["name"], embeddings.get_embedder())
        except ValueError as exc:
            return _feedback_back("error", str(exc), "/firm-knowledge", "#retrieval")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("RETRIEVAL_TUNED", user, {"model_id": res["model_id"], "status": res["status"], "base_hit": res["base_hit"], "tuned_hit": res["tuned_hit"]})
    verdict = "now used" if res["status"] == "ACTIVE" else "not used because it did not beat untuned retrieval on the held-out feedback"
    return _feedback_back("notice", f"{res['model_id']}: finds the helpful evidence {round(res['tuned_hit'] * 100)}% of the time on {res['n_eval']} held-out queries "
                                    f"(untuned {round(res['base_hit'] * 100)}%): {verdict}.", "/firm-knowledge", "#retrieval")


@app.post("/api/knowledge/retrieval/retire")
async def retire_tuned_retrieval(request: Request, model_id: str = Form(...)):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            retrieval_tuning.retire(conn, migration_id, model_id)
        except ValueError as exc:
            return _feedback_back("error", str(exc), "/firm-knowledge", "#retrieval")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("RETRIEVAL_TUNING_RETIRED", user, {"model_id": model_id})
    return _feedback_back("notice", "Back to untuned retrieval.", "/firm-knowledge", "#retrieval")


@app.post("/api/knowledge/decision-model/train")
async def train_decision_model(request: Request):
    """Learn from this migration's approved and rejected mapping suggestions; the result only adds an advisory line and is used only if it beats the agent's own confidence."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            res = decision_model.train(conn, migration_id, user["name"], embeddings.get_embedder())
        except ValueError as exc:
            return _feedback_back("error", str(exc), "/firm-knowledge", "#decision-model")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("DECISION_MODEL_TRAINED", user, {"model_id": res["model_id"], "status": res["status"], "base_acc": res["base_acc"], "tuned_acc": res["tuned_acc"]})
    verdict = "now shown beside pending suggestions" if res["status"] == "ACTIVE" else "not used because it did not clearly beat the agent's own confidence on the held-out decisions"
    return _feedback_back("notice", f"{res['model_id']}: right on {round(res['tuned_acc'] * 100)}% of {res['n_eval']} held-out decisions (the agent's confidence alone: "
                                    f"{round(res['base_acc'] * 100)}%): {verdict}.", "/firm-knowledge", "#decision-model")


@app.post("/api/knowledge/decision-model/retire")
async def retire_decision_model(request: Request, model_id: str = Form(...)):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            decision_model.retire(conn, migration_id, model_id)
        except ValueError as exc:
            return _feedback_back("error", str(exc), "/firm-knowledge", "#decision-model")
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("DECISION_MODEL_RETIRED", user, {"model_id": model_id})
    return _feedback_back("notice", "The decision model is no longer shown.", "/firm-knowledge", "#decision-model")


@app.post("/api/knowledge/index-existing")
async def index_existing_documents(request: Request):
    """Make the documents registered before search existed searchable, from the text kept with each (a spreadsheet or CSV keeps only its column names,
    and a file's text is cut at the storage limit, so those cannot be indexed here: upload them again)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        have = set(knowledge_chunks.stats(conn, migration_id))
        conn.commit()
    finally:
        conn.close()
    done, skipped = 0, []
    for doc in list(state_store.firm_knowledge_docs):
        if _chunk_asset_id(migration_id, doc.get("id")) in have:
            continue
        text = str(doc.get("full_text") or "")
        if not text.strip():
            skipped.append(str(doc.get("filename")))
            continue
        note = _index_firm_asset(migration_id, doc, text=text, source=str(doc.get("source") or "file"), source_ref=str(doc.get("source_url") or doc.get("filename")),
                                 version=knowledge_chunks.version_of(text.encode("utf-8")))
        if note:
            skipped.append(f"{doc.get('filename')} ({note})")
        else:
            done += 1
    _target_setup_audit("KNOWLEDGE_EXISTING_INDEXED", user, {"indexed": done, "skipped": len(skipped)})
    msg = f"Indexed {done} document(s)." + (f" Not indexed: {', '.join(skipped)}; upload those again to make them searchable." if skipped else "")
    return _feedback_back("notice", msg, "/firm-knowledge", "#retrieval")


@app.post("/api/knowledge/purge")
async def purge_search_data(request: Request):
    """Delete this migration's search data: chunks, vectors, feedback and tuned retrieval. The documents stay registered but are no longer searchable."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        gone = knowledge_index.purge_migration(conn, migration_id)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("KNOWLEDGE_SEARCH_DATA_DELETED", user, {"migration_id": migration_id, **gone})
    return _feedback_back("notice", f"Deleted {gone['chunks']} chunk(s), {gone['feedback']} feedback mark(s) and {gone['models']} tuned retrieval version(s).", "/firm-knowledge", "#retrieval")


@app.post("/api/knowledge/resync")
async def resync_knowledge_asset(request: Request, doc_id: str = Form(...)):
    """Read an imported web or Confluence page again: nothing changes unless the page did."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    doc = next((d for d in state_store.firm_knowledge_docs if str(d.get("id")) == doc_id), None)
    if not doc or doc.get("source") not in ("confluence", "web") or not doc.get("source_url"):
        return _knowledge_back("error", "Only an imported web or Confluence page can be read again.")
    conn = get_db_connection()
    try:
        try:
            if doc["source"] == "confluence":
                pages = knowledge_sources.fetch_pages(conn, doc["source_url"], "page")["pages"]
                page = {"title": pages[0]["title"], "text": pages[0]["text"], "url": pages[0]["url"], "version": pages[0]["version"],
                        "extra": {"source": "confluence", "source_page_id": pages[0]["page_id"], "source_space": pages[0]["space"]}}
            else:
                web = knowledge_sources.fetch_web(conn, doc["source_url"])
                page = {**web, "extra": {"source": "web"}}
        except ValueError as exc:
            return _knowledge_back("error", str(exc))
        conn.commit()
    finally:
        conn.close()
    if str(doc.get("source_version")) == page["version"]:
        return _knowledge_back("notice", f"{doc['filename']} has not changed.")
    _register_text_asset(migration_id, doc.get("category", "data_dictionary"), page["title"], page["text"], doc["source"],
                         {**page["extra"], "source_url": page["url"], "source_version": page["version"]}, doc, user["name"])
    return _knowledge_back("notice", f"{page['title']} changed and was imported again.")


@app.post("/api/pipeline/run")
async def execute_agent_pipeline(request: Request, rep_code: str = Form("REP-9981")):
    """Retain a clear compatibility response for the retired legacy cohort pipeline."""
    get_current_user(request)
    raise HTTPException(
        status_code=409,
        detail=(
            "The legacy cohort pipeline is retired. Use Source Discovery, "
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
    raise HTTPException(status_code=409, detail="Cohort/cutover approval is not available. Review source-to-canonical mappings in Mapping Studio.")


@app.post("/api/hitl/reject")
async def reject_cohort(request: Request):
    """Cohort rejection is explicitly deferred from the P0 mapping pilot."""
    get_current_user(request)
    raise HTTPException(status_code=409, detail="Cohort/cutover review is not available. Reject mapping proposals in Mapping Studio with a reason.")


# --- CLIENT ENDPOINTS ---

# --- XODUS data plane: stable pipelines, pull requests, setup, deployment check, runs, promotion gate ---

def _entity_depends() -> Dict[str, set]:
    """{entity: the entities whose records must load first}: every entity a field of it points at (from the firm configuration)."""
    try:
        return {e: {o for o, _k in fields.values()} for e, fields in reference_fields().items()}
    except FirmConfigError:
        return {}


def _with_downstream(selected: List[str], depends: Dict[str, Any]) -> List[str]:
    """The selected entities plus every entity that depends on one of them, directly or through others (their canonical rows point at the
    selected entities' surviving keys, so they must be rebuilt with them)."""
    chosen = set(selected)
    changed = True
    while changed:
        changed = False
        for entity, parents in depends.items():
            if entity not in chosen and chosen & set(parents or ()):
                chosen.add(entity)
                changed = True
    return sorted(chosen)


def live_validation_status(conn, migration_id: str) -> Optional[Dict[str, Any]]:
    """The latest live validation of the current batch (the batch of the latest PL_XODUS_Run), or None."""
    run = adf_runs.latest(conn, migration_id, "PL_XODUS_Run")
    return live_validation.latest(conn, migration_id, run["batch_id"]) if run and run.get("batch_id") else None


def _load_order(entities: List[str]) -> List[str]:
    order, left = [], sorted(entities)
    while left:
        ready = [e for e in left if not (_entity_depends().get(e, set()) & set(left))] or left[:1]
        order += ready
        left = [e for e in left if e not in ready]
    return order


def _xodus_package_inputs(migration_id: str):
    """What xodus_pipelines.build needs, from the approved state of this migration (approved files, mappings, rules, derivations)."""
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"]
        live = live_target.get_config(conn)
        conn.commit()
    finally:
        conn.close()
    try:
        firm = load_firm_config()
    except FirmConfigError as exc:
        raise ValueError(f"Firm configuration is required: {exc}")
    entities: Dict[str, Any] = {}
    files: Dict[str, List[str]] = {}
    for entity, group in state_store.entity_source_files.items():
        if entity not in P0_ENTITIES or entity not in firm.entities:
            continue
        recs = [r for r in group.values() if r.get("inference_approval_status") == "APPROVED"]
        mappings = {src: {"canonical_field": r["canonical_field"], "expression": r["transformation_expression"]}
                    for src, r in _get_approved_mapping_rules(migration_id, entity).items()}
        if not recs or not mappings:
            continue
        derivs = [{"canonical_field": d["canonical_field"], "input_field": d["input_field"], "rule_expression": d["rule_expression"]}
                  for d in _derivations_for(migration_id, entity)]
        fields = firm.canonical_field_names(entity) + canonical_overlay.active_added_fields(entity, migration_id) + [d["canonical_field"] for d in derivs]
        for r in recs:
            files.setdefault(str(r.get("filename")), list(r.get("columns") or []))
        entities[entity] = {"files": sorted({str(r.get("filename")) for r in recs}), "mappings": mappings, "derivations": derivs,
                            "key": next((f.name for f in firm.entities[entity].fields if f.is_key), None), "canonical_fields": list(dict.fromkeys(fields))}
    conn = get_db_connection()
    try:
        er_rules = entity_resolution.approved_rules(conn, migration_id)
        derivs = {e: entity_resolution.derivation(conn, migration_id, e) for e in entity_resolution.recipes()}
        conn.commit()
    finally:
        conn.close()
    for name, ent in entities.items():
        if er_rules.get(name):
            ent["resolution"] = er_rules[name]
    for derived_entity in entity_resolution.recipes():
        r = entity_resolution.recipe_for(derived_entity)
        if ((derivs.get(derived_entity) or {}).get("status") == "APPROVED" and derived_entity in _in_scope_entities() and derived_entity not in entities
                and {r["parent"]["entity"], r["holder"]["entity"]} <= set(entities) and derived_entity in firm.entities):
            entities[derived_entity] = {"files": [], "mappings": {}, "derivations": [], "key": None, "derived": r["method"],
                                        "canonical_fields": firm.canonical_field_names(derived_entity)}
    exprs = ({m["transformation_expression"] for m in approved} | {m["expression"] for e in entities.values() for m in e["mappings"].values()}
             | {d["rule_expression"] for e in entities.values() for d in e["derivations"]})
    rules = {}
    for x in exprs:
        x = str(x or "").strip().upper()
        if transformation_rules.parse_expression(x):
            r = transformation_rules.approved_rule_for_expression(x)
            if r:
                rules[x] = r
    for r in rules.values():  # files a LOOKUP rule reads are loaded too (advisors), even when they are not an entity of their own
        parent = (r.get("params") or {}).get("parent_file") if r.get("rule_type") == "LOOKUP" else None
        if parent and parent not in files:
            rec = next((x for g in state_store.entity_source_files.values() for x in g.values() if str(x.get("filename")) == parent), None)
            if rec:
                files[parent] = list(rec.get("columns") or [])
    raw_files = [{"file_name": n, "columns": c} for n, c in sorted(files.items())]
    env = {"container": _blob_container(), "rawFolder": blob_source.source_prefix(_source_firm()), "liveSchema": (live or {}).get("schema_name") or "simplicity"}
    return spec, approved, rules, entities, raw_files, _load_order(list(entities)), env


def _xodus_package(conn, migration_id: str):
    """(artifact, {"manifest", "files"}) for the latest stored package when it is a stable-pipeline package; (artifact or None, None) otherwise."""
    art = artifact_store.latest(conn, migration_id, "ADF_PACKAGE")
    if not art:
        return None, None
    try:
        rec = artifact_store.read(conn, art["artifact_id"])
        with zipfile.ZipFile(io.BytesIO(rec["data"])) as z:
            files = {n: z.read(n).decode("utf-8") for n in z.namelist() if not n.endswith("/")}
        manifest = json.loads(files.get("manifest.json") or "{}")
    except (KeyError, ValueError, zipfile.BadZipFile):
        return art, None
    return (art, {"manifest": manifest, "files": files}) if manifest.get("carrier") == "XODUS-SQL-ADF" else (art, None)


_FINISHED_RUN = ("SUCCEEDED", "FAILED", "CANCELLED")


def _run_in_progress(conn, migration_id: str, pipeline: str) -> Optional[Dict[str, Any]]:
    """The run of this pipeline that is still going, by Data Factory's own answer (a stored status can be old): None when it has finished.
    A pipeline that is running is never started again."""
    req = adf_runs.latest(conn, migration_id, pipeline)
    if not req or req["status"] in _FINISHED_RUN:
        return None
    try:
        req = adf_runs.refresh(conn, req)
    except ValueError:
        return req  # Data Factory cannot be asked: treat the run as still going rather than start a second one
    return None if req["status"] in _FINISHED_RUN else req


def _already_running_message(req: Dict[str, Any]) -> str:
    return f"{req['pipeline']} is already running (run {req['adf_run_id']}, {str(req['status']).lower()}). Wait for it to finish, then refresh its status."


def _merged_pr(conn, migration_id: str, version: str) -> Optional[Dict[str, Any]]:
    return next((p for p in git_pull_requests.list_for(conn, migration_id) if p["package_version"] == version and p["state"] == "MERGED"), None)


def _data_plane_state(migration_id: str, refresh_runs: bool = False) -> Dict[str, Any]:
    """Everything stages 08-10 show about the stable-pipeline package: pull requests, setup run, deployment check, runs, gate."""
    conn = get_db_connection()
    try:
        art, pkg = _xodus_package(conn, migration_id)
        out: Dict[str, Any] = {"pkg": pkg, "artifact": art}
        if pkg:
            man = pkg["manifest"]
            out["manifest"] = man
            out["prs"] = git_pull_requests.list_for(conn, migration_id)
            out["merged_pr"] = _merged_pr(conn, migration_id, man["version"])
            out["open_pr"] = next((p for p in out["prs"] if p["package_version"] == man["version"] and p["state"] == "OPEN"), None)
            out["package_stale"] = _code_map_stale(conn, migration_id, man)  # approvals changed since this package was built
            out["setup_run"] = adf_runs.latest(conn, migration_id, man["setupPipeline"])
            out["run"] = adf_runs.latest(conn, migration_id, "PL_XODUS_Run")
            out["promote_run"] = adf_runs.latest(conn, migration_id, "PL_XODUS_Promote")
            out["running"] = {k: bool(out[r] and out[r]["status"] not in _FINISHED_RUN) for k, r in (("setup", "setup_run"), ("run", "run"), ("promote", "promote_run"))}
            if refresh_runs:
                for k in ("setup_run", "run", "promote_run"):
                    if out[k] and out[k]["status"] not in ("SUCCEEDED", "FAILED", "CANCELLED"):
                        try:
                            out[k] = adf_runs.refresh(conn, out[k])
                        except ValueError as exc:
                            out[k] = {**out[k], "message": str(exc)}
            dc = deployment_check.latest(conn, migration_id)
            out["deployment"] = dc if dc and dc.get("version") == man["version"] else None
            out["stale_deployment"] = dc if dc and dc.get("version") != man["version"] else None
            out["adf_ready"] = (azure_connection.get_config(conn) or {}).get("status") == "CONNECTED"
        conn.commit()
    finally:
        conn.close()
    return out


def _dp_redirect(page: str, key: str, message: str):
    anchor = {"/publish-artifacts": "#data-plane", "/execution": "#start-run", "/validation-rules": "#promotion"}.get(page, "")
    return RedirectResponse(url=f"{page}?{key}=" + quote_plus(message) + anchor, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/live-target")
async def save_live_target(request: Request):
    user = get_current_user(request)
    form = await request.form()
    conn = get_db_connection()
    try:
        try:
            live_target.save(conn, user["name"], {k: str(form.get(k, "")) for k in live_target.FIELDS})
        except ValueError as exc:
            return RedirectResponse(url="/azure-setup?" + urlencode({"error": str(exc)}) + "#live", status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("LIVE_DATABASE_SAVED", user, {"database": str(form.get("database_name", ""))})
    return RedirectResponse(url="/azure-setup?" + urlencode({"notice": "Live database settings saved. Test the connection next."}) + "#live", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/live-target/test")
async def test_live_target(request: Request):
    get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            cfg = live_target.test_connection(conn)
        except ValueError as exc:
            return RedirectResponse(url="/azure-setup?" + urlencode({"error": str(exc)}) + "#live", status_code=status.HTTP_303_SEE_OTHER)
        conn.commit()
    finally:
        conn.close()
    key = "notice" if cfg["status"] == "CONNECTED" else "error"
    return RedirectResponse(url="/azure-setup?" + urlencode({key: cfg["check_detail"]}) + "#live", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/git/pull-request")
async def open_package_pull_request(request: Request):
    """Stage 08: push the stable-pipeline package to its own branch and open a pull request. Nothing is merged here."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            art, pkg = _xodus_package(conn, migration_id)
            if not pkg:
                raise ValueError("Generate the pipeline package first (07 Transformation SQL) with the Azure environment set up.")
            man = pkg["manifest"]
            body = (f"Generated by XODUS from approved mappings only, for {migration_id}.\n\n- Package {art['artifact_id']} v{art['artifact_version']}, version {man['version']}\n"
                    f"- Setup pipeline: {man['setupPipeline']} (run it after merging)\n- Entities in load order: {', '.join(man['loadOrder'])}\n"
                    f"- Live tables: {', '.join(t['live_table'] for t in man['liveTables'])}\n\nOpened by {user['name']} in XODUS; approve and merge it from the Publish page.")
            pr = git_pull_requests.open_pull_request(conn, user["name"], migration_id, pkg["files"], man["version"], art["artifact_id"], body)
        except ValueError as exc:
            return _dp_redirect("/publish-artifacts", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("PULL_REQUEST_OPENED", user, {"number": pr["number"], "branch": pr["branch"], "version": pr["package_version"]})
    return _dp_redirect("/publish-artifacts", "notice", f"Pull request #{pr['number']} opened ({pr['files']} files on {pr['branch']}"
                        + (f"; it also removes {len(pr['removed'])} file(s) of the earlier XODUS package" if pr.get("removed") else "") + "). Approve and merge it below.")


@app.post("/api/git/pull-request/{number}/refresh")
async def refresh_package_pull_request(request: Request, number: int):
    get_current_user(request)
    conn = get_db_connection()
    try:
        try:
            pr = git_pull_requests.refresh(conn, number)
        except ValueError as exc:
            return _dp_redirect("/publish-artifacts", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    return _dp_redirect("/publish-artifacts", "notice", f"Pull request #{number} is {pr['state'].lower()}.")


@app.post("/api/git/pull-request/{number}/merge")
async def merge_package_pull_request(request: Request, number: int):
    """Option b: the signed-in Migration Analyst approves; XODUS records them and merges the pull request on their behalf."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        # Only the pull request of the current, up-to-date package may be merged: an older one, or one whose package is stale, would
        # deploy SQL that no longer matches the approvals.
        _, pkg = _xodus_package(conn, migration_id)
        target = next((p for p in git_pull_requests.list_for(conn, migration_id) if p["number"] == number), None)
        if pkg and target and target["package_version"] != pkg["manifest"]["version"]:
            return _dp_redirect("/publish-artifacts", "error", f"Pull request #{number} is for an older package; it is superseded by {pkg['manifest']['version']}.")
        if pkg and target and _code_map_stale(conn, migration_id, pkg["manifest"]):
            return _dp_redirect("/publish-artifacts", "error", "The package is out of date. Generate a new package (stage 07) and merge its pull request instead.")
        try:
            pr = git_pull_requests.merge(conn, number, user["name"])
        except ValueError as exc:
            return _dp_redirect("/publish-artifacts", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("PULL_REQUEST_APPROVED_AND_MERGED", user, {"number": number, "merge_sha": pr.get("merge_sha")})
    return _dp_redirect("/publish-artifacts", "notice", f"Pull request #{number} approved by {user['name']} and merged. Publish it in Data Factory, then run the setup pipeline.")


@app.post("/api/data-plane/setup")
async def run_setup_pipeline(request: Request):
    """Starts PL_XODUS_Setup_<migration> through the service principal: deploys this package's SQL objects. Needs the merged pull request."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            art, pkg = _xodus_package(conn, migration_id)
            if not pkg:
                raise ValueError("Generate the pipeline package first (07 Transformation SQL).")
            man = pkg["manifest"]
            if not _merged_pr(conn, migration_id, man["version"]):
                raise ValueError("Merge the pull request for this package version first.")
            busy = _run_in_progress(conn, migration_id, man["setupPipeline"])
            if busy:
                raise ValueError(_already_running_message(busy))
            req = adf_runs.start(conn, migration_id, man["setupPipeline"], {}, user["name"])
        except ValueError as exc:
            return _dp_redirect("/publish-artifacts", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("SETUP_PIPELINE_STARTED", user, {"pipeline": req["pipeline"], "adf_run_id": req["adf_run_id"]})
    return _dp_redirect("/publish-artifacts", "notice", f"{req['pipeline']} started (run {req['adf_run_id']}). Refresh its status, then run the deployment check.")


@app.post("/api/data-plane/runs/{request_id}/refresh")
async def refresh_data_plane_run(request: Request, request_id: str, return_to: str = Form("/publish-artifacts")):
    get_current_user(request)
    page = return_to if return_to in ("/publish-artifacts", "/execution", "/validation-rules") else "/publish-artifacts"
    conn = get_db_connection()
    try:
        adf_runs.ensure_table(conn)
        row = conn.execute("SELECT * FROM adf_run_requests WHERE request_id = ?", (request_id,)).fetchone()
        if not row:
            return _dp_redirect(page, "error", "No such run request.")
        try:
            run = adf_runs.refresh(conn, dict(row))
        except ValueError as exc:
            return _dp_redirect(page, "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    detail = "" if not run["failures"] else " Failed at " + "; ".join(f"{f['activity']}: {f['error'][:200]}" for f in run["failures"][:2])
    return _dp_redirect(page, "error" if run["status"] in ("FAILED", "CANCELLED") else "notice", f"{run['pipeline']} run {run['adf_run_id']}: {run['status'].lower()}.{detail}")


@app.post("/api/data-plane/deployment-check")
async def run_deployment_check(request: Request):
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        try:
            art, pkg = _xodus_package(conn, migration_id)
            if not pkg:
                raise ValueError("Generate the pipeline package first (07 Transformation SQL).")
            res = deployment_check.run(conn, migration_id, pkg["manifest"], user["name"])
        except ValueError as exc:
            return _dp_redirect("/publish-artifacts", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    fails = [c for c in res["checks"] if c["status"] == "FAIL"]
    _target_setup_audit("DEPLOYMENT_CHECKED", user, {"version": res["version"], "status": res["status"], "failures": len(fails)})
    return _dp_redirect("/publish-artifacts", "notice" if res["status"] == "PASS" else "error",
                        f"Deployment check {res['status']}." + ("" if not fails else " " + fails[0]["detail"]))


@app.post("/api/data-plane/start")
async def start_data_plane_run(request: Request):
    """Stage 09: start PL_XODUS_Run for a new batch through the service principal. The approver is the signed-in user."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    form = await request.form()
    selected = [str(x).strip() for x in form.getlist("entities") if str(x).strip()]  # none = a whole new batch
    conn = get_db_connection()
    try:
        art, pkg = _xodus_package(conn, migration_id)
        reasons = []
        entities_param, batch = "", ""
        if not pkg:
            reasons.append("There is no pipeline package: generate one in stage 07.")
        else:
            man = pkg["manifest"]
            if selected and not man.get("waves"):
                reasons.append("This package was built before entity reruns. Generate a new package (stage 07) and publish it (stage 08) to rerun single entities.")
            if selected:  # rerun some entities inside the latest batch; the pipeline's scope check refuses anything that would leave it inconsistent
                unknown = [e for e in selected if e not in man["loadOrder"]]
                if unknown:
                    reasons.append("Not entities of this package: " + ", ".join(unknown) + ".")
                last = adf_runs.latest(conn, migration_id, "PL_XODUS_Run")
                if not last or not last.get("batch_id"):
                    reasons.append("There is no batch to rerun entities in: start a whole batch first.")
                else:
                    batch = last["batch_id"]
                chosen = _with_downstream(selected, man.get("dependsOn") or {}) if form.get("downstream") else sorted(set(selected))
                entities_param = ",".join(e for e in man["loadOrder"] if e in chosen)
            status_info = _adf_status(conn, migration_id)
            if status_info and status_info["stale_reasons"]:
                reasons.append("The package is stale (" + " ".join(status_info["stale_reasons"]) + ") Generate a new one and publish it.")
            if not _merged_pr(conn, migration_id, man["version"]):
                reasons.append("The pull request for this package is not merged (stage 08).")
            dc = deployment_check.latest(conn, migration_id)
            if not dc or dc.get("version") != man["version"] or dc.get("status") != "PASS":
                reasons.append("The deployment check has not passed for this package (stage 08).")
        if workflow_progress().get("validation_status") != "PASS":
            reasons.append("Mapping validation has not passed (stage 05).")
        busy = _run_in_progress(conn, migration_id, "PL_XODUS_Run")
        if busy:
            reasons.append(_already_running_message(busy))
        if reasons:
            return _dp_redirect("/execution", "error", "Not started: " + " ".join(reasons))
        batch = batch or "B" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            req = adf_runs.start(conn, migration_id, "PL_XODUS_Run", {"MigrationId": migration_id, "MigrationSlug": man["migrationSlug"], "BatchId": batch,
                                                                     **({"Entities": entities_param} if man.get("waves") else {})},  # an older pipeline has no such parameter
                                 user["name"], batch_id=batch)
        except ValueError as exc:
            return _dp_redirect("/execution", "error", "Not started: " + str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("DATA_PLANE_RUN_STARTED", user, {"batch_id": batch, "adf_run_id": req["adf_run_id"], "version": man["version"], "entities": entities_param or "ALL"})
    if entities_param:
        return _dp_redirect("/execution", "notice", f"Rerunning {entities_param.replace(',', ', ')} in batch {batch} (Data Factory run {req['adf_run_id']}). "
                                                    "Entities that depend on them and were not selected are now stale and need a rerun before validation.")
    return _dp_redirect("/execution", "notice", f"Batch {batch} started (Data Factory run {req['adf_run_id']}). Raw files load from {man['rawFolder']} of {man['container']}.")


@app.post("/api/data-plane/promote")
async def promote_batch(request: Request):
    """Stage 10, after sign-off: start PL_XODUS_Promote. The pipeline itself refuses unless the gate row matches the mirror."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    state = _data_plane_state(migration_id)
    man, run = state.get("manifest"), state.get("run")
    if not man or not run or run["status"] != "SUCCEEDED":
        return _dp_redirect("/validation-rules", "error", "Promotion needs a succeeded run of the current package.")
    if not state.get("deployment") or state["deployment"]["status"] != "PASS":
        return _dp_redirect("/validation-rules", "error", "The deployment check has not passed for this package.")
    promo = state.get("promote_run")
    if promo and promo.get("batch_id") == run["batch_id"] and promo.get("status") in ("SUCCEEDED", "QUEUED", "INPROGRESS", "CANCELING"):
        return _dp_redirect("/validation-rules", "error", f"Batch {run['batch_id']} is already promoted (or its promotion is running). "
                                                          "Run a new batch to promote new data.")
    conn = get_db_connection()
    try:
        try:
            gate = promotion_gate.status(conn, migration_id, run["batch_id"])
            if not gate:
                raise ValueError(f"Batch {run['batch_id']} has no recorded sign-off. The Validation Specialist signs off the target validation first.")
            busy = _run_in_progress(conn, migration_id, "PL_XODUS_Promote")
            if busy:
                raise ValueError(_already_running_message(busy))
            req = adf_runs.start(conn, migration_id, "PL_XODUS_Promote", {"MigrationId": migration_id, "MigrationSlug": man["migrationSlug"], "BatchId": run["batch_id"]},
                                 user["name"], batch_id=run["batch_id"])
        except ValueError as exc:
            return _dp_redirect("/validation-rules", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("PROMOTION_STARTED", user, {"batch_id": run["batch_id"], "adf_run_id": req["adf_run_id"]})
    return _dp_redirect("/validation-rules", "notice", f"Promotion of batch {run['batch_id']} started (Data Factory run {req['adf_run_id']}).")


@app.get("/api/target-validation/{run_id}/row-keys.csv")
async def target_validation_row_keys(request: Request, run_id: str):
    """The keys of the failing rows of one target validation run (keys only, never other values), for fixing the source data."""
    get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    conn = get_db_connection()
    try:
        target_validation.ensure_table(conn)
        row = conn.execute("SELECT results_json FROM target_validation_runs WHERE run_id = ? AND migration_id = ?", (run_id, migration_id)).fetchone()
        conn.commit()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="No such target validation run.")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["check", "entity", "table", "field", "row_key"])
    for c in json.loads(row["results_json"]).get("checks", []):
        for k in c.get("row_keys") or []:
            w.writerow([c["check"], c.get("entity", ""), c.get("table", ""), c.get("field", ""), k])
    return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{run_id}-row-keys.csv"'})


@app.post("/api/live-validation/run")
async def run_live_validation(request: Request):
    """After a promotion: read-only checks on the live tables in Simplicity (separate from the target validation of the mirror)."""
    user = get_current_user(request)
    migration_id = str(state_store.questionnaire_data.get("migration_id", "MIG-UNCONFIGURED"))
    state = _data_plane_state(migration_id)
    man, run, promo = state.get("manifest"), state.get("run"), state.get("promote_run")
    if not man or not run or not promo or promo["status"] != "SUCCEEDED" or promo.get("batch_id") != run.get("batch_id"):
        return _dp_redirect("/validation-rules", "error", "Live validation runs after the current batch was promoted successfully.")
    conn = get_db_connection()
    try:
        try:
            gate = promotion_gate.status(conn, migration_id, run["batch_id"]) or {}
            dc = deployment_check.latest(conn, migration_id) or {}
            res = live_validation.run(conn, migration_id, run["batch_id"], user["name"], man, gate, dc.get("live_columns") or {})
        except ValueError as exc:
            return _dp_redirect("/validation-rules", "error", str(exc))
        conn.commit()
    finally:
        conn.close()
    _target_setup_audit("LIVE_VALIDATION_RUN", user, {"run_id": res["run_id"], "batch_id": run["batch_id"], "status": res["status"]})
    fails = [c for c in res["checks"] if c["status"] != "PASS"]
    return _dp_redirect("/validation-rules", "notice" if res["status"] == "PASS" else "error",
                        f"Live validation {res['run_id']}: {res['status']}." + ("" if not fails else " " + fails[0]["detail"]))


def _xodus_validation_entities(migration_id: str, man: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Stage 10 checks for the stable-pipeline package: canonical tables vs raw files, mirror tables vs the live columns."""
    conn = get_db_connection()
    try:
        spec = target_connector.spec(conn)
        approved = [m for m in target_mapping.list_mappings(conn, migration_id, spec) if m["status"] == "APPROVED"] if spec else []
        dc = deployment_check.latest(conn, migration_id) or {}
        conn.commit()
    finally:
        conn.close()
    live_cols = dc.get("live_columns") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for t in man["liveTables"]:
        entity, canon = t["entity"], man["canonical"][t["entity"]]
        e = out.setdefault(entity, {"staging_table": canon["table"], "expected_rows": None, "key": canon["key"], "tables": {}, "unmapped_required": []})
        if len(canon["files"]) == 1:
            e["expected_sql"] = f"SELECT COUNT(*) FROM [xodus_raw].[{canon['files'][0]}]"
        if canon.get("xref"):
            e["expected_sql"] = f"SELECT COUNT(DISTINCT [survivor_key]) FROM [xodus_canon].[{canon['xref']}]"
            e["resolution"] = canon.get("resolution") or []
        coded = ((man.get("codeSets") or {}).get("coded") or {}).get(entity) or {}
        if coded and "code_sets" not in e:
            conn = get_db_connection()
            try:
                model = canonical_model.current()
                conn.commit()
            finally:
                conn.close()
            e["code_sets"] = {f: canonical_model.codes(model, cs) for f, cs in coded.items() if cs in model["code_sets"]}
        fields = {f["name"]: f for f in spec["entities"][entity]["fields"]}
        mine = [m for m in approved if m["entity_name"] == entity and m["target_field"] in fields and fields[m["target_field"]]["table"] == t["live_table"] and m.get("canonical_field")]
        live = live_cols.get(t["live_table"], {})
        tt = {"columns": {m["canonical_field"]: fields[m["target_field"]]["column"] for m in mine}, "required": [], "code_lists": {}, "types": {}, "max_lengths": {},
              "row_key": "xodus_source_key"}
        for m in mine:
            f, col = fields[m["target_field"]], fields[m["target_field"]]["column"]
            d = live.get(col, {})
            if f.get("required") or (d and not d.get("nullable", True)):
                tt["required"].append(col)
            kind = d.get("type") or f.get("type")
            if kind and str(kind).lower() not in ("string", "nvarchar", "varchar", "nchar", "char", "text", "ntext", "boolean", "bit"):
                tt["types"][col] = kind
            if isinstance(d.get("max_length"), int) and d["max_length"] > 0:
                tt["max_lengths"][col] = d["max_length"]
            if f.get("codes"):
                tt["code_lists"][col] = list(f["codes"])
        e["tables"][t["mirror_table"]] = tt
        e["unmapped_required"] += [c for c, d in live.items() if not d.get("nullable", True) and c not in t["columns"]]
    return out
