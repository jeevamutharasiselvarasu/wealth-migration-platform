"""Watches a manual Data Factory run through the run-log table the pipeline writes (see adf_package.run_log_ddl).

The platform never signs in to Data Factory. It reads [schema].[xodus_run_log] with the read-only SQL sign-in, works out whether the latest
run is running, succeeded or failed, and for a failure explains the likely cause with a recommended fix. Recommendations are deterministic
pattern matches on the logged error text; nothing is changed automatically.
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from control_plane.services import sql_target

STALL_AFTER = timedelta(minutes=60)  # a run with no end row this long after its start is reported as stalled, not as running
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_DDL = """
CREATE TABLE IF NOT EXISTS adf_run_status (
    migration_id TEXT PRIMARY KEY, pipeline_run_id TEXT, status TEXT NOT NULL, started_utc TEXT, finished_utc TEXT,
    checked_utc TEXT NOT NULL, summary_json TEXT NOT NULL
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_log(conn: sqlite3.Connection, migration_id: str, connect: Callable[..., Any] = sql_target._default_connect, limit: int = 300) -> List[Dict[str, Any]]:
    """Newest-first run-log rows for one migration. Read-only SELECT; raises ValueError with a readable reason when it cannot."""
    cfg = sql_target.get_config(conn)
    if not cfg:
        raise ValueError("Save the SQL settings on the Azure connection page first; the platform reads the run log with that sign-in.")
    password = os.environ.get(cfg["password_env_var"], "")
    cfg["username"] = cfg["username_effective"]
    if not cfg["username"] or not password:
        raise ValueError("The SQL sign-in has no user name or password on the server (see the Azure connection page).")
    schema = cfg["schema_name"]
    if not IDENT.match(schema or ""):
        raise ValueError("The SQL schema name is not a safe identifier.")
    try:
        db = connect(cfg, password)
        try:
            cur = db.cursor()
            cur.execute(f"SELECT TOP {int(limit)} log_id, pipeline_run_id, trigger_file, step_name, status, rows_read, rows_written, message, logged_utc "
                        f"FROM [{schema}].[xodus_run_log] WHERE migration_id = %s ORDER BY log_id DESC", (migration_id,))
            cols = ("log_id", "pipeline_run_id", "trigger_file", "step_name", "status", "rows_read", "rows_written", "message", "logged_utc")
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            db.close()
    except ImportError:
        raise ValueError("The SQL driver (pymssql) is not installed in the server image.")
    except Exception as exc:
        text = str(exc).replace(password, "***")
        if "Invalid object name" in text:
            raise ValueError(f"The run-log table [{schema}].[xodus_run_log] does not exist yet. Run sql/02_run_log.sql in the database.")
        raise ValueError(f"Could not read the run log: {type(exc).__name__}: {text[:300]}")
    for r in rows:
        r["logged_utc"] = str(r["logged_utc"])
    return rows


def _parse(ts: str) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# (pattern, cause, recommendation, platform action or None). First match wins; matched on the lower-cased error text.
_DIAGNOSES = [
    (r"invalid object name.*(staging|xodus_run_log)|there is no such table|cannot find the object",
     "A table the pipeline writes to does not exist in the database.",
     "Run the create scripts once against the database: sql/00_staging_tables.sql, sql/01_target_tables.sql and sql/02_run_log.sql (from the published package).", None),
    (r"invalid column name|column .* does not exist|mapping .* column|source column .* not found|column .* not found",
     "The staging table's columns do not match the columns in the CSV file, so the copy could not map them.",
     "Rewrite the canonical files so the headers match the package, then re-run. If the staging table was created from an older package, re-run sql/00_staging_tables.sql (drop and recreate the staging tables first).", "rewrite_stage"),
    (r"blobnotfound|pathnotfound|specified blob does not exist|the specified path does not exist|no files|file .* not found|does not exist.*(blob|container|path)",
     "The CSV file the dataset points at is not in the data lake folder.",
     "Write the canonical files to stage/ again (button below), then re-run. Check the dataset folder path matches the stage/<migration> folder.", "rewrite_stage"),
    (r"authorizationpermissionmismatch|authorizationfailure|this request is not authorized|403.*blob|forbidden",
     "Data Factory is not allowed to read the storage account.",
     "Give the factory's managed identity (adf-amp-xodus) the Storage Blob Data Reader role on the storage account, and check the storage firewall allows the managed private endpoint.", None),
    (r"login failed|cannot open database|permission was denied|the user .* is not able to access|token-identified principal",
     "The factory identity cannot sign in to the database or lacks a permission.",
     "Create the database user for the factory identity (CREATE USER [adf-amp-xodus] FROM EXTERNAL PROVIDER) and add db_datareader, db_datawriter and db_ddladmin.", None),
    (r"string or binary data would be truncated|truncat",
     "A value is longer than its staging or target column allows.",
     "Find the long value in the CSV (run the preview on stage 06), shorten it at the source or add a rule, or widen the column in the create script.", None),
    (r"conversion failed|cannot convert|arithmetic overflow|invalid date|out-of-range",
     "A value could not be converted to the column's data type during the merge.",
     "Check the column types in the target table against the values (dates and numbers), and add a date or number rule for the field on the Business rules page.", None),
    (r"violation of (primary key|unique)|duplicate key|cannot insert duplicate",
     "The file has more than one row for the same key, so the load could not insert or merge it.",
     "Resolve the duplicate keys in the source (stage 05 reports key uniqueness), then rewrite the canonical files and re-run.", "rewrite_stage"),
    (r"timeout|timed out|network|name resolution|private endpoint|connection .* refused",
     "Data Factory could not reach the database or storage in time.",
     "Check the managed private endpoints are approved and the database firewall allows Azure services, then re-run.", None),
]


def diagnose(step: str, message: str) -> Dict[str, Any]:
    text = (message or "").lower()
    for pattern, cause, advice, action in _DIAGNOSES:
        if re.search(pattern, text):
            return {"step": step, "cause": cause, "recommendation": advice, "action": action, "message": (message or "")[:1200]}
    return {"step": step, "cause": "The step failed with an error the platform does not recognise.",
            "recommendation": "Read the message below, fix the cause in Azure, and re-run. If it points at data, rewrite the canonical files first.",
            "action": None, "message": (message or "")[:1200]}


def summarise(rows: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Latest run from newest-first log rows: overall status, steps and, for a failure, the diagnosis of each failed step."""
    now = now or _now()
    if not rows:
        return {"status": "NO_RUN", "run_id": None, "steps": [], "failures": [], "trigger_file": None, "started_utc": None, "finished_utc": None}
    run_id = rows[0]["pipeline_run_id"]
    mine = sorted([r for r in rows if r["pipeline_run_id"] == run_id], key=lambda r: r["log_id"])
    steps: Dict[str, Dict[str, Any]] = {}
    for r in mine:
        steps[r["step_name"]] = {"step": r["step_name"], "status": r["status"], "rows_read": r["rows_read"], "rows_written": r["rows_written"],
                                 "message": r["message"], "at": r["logged_utc"]}
    failures = [diagnose(s["step"], s["message"] or "") for s in steps.values() if s["status"] == "FAILED"]
    pipeline = steps.get("Pipeline", {})
    started = next((r["logged_utc"] for r in mine if r["step_name"] == "Pipeline" and r["status"] == "STARTED"), mine[0]["logged_utc"])
    if failures:
        status, finished = "FAILED", max(s["at"] for s in steps.values() if s["status"] == "FAILED")
    elif pipeline.get("status") == "SUCCEEDED":
        status, finished = "SUCCEEDED", pipeline["at"]
    else:
        status, finished = "RUNNING", None
        began = _parse(started)
        if began and now - began > STALL_AFTER:
            status = "STALLED"
    return {"status": status, "run_id": run_id, "steps": list(steps.values()), "failures": failures, "trigger_file": mine[0]["trigger_file"],
            "started_utc": started, "finished_utc": finished}


def refresh(conn: sqlite3.Connection, migration_id: str, connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Any]:
    """Read the log, store the latest summary (so the stage rail knows), and return it. A read problem is returned as status UNREADABLE."""
    ensure_table(conn)
    try:
        summary = summarise(fetch_log(conn, migration_id, connect))
    except ValueError as exc:
        return {"status": "UNREADABLE", "error": str(exc), "run_id": None, "steps": [], "failures": []}
    conn.execute("DELETE FROM adf_run_status WHERE migration_id = ?", (migration_id,))
    conn.execute("INSERT INTO adf_run_status (migration_id, pipeline_run_id, status, started_utc, finished_utc, checked_utc, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (migration_id, summary["run_id"], summary["status"], summary["started_utc"], summary["finished_utc"], _now().isoformat(), json.dumps(summary, default=str)))
    return summary


def stored(conn: sqlite3.Connection, migration_id: str) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT summary_json, checked_utc FROM adf_run_status WHERE migration_id = ?", (migration_id,)).fetchone()
    return {**json.loads(row["summary_json"]), "checked_utc": row["checked_utc"]} if row else None


def succeeded(conn: sqlite3.Connection, migration_id: str) -> bool:
    s = stored(conn, migration_id)
    return bool(s and s["status"] == "SUCCEEDED")
