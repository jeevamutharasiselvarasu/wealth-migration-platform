"""Validation sign-off evidence: one JSON document a reviewer can file with the migration record.

It collects what was checked, who accepted which finding and why, and which package and simulated run it relates to.
It contains counts, ids, statuses, names and reasons only - never source record values.
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from control_plane.services import execution_simulator, package_contract, validation_scenarios as vs

SCHEMA = "xodus.signoff-evidence/v1"


def build(conn: sqlite3.Connection, migration_id: str, generated_by: str, drift: Optional[list] = None) -> Dict[str, Any]:
    run = conn.execute("SELECT run_id, status, results_json, executed_by, executed_utc FROM validation_runs WHERE migration_id = ? "
                       "ORDER BY executed_utc DESC LIMIT 1", (migration_id,)).fetchone()
    if not run:
        raise ValueError("Run validation before exporting sign-off evidence.")
    results = json.loads(run["results_json"])
    exceptions = vs.list_exceptions(conn, migration_id, run["run_id"])
    effective = vs.effective_status(results.get("checks", []), exceptions)
    packages = package_contract.list_packages(conn, migration_id)
    runs = execution_simulator.list_runs(conn, migration_id, limit=1)
    doc: Dict[str, Any] = {
        "schema": SCHEMA, "migration_id": migration_id, "generated_utc": datetime.now(timezone.utc).isoformat(), "generated_by": generated_by,
        "validation_run": {"run_id": run["run_id"], "executed_by": run["executed_by"], "executed_utc": run["executed_utc"],
                           "raw_status": run["status"], "effective_status": effective, "summary": results.get("summary", {})},
        "checks": [{k: c.get(k) for k in ("ref", "check", "entity", "file", "field", "status", "detail")} for c in results.get("checks", [])],
        "scenarios": [{"scenario_id": s["scenario_id"], "entity": s["entity_name"], "check_type": s["check_type"], "field": s["field_name"],
                       "stage": s["stage"], "status": s["status"], "why": s["description"], "created_by": s["created_by"]} for s in vs.list_scenarios(conn, migration_id)],
        "exceptions": [{"exception_id": e["exception_id"], "check_ref": e["check_ref"], "reason": e["reason"], "status": e["status"], "proposed_by": e["proposed_by"],
                        "decided_by": e["decided_by"], "decision_comments": e["decision_comments"]} for e in exceptions],
        "source_drift": drift or [],
        "package": ({"package_id": packages[0]["package_id"], "version": packages[0]["package_version"], "content_sha256": packages[0]["content_sha256"],
                     "schema_version": packages[0]["schema_version"]} if packages else None),
        "latest_simulated_execution": None,
        "notes": ["Counts, identifiers and reasons only; no source record values are included.",
                  "Execution evidence is SIMULATED; nothing was loaded to a target system."],
    }
    if runs:
        r = runs[0]
        doc["latest_simulated_execution"] = {"run_id": r["run_id"], "status": r["status"], "mode": r["mode"], "package_version": r["package_version"],
                                             "totals": r["telemetry"].get("totals"),
                                             "problems": [{**{k: f[k] for k in ("entity", "table", "column", "rule", "reason", "rows")},
                                                           "review": ({k: r["reviews"][i][k] for k in ("status", "reviewer", "reason")} if i in r["reviews"] else None)}
                                                          for i, f in enumerate(r["telemetry"].get("failures", []))]}
    return doc
