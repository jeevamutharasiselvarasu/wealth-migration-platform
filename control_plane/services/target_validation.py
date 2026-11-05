"""Predefined target validation: read-only checks on the data that was actually loaded into the target Azure SQL database.

Every check is a single COUNT query built from the approved mappings and the confirmed target model (identifiers are validated and bracketed;
no value is ever read back or shown, only counts). Findings are accepted only through the exception flow (a different approver), the same as
mapping validation. Nothing in the database is changed.
"""
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from control_plane.services import run_monitor, sql_target

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RELATIONSHIPS = (("Account", "HH_ID", "Household", "HH_ID"), ("Client", "HH_ID", "Household", "HH_ID"))

_DDL = """
CREATE TABLE IF NOT EXISTS target_validation_runs (
    run_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, status TEXT NOT NULL, results_json TEXT NOT NULL,
    executed_by TEXT NOT NULL, executed_utc TEXT NOT NULL
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _id(name: str) -> str:
    if not IDENT.match(str(name or "")):
        raise ValueError(f"'{name}' is not a safe name.")
    return f"[{name}]"


def build_plan(*, schema: str, staging_schema: str, entities: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """entities[E] = {"staging_table", "expected_rows", "key" (canonical key field or None),
                      "tables": {target_table: {"columns": {canonical_field: target_column}, "required": [target_column, ...]}},
                      "unmapped_required": [target field names]}.
    Returns checks: {"check", "entity", "table", "field", "sql", "kind", "expected"} (sql None = static finding)."""
    plan: List[Dict[str, Any]] = []
    sch, stg = _id(schema), _id(staging_schema)

    def add(check: str, entity: str, table: str, field: str, sql: Optional[str], kind: str, expected: Any = None, detail: str = "") -> None:
        plan.append({"check": check, "entity": entity, "table": table, "field": field, "sql": sql, "kind": kind, "expected": expected, "detail": detail})

    for entity, e in sorted(entities.items()):
        staging = f"{stg}.{_id(e['staging_table'])}"
        add("STAGING_ROW_COUNT", entity, e["staging_table"], "", f"SELECT COUNT(*) FROM {staging}", "equals", e.get("expected_rows"))
        for table, t in sorted(e["tables"].items()):
            tgt = f"{sch}.{_id(table)}"
            key_col = t["columns"].get(e["key"]) if e.get("key") else None
            if key_col:
                add("KEYS_LOADED", entity, table, e["key"],
                    f"SELECT COUNT(DISTINCT s.{_id(e['key'])}) FROM {staging} s WHERE s.{_id(e['key'])} IS NOT NULL AND NOT EXISTS "
                    f"(SELECT 1 FROM {tgt} t WHERE t.{_id(key_col)} = s.{_id(e['key'])})", "zero")
                add("TARGET_KEY_UNIQUE", entity, table, key_col,
                    f"SELECT COUNT(*) - COUNT(DISTINCT {_id(key_col)}) FROM {tgt} WHERE {_id(key_col)} IS NOT NULL", "zero")
            else:  # no target column holds the business key, so rows cannot be matched one by one: compare the loaded row count instead
                add("ROWS_LOADED", entity, table, "", f"SELECT COUNT(*) FROM {tgt}", "at_least", e.get("expected_rows"),
                    detail=f"The key field {e.get('key') or ''} is not mapped to a column of {table}, so rows are compared by count.")
            for col in sorted(set(t.get("required", []))):
                add("REQUIRED_NOT_BLANK", entity, table, col,
                    f"SELECT COUNT(*) FROM {tgt} WHERE {_id(col)} IS NULL OR LTRIM(RTRIM(CAST({_id(col)} AS nvarchar(4000)))) = ''", "zero")
        if e.get("unmapped_required"):
            add("REQUIRED_FIELDS_MAPPED", entity, "", ", ".join(e["unmapped_required"]), None, "warn_static",
                detail="Required target field(s) with no approved mapping: " + ", ".join(e["unmapped_required"]) + ".")
    for src_e, src_f, dst_e, dst_f in RELATIONSHIPS:
        s, d = entities.get(src_e), entities.get(dst_e)
        if not s or not d:
            continue
        pairs = [(st, tt["columns"][src_f]) for st, tt in s["tables"].items() if src_f in tt["columns"]]
        dpairs = [(dt, tt["columns"][dst_f]) for dt, tt in d["tables"].items() if dst_f in tt["columns"]]
        if not pairs or not dpairs:
            continue
        (st, sc), (dt, dc) = pairs[0], dpairs[0]
        add("NO_ORPHANS", src_e, st, f"{sc} → {dst_e}.{dc}",
            f"SELECT COUNT(*) FROM {sch}.{_id(st)} a WHERE a.{_id(sc)} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {sch}.{_id(dt)} h WHERE h.{_id(dc)} = a.{_id(sc)})", "zero")
    return plan


def evaluate(plan: List[Dict[str, Any]], execute: Callable[[str], int], run_summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run each planned query (execute returns one integer) and turn the counts into findings. Counts only, never values."""
    checks: List[Dict[str, Any]] = []
    for p in plan:
        base = {"check": p["check"], "entity": p["entity"], "table": p["table"], "field": p["field"]}
        if p["kind"] == "static":
            checks.append({**base, "status": "NOT_EVALUATED", "detail": p["detail"]})
            continue
        if p["kind"] == "warn_static":
            checks.append({**base, "status": "WARN", "detail": p["detail"]})
            continue
        try:
            n = int(execute(p["sql"]))
        except Exception as exc:
            checks.append({**base, "status": "NOT_EVALUATED", "detail": f"The query could not run: {type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if p["check"] == "STAGING_ROW_COUNT":
            if p["expected"] is None:
                checks.append({**base, "status": "WARN", "detail": f"Staging holds {n} row(s); the platform has no expected count to compare."})
            else:
                ok = n == p["expected"]
                checks.append({**base, "status": "PASS" if ok else "FAIL",
                               "detail": f"Staging holds {n} row(s); the platform wrote {p['expected']} to the canonical file." + ("" if ok else " They should match.")})
        elif p["check"] == "ROWS_LOADED":
            if p["expected"] is None:
                checks.append({**base, "status": "WARN", "detail": f"The target table holds {n} row(s); the platform has no expected count to compare."})
            else:
                ok = n >= p["expected"]
                checks.append({**base, "status": "PASS" if ok else "FAIL",
                               "detail": f"The target table holds {n} row(s); the platform sent {p['expected']}. " + ("(No business key is mapped here, so rows are compared by count.)" if ok
                                                                                                               else "It should hold at least that many.")})
        elif p["check"] == "KEYS_LOADED":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": "Every staging key is present in the target." if n == 0 else f"{n} staging key(s) are missing from the target table."})
        elif p["check"] == "TARGET_KEY_UNIQUE":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": "No duplicate keys in the target." if n == 0 else f"{n} duplicate key value(s) in the target table."})
        elif p["check"] == "REQUIRED_NOT_BLANK":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": "Populated in every target row." if n == 0 else f"{n} target row(s) have a blank value."})
        elif p["check"] == "NO_ORPHANS":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": "Every row points at an existing household." if n == 0 else f"{n} row(s) point at a household that is not in the target."})
    status = (run_summary or {}).get("status")
    checks.append({"check": "PIPELINE_RUN", "entity": "", "table": "", "field": "",
                   "status": "PASS" if status == "SUCCEEDED" else ("NOT_EVALUATED" if not status or status in ("NO_RUN", "UNREADABLE") else "FAIL"),
                   "detail": {"SUCCEEDED": "The latest pipeline run succeeded with no failed step.", "RUNNING": "The latest pipeline run is still running.",
                              "FAILED": "The latest pipeline run has a failed step.", "STALLED": "The latest pipeline run never finished."}.get(status or "", "No pipeline run has been read from the log yet.")})
    for c in checks:
        c["ref"] = "|".join(str(c.get(k, "")) for k in ("check", "entity", "table", "field"))
    return checks


def overall(checks: List[Dict[str, Any]]) -> str:
    statuses = {c["status"] for c in checks}
    return "FAIL" if "FAIL" in statuses else ("WARN" if statuses & {"WARN", "NOT_EVALUATED"} else "PASS")


def _scalar_runner(conn: sqlite3.Connection, connect: Callable[..., Any] = sql_target._default_connect):
    cfg = sql_target.get_config(conn)
    if not cfg:
        raise ValueError("Save the SQL settings on the Azure connection page first.")
    password = os.environ.get(cfg["password_env_var"], "")
    cfg["username"] = cfg["username_effective"]
    if not cfg["username"] or not password:
        raise ValueError("The SQL sign-in has no user name or password on the server (see the Azure connection page).")
    try:
        db = connect(cfg, password)
    except ImportError:
        raise ValueError("The SQL driver (pymssql) is not installed in the server image.")
    except Exception as exc:
        raise ValueError(f"Could not sign in to the database: {type(exc).__name__}: {str(exc)[:200].replace(password, '***')}")

    def execute(sql: str) -> int:
        cur = db.cursor()
        cur.execute(sql)
        return int(cur.fetchone()[0] or 0)
    return execute, db.close, cfg["schema_name"]


def run(conn: sqlite3.Connection, migration_id: str, actor: str, entities: Dict[str, Dict[str, Any]], staging_schema: str = "staging",
        connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Any]:
    """Run the predefined checks against the loaded database and store the result. Raises ValueError when the database cannot be read."""
    ensure_table(conn)
    execute, close, schema = _scalar_runner(conn, connect)
    try:
        plan = build_plan(schema=schema, staging_schema=staging_schema, entities=entities)
        checks = evaluate(plan, execute, run_monitor.stored(conn, migration_id))
    finally:
        close()
    result = {"status": overall(checks), "checks": checks, "summary": {s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "WARN", "FAIL", "NOT_EVALUATED")}}
    run_id = "TVAL-" + uuid.uuid4().hex[:10].upper()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO target_validation_runs (run_id, migration_id, status, results_json, executed_by, executed_utc) VALUES (?, ?, ?, ?, ?, ?)",
                 (run_id, migration_id, result["status"], json.dumps(result), actor, now))
    return {"run_id": run_id, "executed_by": actor, "executed_utc": now, **result}


def latest(conn: sqlite3.Connection, migration_id: str) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM target_validation_runs WHERE migration_id = ? ORDER BY executed_utc DESC LIMIT 1", (migration_id,)).fetchone()
    return {"run_id": row["run_id"], "executed_by": row["executed_by"], "executed_utc": row["executed_utc"], **json.loads(row["results_json"])} if row else None
