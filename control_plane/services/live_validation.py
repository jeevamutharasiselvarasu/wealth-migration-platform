"""Live validation: read-only checks on the live tables in Simplicity after a batch was promoted.

Target validation (before promotion) checks the mirror in the XODUS database; this checks what actually landed in the live tables.
The two databases cannot be queried together, so it relies on what the promotion recorded (the signed-off mirror counts and the
rows the merge inserted or updated) plus checks inside Simplicity. Counts only; for a failed check, the identifiers of up to 50
failing rows are kept (keys only, never other values). Nothing is changed.
"""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from control_plane.services import live_target, sql_target

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_KEYS = 50
_DDL = """
CREATE TABLE IF NOT EXISTS live_validation_runs (
    run_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, batch_id TEXT NOT NULL, status TEXT NOT NULL, results_json TEXT NOT NULL,
    executed_by TEXT NOT NULL, executed_utc TEXT NOT NULL
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _q(name: str) -> str:
    if not IDENT.match(str(name or "")):
        raise ValueError(f"'{name}' is not a safe identifier.")
    return f"[{name}]"


def build_plan(manifest: Dict[str, Any], live_columns: Dict[str, Dict[str, Dict[str, Any]]], gate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Checks for every live table the migration writes. Each has a count query and, where rows can be named, a keys query."""
    schema = _q(manifest["liveSchema"])
    promoted = {r.get("live_table"): r for r in ((gate.get("promotion") or {}).get("tables") or [])}
    plan: List[Dict[str, Any]] = []
    for t in manifest["liveTables"]:
        table, tgt = t["live_table"], f"{schema}.{_q(t['live_table'])}"
        expected = int((gate.get("counts") or {}).get(t["mirror_table"], 0))
        keys = t.get("key_columns") or []
        plan.append({"check": "LIVE_ROWS_PRESENT", "table": table, "field": "", "sql": f"SELECT COUNT(*) FROM {tgt}", "expected": expected,
                     "promoted": promoted.get(table), "keyed": bool(keys)})
        if keys:
            k = _q(keys[0])
            plan.append({"check": "LIVE_KEY_UNIQUE", "table": table, "field": keys[0],
                         "sql": f"SELECT COUNT(*) - COUNT(DISTINCT {k}) FROM {tgt} WHERE {k} IS NOT NULL",
                         "keys_sql": f"SELECT TOP ({MAX_KEYS}) CAST({k} AS nvarchar(400)) FROM {tgt} WHERE {k} IS NOT NULL GROUP BY {k} HAVING COUNT(*) > 1",
                         "key_column": keys[0]})
        cols = live_columns.get(table, {})
        for c in t["columns"]:
            d = cols.get(c)
            if d and not d.get("nullable", True) and d.get("type") in ("nvarchar", "varchar", "nchar", "char"):
                cond = f"LTRIM(RTRIM({_q(c)})) = N''"
                plan.append({"check": "LIVE_NOT_BLANK", "table": table, "field": c, "sql": f"SELECT COUNT(*) FROM {tgt} WHERE {cond}",
                             "keys_sql": (f"SELECT TOP ({MAX_KEYS}) CAST({_q(keys[0])} AS nvarchar(400)) FROM {tgt} WHERE {cond}" if keys else None),
                             "key_column": keys[0] if keys else None})
    return plan


def evaluate(plan: List[Dict[str, Any]], scalar: Callable[[str], int], rows: Callable[[str], List[str]]) -> List[Dict[str, Any]]:
    out = []
    for p in plan:
        base = {"check": p["check"], "table": p["table"], "field": p["field"]}
        try:
            n = int(scalar(p["sql"]))
        except Exception as exc:
            out.append({**base, "status": "NOT_EVALUATED", "detail": f"The query could not run: {type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if p["check"] == "LIVE_ROWS_PRESENT":
            pr = p.get("promoted") or {}
            moved = int(pr.get("inserted") or 0) + int(pr.get("updated") or 0)
            if n < p["expected"] and p["keyed"]:
                status, detail = "FAIL", f"The live table holds {n} row(s); {p['expected']} were signed off and promoted."
            elif p["keyed"] and pr and moved != p["expected"]:
                status, detail = "FAIL", f"The promotion inserted {pr.get('inserted', 0)} and updated {pr.get('updated', 0)} row(s); {p['expected']} were signed off."
            else:
                status = "PASS"
                detail = (f"The live table holds {n} row(s); the promotion inserted {pr.get('inserted', 0)} and updated {pr.get('updated', 0)} of the "
                          f"{p['expected']} signed off." if pr else f"The live table holds {n} row(s); {p['expected']} were signed off.")
                if not p["keyed"]:
                    detail += " This table has no key, so rows already in live are kept and only new ones are added."
            out.append({**base, "status": status, "detail": detail})
            continue
        status = "PASS" if n == 0 else "FAIL"
        detail = {"LIVE_KEY_UNIQUE": "No duplicate keys in the live table." if n == 0 else f"{n} duplicate key value(s) in the live table.",
                  "LIVE_NOT_BLANK": "No blank values." if n == 0 else f"{n} live row(s) hold an empty value in a required column."}[p["check"]]
        row = {**base, "status": status, "detail": detail}
        if n and p.get("keys_sql"):
            try:
                row["row_keys"], row["key_column"] = [str(v) for v in rows(p["keys_sql"])][:MAX_KEYS], p.get("key_column")
            except Exception:
                pass
        out.append(row)
    return out


def run(conn: sqlite3.Connection, migration_id: str, batch_id: str, actor: str, manifest: Dict[str, Any], gate: Dict[str, Any],
        live_columns: Dict[str, Dict[str, Dict[str, Any]]], connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Any]:
    ensure_table(conn)
    cfg = live_target.get_config(conn)
    if not cfg:
        raise ValueError("Set up the live database connection (Platform Setup) first.")
    plan = build_plan(manifest, live_columns, gate)
    cache: Dict[str, list] = {}

    def fetch(sql: str) -> list:
        if sql not in cache:
            cache[sql] = sql_target.run(cfg, [(sql, ())], connect)[0]
        return cache[sql]
    checks = evaluate(plan, lambda q: (fetch(q)[0][0] or 0), lambda q: [r[0] for r in fetch(q)])
    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else ("WARN" if any(c["status"] != "PASS" for c in checks) else "PASS")
    rec = {"run_id": "LVAL-" + uuid.uuid4().hex[:10].upper(), "migration_id": migration_id, "batch_id": batch_id, "status": status,
           "checks": checks, "executed_by": actor, "executed_utc": datetime.now(timezone.utc).isoformat()}
    conn.execute("INSERT INTO live_validation_runs (run_id, migration_id, batch_id, status, results_json, executed_by, executed_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (rec["run_id"], migration_id, batch_id, status, json.dumps({"checks": checks}), actor, rec["executed_utc"]))
    return rec


def latest(conn: sqlite3.Connection, migration_id: str, batch_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    sql, args = "SELECT * FROM live_validation_runs WHERE migration_id = ?", [migration_id]
    if batch_id:
        sql += " AND batch_id = ?"
        args.append(batch_id)
    row = conn.execute(sql + " ORDER BY executed_utc DESC LIMIT 1", args).fetchone()
    return {**dict(row), **json.loads(row["results_json"])} if row else None
