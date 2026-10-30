"""SIMULATED data-plane execution of a deployment package, with execution telemetry.

This is a stand-in for the ADF data plane: it evaluates a stored package against canonical-shaped rows in
memory, using only the package content (mappings, rule parameters, validation scenarios). Nothing is sent to
Azure and nothing is loaded anywhere; every run is labelled SIMULATED. Telemetry holds counts and links to the
mapping and rule behind each problem, never record values.
"""
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import transformation_rules as tr
from control_plane.services import validation_scenarios as vs

MAX_RETRIES = 3
OUTAGE_MESSAGE = "SIMULATED infrastructure error: integration runtime unavailable. No data was processed."

_DDL = """CREATE TABLE IF NOT EXISTS execution_runs (
    run_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, package_id TEXT NOT NULL, package_version INTEGER NOT NULL,
    mode TEXT NOT NULL, status TEXT NOT NULL, started_utc TEXT NOT NULL, ended_utc TEXT NOT NULL, duration_ms INTEGER NOT NULL,
    triggered_by TEXT NOT NULL, retry_of TEXT, telemetry_json TEXT NOT NULL)"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _sanitise(message: str) -> str:
    """Remove quoted record values from a rule warning so telemetry carries categories only."""
    return re.sub(r"'[^']*'", "'…'", message)


def simulate(package: Dict[str, Any], canonical_rows: Dict[str, List[Dict[str, Any]]], outage: bool = False) -> Dict[str, Any]:
    """Return telemetry for one simulated run of the package. Pure and deterministic."""
    if outage:
        return {"status": "FAILED", "error": OUTAGE_MESSAGE, "retryable": True, "entities": {}, "failures": [], "post_checks": [],
                "totals": {"read": 0, "loaded": 0, "skipped": 0, "failed": 0}}
    rules = {r["expression"]: r for r in package["transformation_rules"]}
    mappings: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for m in package["canonical_to_target"]:
        mappings.setdefault(m["entity"], {}).setdefault(m["target_table"], []).append(m)
    failures: Dict[tuple, Dict[str, Any]] = {}
    entities: Dict[str, Any] = {}
    loaded_rows: Dict[str, List[Dict[str, Any]]] = {}
    for entity, tables in sorted(mappings.items()):
        rows = canonical_rows.get(entity, [])
        per_table: Dict[str, Any] = {}
        entity_loaded: List[Dict[str, Any]] = []
        for table, maps in sorted(tables.items()):
            counts = {"read": len(rows), "loaded": 0, "skipped": 0, "failed": 0}
            for row in rows:
                out: Dict[str, Any] = {}
                warned = blank_required = False
                for m in maps:
                    value = row.get(m["canonical_field"])
                    expr = m["transformation"]
                    if expr != "DIRECT_COPY":
                        rule = rules[expr]
                        value, warning = tr.apply_rule(rule["rule_type"], rule["params"], value, row)
                        if warning:
                            warned = True
                            key = (entity, table, m["target_column"], expr, _sanitise(warning))
                            f = failures.setdefault(key, {"entity": entity, "table": table, "column": m["target_column"], "rule": expr,
                                                          "reason": _sanitise(warning), "rows": 0})
                            f["rows"] += 1
                    out[m["canonical_field"]] = value
                    if m["required"] and (value is None or str(value).strip() == ""):
                        blank_required = True
                        key = (entity, table, m["target_column"], "REQUIRED_BLANK", "required value blank after transformation")
                        f = failures.setdefault(key, {"entity": entity, "table": table, "column": m["target_column"], "rule": m["transformation"],
                                                      "reason": "required value blank after transformation", "rows": 0})
                        f["rows"] += 1
                if blank_required:
                    counts["skipped"] += 1
                elif warned:
                    counts["failed"] += 1
                else:
                    counts["loaded"] += 1
                    entity_loaded.append(out)
            per_table[table] = counts
        entities[entity] = {"source_rows": len(rows), "tables": per_table}
        loaded_rows[entity] = entity_loaded
    totals = {k: sum(t[k] for e in entities.values() for t in e["tables"].values()) for k in ("read", "loaded", "skipped", "failed")}
    post: List[Dict[str, Any]] = []
    for sc in package["validation"]["scenarios"]:
        if sc["stage"] != "POST":
            continue
        scenario = {"check_type": sc["check_type"], "field_name": sc["field"], "params": sc["params"]}
        rows = loaded_rows.get(sc["entity"], [])
        res = vs.evaluate(scenario, "simulated-output", rows) if rows else {"status": "NOT_EVALUATED", "detail": "No rows were loaded for this entity."}
        post.append({"scenario_id": sc["scenario_id"], "entity": sc["entity"], "description": sc["description"], **res})
    for e in entities.values():
        for t in e["tables"].values():
            t["balanced"] = t["loaded"] + t["skipped"] + t["failed"] == t["read"]
    problems = totals["skipped"] + totals["failed"]
    status = "FAILED" if totals["read"] and not totals["loaded"] else ("PARTIAL_SUCCESS" if problems else "SUCCEEDED")
    return {"status": status, "error": None, "retryable": False, "entities": entities, "failures": sorted(failures.values(), key=lambda f: (-f["rows"], f["entity"], f["column"])),
            "post_checks": post, "totals": totals}


def record_run(conn: sqlite3.Connection, migration_id: str, package: Dict[str, Any], actor: str, telemetry: Dict[str, Any],
               started: datetime, duration_ms: int, retry_of: Optional[str] = None) -> str:
    import json
    ensure_table(conn)
    rid = "RUN-" + uuid.uuid4().hex[:8].upper()
    conn.execute("INSERT INTO execution_runs (run_id, migration_id, package_id, package_version, mode, status, started_utc, ended_utc, duration_ms, triggered_by, retry_of, telemetry_json) "
                 "VALUES (?, ?, ?, ?, 'SIMULATED', ?, ?, ?, ?, ?, ?, ?)",
                 (rid, migration_id, package["meta"]["package_id"], package["meta"]["package_version"], telemetry["status"], started.isoformat(),
                  datetime.now(timezone.utc).isoformat(), duration_ms, actor, retry_of, json.dumps(telemetry)))
    return rid


def list_runs(conn: sqlite3.Connection, migration_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    import json
    ensure_table(conn)
    out = []
    for r in conn.execute("SELECT * FROM execution_runs WHERE migration_id = ? ORDER BY started_utc DESC LIMIT ?", (migration_id, limit)).fetchall():
        d = dict(r)
        d["telemetry"] = json.loads(d.pop("telemetry_json"))
        out.append(d)
    return out


def retry_allowed(conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
    """Safeguards: only a failed, retryable run that has not already been retried, within the retry limit."""
    import json
    ensure_table(conn)
    row = conn.execute("SELECT * FROM execution_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        raise ValueError("Run not found.")
    if row["status"] != "FAILED" or not json.loads(row["telemetry_json"]).get("retryable"):
        raise ValueError("Only a run that failed on an infrastructure error can be retried; fix mappings or rules instead.")
    if conn.execute("SELECT 1 FROM execution_runs WHERE retry_of = ?", (run_id,)).fetchone():
        raise ValueError("This run was already retried.")
    chain, cur = 0, row
    while cur and cur["retry_of"]:
        chain += 1
        cur = conn.execute("SELECT * FROM execution_runs WHERE run_id = ?", (cur["retry_of"],)).fetchone()
    if chain >= MAX_RETRIES:
        raise ValueError(f"Retry limit of {MAX_RETRIES} reached.")
    return dict(row)
