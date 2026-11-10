"""Starts XODUS pipelines and reads their runs through the Data Factory REST API, signed in as the service principal.

Only the published pipelines named in the package are started, with parameters XODUS builds itself. Every request is recorded (who,
when, which batch) so a run always ties back to a person's approval. Run history comes from Data Factory itself, next to the log the
pipelines write to the XODUS database.
"""
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx

from control_plane.services import azure_connection

PIPELINE = re.compile(r"^PL_XODUS_[A-Za-z0-9_]{1,80}$")
_DDL = """
CREATE TABLE IF NOT EXISTS adf_run_requests (
    request_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, batch_id TEXT, pipeline TEXT NOT NULL, adf_run_id TEXT NOT NULL,
    requested_by TEXT NOT NULL, requested_utc TEXT NOT NULL, status TEXT NOT NULL, message TEXT, checked_utc TEXT
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _base(cfg: Dict[str, Any]) -> str:
    return (f"https://management.azure.com/subscriptions/{cfg['subscription_id']}/resourceGroups/{cfg['resource_group']}"
            f"/providers/Microsoft.DataFactory/factories/{cfg['factory_name']}")


def _call(conn: sqlite3.Connection, method: str, tail: str, http: Callable[..., httpx.Response], body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = azure_connection.get_config(conn)
    if not cfg or not (cfg.get("tenant_id") and cfg.get("client_id")):
        raise ValueError("The Data Factory service principal is not set up (Platform Setup, Azure connection).")
    if cfg.get("status") != "CONNECTED":
        raise ValueError("Test the Data Factory connection successfully first (Platform Setup, Azure connection).")
    try:
        token = azure_connection.acquire_token(cfg, "https://management.azure.com/.default", http)
        sep = "&" if "?" in tail else "?"
        r = http(method, f"{_base(cfg)}{tail}{sep}api-version={azure_connection.API}",
                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, **({"json": body} if body is not None else {}))
    except httpx.HTTPError:
        raise ValueError("Could not reach Azure.")
    if r.status_code in (401, 403):
        raise ValueError("The service principal may not do this in the data factory (it needs Data Factory Contributor).")
    if r.status_code == 404:
        raise ValueError("Data Factory does not know this pipeline or run. Merge the pull request and publish it in Data Factory first.")
    if r.status_code >= 300:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise ValueError(f"Data Factory returned HTTP {r.status_code}. {msg[:300]}".strip())
    return r.json() if r.content else {}


def start(conn: sqlite3.Connection, migration_id: str, pipeline: str, parameters: Dict[str, str], actor: str, batch_id: Optional[str] = None,
          http: Callable[..., httpx.Response] = azure_connection._default_http) -> Dict[str, Any]:
    if not PIPELINE.match(pipeline or ""):
        raise ValueError("Only XODUS pipelines can be started from the platform.")
    ensure_table(conn)
    res = _call(conn, "POST", f"/pipelines/{pipeline}/createRun", http, body=parameters)
    run_id = res.get("runId")
    if not run_id:
        raise ValueError("Data Factory accepted the request but returned no run id.")
    rec = {"request_id": "RUN-" + uuid.uuid4().hex[:10].upper(), "migration_id": migration_id, "batch_id": batch_id, "pipeline": pipeline,
           "adf_run_id": run_id, "requested_by": actor, "requested_utc": datetime.now(timezone.utc).isoformat(), "status": "QUEUED"}
    conn.execute("INSERT INTO adf_run_requests (request_id, migration_id, batch_id, pipeline, adf_run_id, requested_by, requested_utc, status) "
                 "VALUES (:request_id, :migration_id, :batch_id, :pipeline, :adf_run_id, :requested_by, :requested_utc, :status)", rec)
    return rec


def refresh(conn: sqlite3.Connection, request: Dict[str, Any], http: Callable[..., httpx.Response] = azure_connection._default_http) -> Dict[str, Any]:
    """Data Factory's own view of a run: status, timings and, for a failure, the failed activities and their errors."""
    run = _call(conn, "GET", f"/pipelineruns/{request['adf_run_id']}", http)
    status = run.get("status") or "Unknown"
    out = {**request, "status": status.upper(), "message": run.get("message") or "", "run_start": run.get("runStart"), "run_end": run.get("runEnd"),
           "duration_ms": run.get("durationInMs"), "failures": []}
    if status in ("Failed", "Cancelled"):
        now = datetime.now(timezone.utc)
        acts = _call(conn, "POST", f"/pipelineruns/{request['adf_run_id']}/queryActivityruns", http,
                     body={"lastUpdatedAfter": (now - timedelta(days=7)).isoformat(), "lastUpdatedBefore": (now + timedelta(hours=1)).isoformat()})
        out["failures"] = [{"activity": a.get("activityName"), "type": a.get("activityType"),
                            "error": str((a.get("error") or {}).get("message") or "")[:1200]}
                           for a in acts.get("value", []) if a.get("status") == "Failed"]
    conn.execute("UPDATE adf_run_requests SET status = ?, message = ?, checked_utc = ? WHERE request_id = ?",
                 (out["status"], out["message"][:2000], datetime.now(timezone.utc).isoformat(), request["request_id"]))
    return out


def latest(conn: sqlite3.Connection, migration_id: str, pipeline: Optional[str] = None) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    sql = "SELECT * FROM adf_run_requests WHERE migration_id = ?"
    args: List[Any] = [migration_id]
    if pipeline:
        sql += " AND pipeline = ?"
        args.append(pipeline)
    row = conn.execute(sql + " ORDER BY requested_utc DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def history(conn: sqlite3.Connection, migration_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM adf_run_requests WHERE migration_id = ? ORDER BY requested_utc DESC LIMIT ?",
                                          (migration_id, limit)).fetchall()]
