"""Deployment check: is the database side ready for the published pipelines? Read-only; it never creates or changes anything.

XODUS database: the setup pipeline has deployed this package's version, and every table and procedure it needs exists.
Simplicity:     the landing objects are at the same version, and every live table and column the batch writes exists. A live column
                that is NOT NULL but has no approved mapping is a blocker (the promotion would fail). The live tables are never created
                by XODUS: if one is missing, the check says so and stops there.
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from control_plane.services import live_target, sql_target

_DDL = """
CREATE TABLE IF NOT EXISTS deployment_checks (
    migration_id TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL, checked_by TEXT NOT NULL, checked_utc TEXT NOT NULL
)
"""
NUMERIC = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money", "bit"}
DATES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def evaluate(manifest: Dict[str, Any], xodus_version: Optional[str], existing: Dict[str, bool], landing_version: Optional[str],
             live_columns: Dict[str, Dict[str, Dict[str, Any]]], xodus_where: str = "") -> Dict[str, Any]:
    """Pure: turn what was read into findings."""
    checks: List[Dict[str, str]] = []

    def add(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})
    v = manifest["version"]
    add("XODUS_VERSION", "PASS" if xodus_version == v else "FAIL",
        f"The XODUS database has this package's objects (version {v})." if xodus_version == v else
        (f"The XODUS database has version {xodus_version}; this package is {v}. Run {manifest['setupPipeline']}." if xodus_version
         else f"This migration's objects are not deployed in the XODUS database{xodus_where} yet. Run {manifest['setupPipeline']}. "
              "If the setup pipeline already succeeded, the factory's LS_SQL_Target and this platform's database connection point at different databases."))
    missing = sorted(n for n, ok in existing.items() if not ok)
    add("XODUS_OBJECTS", "PASS" if not missing else "FAIL",
        f"All {len(existing)} tables and procedures exist." if not missing else "Missing: " + ", ".join(missing[:12]) + (" ..." if len(missing) > 12 else ""))
    add("LANDING_VERSION", "PASS" if landing_version == v else "FAIL",
        "The Simplicity landing objects match this package." if landing_version == v else
        (f"Simplicity has landing version {landing_version}; this package is {v}. Run {manifest['setupPipeline']}." if landing_version
         else f"The landing objects are not deployed in Simplicity yet. Run {manifest['setupPipeline']}."))
    for t in manifest["liveTables"]:
        live = live_columns.get(t["live_table"])
        name = f"{manifest['liveSchema']}.{t['live_table']}"
        if live is None:
            add("LIVE_TABLE", "FAIL", f"{name} does not exist in Simplicity. XODUS never creates live tables: create it with the Simplicity scripts.")
            continue
        absent = [c for c in t["columns"] if c not in live]
        add("LIVE_COLUMNS", "PASS" if not absent else "FAIL",
            f"{name}: every column the batch writes exists." if not absent else f"{name} has no column(s) " + ", ".join(absent) + ".")
        required = sorted(c for c, d in live.items() if not d["nullable"] and c not in t["columns"])
        add("LIVE_REQUIRED_MAPPED", "PASS" if not required else "FAIL",
            f"{name}: every NOT NULL column has an approved mapping." if not required else
            f"{name}: NOT NULL column(s) without an approved mapping: {', '.join(required)}. The promotion would fail; map them or change the live table.")
        notes = []
        for c in t["columns"]:
            d = live.get(c)
            if d and d["type"] in DATES | NUMERIC:
                notes.append(f"{c} ({d['type']})")
        if notes:
            add("LIVE_TYPES", "WARN", f"{name}: typed columns, checked value by value in stage 10: " + ", ".join(notes) + ".")
    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS"
    return {"status": status, "version": v, "checks": checks,
            "live_columns": {t: {c: d for c, d in cols.items()} for t, cols in live_columns.items() if t in {x["live_table"] for x in manifest["liveTables"]}}}


def run(conn: sqlite3.Connection, migration_id: str, manifest: Dict[str, Any], actor: str,
        connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Any]:
    ensure_table(conn)
    xcfg, lcfg = sql_target.get_config(conn), live_target.get_config(conn)
    if not xcfg:
        raise ValueError("Set up the XODUS database connection (Platform Setup, Target database) first.")
    if not lcfg:
        raise ValueError("Set up the live database connection (Platform Setup, Live database) first.")
    s = manifest["migrationSlug"]
    names = [f"{o}" for o in manifest["objects"]["tables"] + manifest["objects"]["procedures"]]
    stmts = [("SELECT version FROM xodus_ops.xodus_schema_version WHERE migration_slug = %s AND component = 'xodus'", (s,))]
    stmts += [("SELECT CASE WHEN OBJECT_ID(%s) IS NULL THEN 0 ELSE 1 END", (n,)) for n in names]
    try:
        res = sql_target.run(xcfg, [("SELECT CASE WHEN OBJECT_ID(N'xodus_ops.xodus_schema_version', N'U') IS NULL THEN 0 ELSE 1 END", ())], connect)
        xv = sql_target.run(xcfg, stmts[:1], connect)[0] if res[0][0][0] else []
        objs = sql_target.run(xcfg, stmts[1:], connect)
    except ValueError as exc:
        raise ValueError(f"XODUS database: {exc}")
    xodus_version = xv[0][0] if xv else None
    existing = {n: bool(r[0][0]) for n, r in zip(names, objs)}
    try:
        res = sql_target.run(lcfg, [("SELECT CASE WHEN OBJECT_ID(N'xodus_landing.xodus_schema_version', N'U') IS NULL THEN 0 ELSE 1 END", ())], connect)
        lv = sql_target.run(lcfg, [("SELECT version FROM xodus_landing.xodus_schema_version WHERE migration_slug = %s AND component = 'landing'", (s,))], connect)[0] if res[0][0][0] else []
        live_columns = live_target.read_columns(lcfg, connect)
    except ValueError as exc:
        raise ValueError(f"Live database: {exc}")
    where = f" ({xcfg.get('server')} / {xcfg.get('database_name')})" if xcfg.get("database_name") else ""
    result = evaluate(manifest, xodus_version, existing, lv[0][0] if lv else None, live_columns, where)
    result.update({"checked_by": actor, "checked_utc": datetime.now(timezone.utc).isoformat()})
    conn.execute("INSERT INTO deployment_checks (migration_id, version, status, result_json, checked_by, checked_utc) VALUES (?, ?, ?, ?, ?, ?)",
                 (migration_id, result["version"], result["status"], json.dumps(result), actor, result["checked_utc"]))
    return result


def latest(conn: sqlite3.Connection, migration_id: str) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT result_json FROM deployment_checks WHERE migration_id = ? ORDER BY checked_utc DESC LIMIT 1", (migration_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None
