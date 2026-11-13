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

from control_plane.services import entity_resolution, run_monitor, sql_target

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
def _relationships() -> tuple:
    from control_plane.services.firm_config import FirmConfigError, reference_fields
    try:
        return tuple((e, f, o, k) for e, fields in reference_fields().items() for f, (o, k) in fields.items())
    except FirmConfigError:
        return ()
MAX_LIST_CODES = 1000  # a reference list longer than this is not checked inline
MAX_ROW_KEYS = 50  # for a failed check, the identifiers (keys only, never other values) of at most this many failing rows are kept
# Live column types a text value must convert to (checked value by value on the mirror, which holds every value as text).
TYPE_SQL = {"date": "date", "datetime": "datetime2", "integer": "bigint", "decimal": "decimal(38, 10)",
            "int": "bigint", "bigint": "bigint", "smallint": "bigint", "tinyint": "bigint", "numeric": "decimal(38, 10)", "float": "float",
            "real": "float", "money": "decimal(38, 10)", "datetime2": "datetime2", "smalldatetime": "datetime2", "datetimeoffset": "datetimeoffset"}


def _code_literal(code: Any) -> str:
    return "N'" + str(code).replace("'", "''") + "'"

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


def _target_of(p: Dict[str, Any]) -> str:
    """The entity a reference points at, named in the check's field ('ACCOUNT_NUMBER → Account.ACCOUNT_NUMBER' gives 'account')."""
    return (str(p.get("field", "")).split("→", 1)[-1].strip().split(".")[0] or "record").lower()


def build_plan(*, schema: str, staging_schema: str, entities: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """entities[E] = {"staging_table", "expected_rows", "key" (canonical key field or None),
                      "tables": {target_table: {"columns": {canonical_field: target_column}, "required": [target_column, ...],
                                                "code_lists": {target_column: [allowed code, ...]}}},
                      "unmapped_required": [target field names]}.
    Returns checks: {"check", "entity", "table", "field", "sql", "kind", "expected"} (sql None = static finding)."""
    plan: List[Dict[str, Any]] = []
    sch, stg = _id(schema), _id(staging_schema)

    def add(check: str, entity: str, table: str, field: str, sql: Optional[str], kind: str, expected: Any = None, detail: str = "",
            keys_sql: Optional[str] = None) -> None:
        plan.append({"check": check, "entity": entity, "table": table, "field": field, "sql": sql, "kind": kind, "expected": expected, "detail": detail,
                     "keys_sql": keys_sql})

    for entity, e in sorted(entities.items()):
        staging = f"{stg}.{_id(e['staging_table'])}"
        add("STAGING_ROW_COUNT", entity, e["staging_table"], "", f"SELECT COUNT(*) FROM {staging}", "equals", e.get("expected_rows"))
        if e.get("expected_sql"):  # the expected count is itself a count query (rows the raw file delivered)
            plan[-1]["expected_sql"] = e["expected_sql"]
        # Entity resolution: after the approved match rules ran, no two canonical rows may still share a match value.
        for fields in e.get("resolution") or []:
            m = entity_resolution.sql_match(fields, "c")
            ck = _id(e["key"]) if e.get("key") else None
            dup = f"SELECT {m} AS [m] FROM {staging} AS c"
            add("DUPLICATES_RESOLVED", entity, e["staging_table"], " + ".join(fields),
                f"SELECT COUNT(*) FROM (SELECT d.[m] FROM ({dup}) AS d WHERE d.[m] IS NOT NULL GROUP BY d.[m] HAVING COUNT(*) > 1) AS x", "zero",
                keys_sql=(f"SELECT TOP ({MAX_ROW_KEYS}) CAST(c.{ck} AS nvarchar(400)) FROM {staging} AS c WHERE {m} IN "
                          f"(SELECT d.[m] FROM ({dup}) AS d WHERE d.[m] IS NOT NULL GROUP BY d.[m] HAVING COUNT(*) > 1)" if ck else None))
        # Canonical conformance (model v2): every populated coded value in the canonical table is a code of its set. A value no one mapped
        # to a canonical code stays as received and is named here by its source key.
        for field, codes in sorted((e.get("code_sets") or {}).items()):
            codes = sorted({str(c) for c in codes if str(c).strip()})
            if not codes:
                continue
            val = f"LTRIM(RTRIM(CAST({_id(field)} AS nvarchar(4000))))"
            cond = f"{_id(field)} IS NOT NULL AND {val} <> '' AND {val} NOT IN ({', '.join(_code_literal(c) for c in codes)})"
            ck = _id(e["key"]) if e.get("key") else None
            add("VALUES_IN_CODE_SET", entity, e["staging_table"], field, f"SELECT COUNT(*) FROM {staging} WHERE {cond}", "zero", expected=len(codes),
                keys_sql=(f"SELECT TOP ({MAX_ROW_KEYS}) CAST({ck} AS nvarchar(400)) FROM {staging} WHERE {cond}" if ck else None))
        for table, t in sorted(e["tables"].items()):
            tgt = f"{sch}.{_id(table)}"
            rk = _id(t["row_key"]) if t.get("row_key") else None  # the column that names a row (the source key), when the table has one

            def keys(cond: str, src: str = tgt, col: Optional[str] = rk) -> Optional[str]:
                return f"SELECT TOP ({MAX_ROW_KEYS}) CAST({col} AS nvarchar(400)) FROM {src} WHERE {cond}" if col else None
            key_col = t["columns"].get(e["key"]) if e.get("key") else None
            if key_col:
                missing = (f"s.{_id(e['key'])} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {tgt} t WHERE t.{_id(key_col)} = s.{_id(e['key'])})")
                add("KEYS_LOADED", entity, table, e["key"], f"SELECT COUNT(DISTINCT s.{_id(e['key'])}) FROM {staging} s WHERE {missing}", "zero",
                    keys_sql=(f"SELECT TOP ({MAX_ROW_KEYS}) CAST(s.{_id(e['key'])} AS nvarchar(400)) FROM {staging} s WHERE {missing}" if rk else None))
                add("TARGET_KEY_UNIQUE", entity, table, key_col,
                    f"SELECT COUNT(*) - COUNT(DISTINCT {_id(key_col)}) FROM {tgt} WHERE {_id(key_col)} IS NOT NULL", "zero",
                    keys_sql=(f"SELECT TOP ({MAX_ROW_KEYS}) CAST({_id(key_col)} AS nvarchar(400)) FROM {tgt} WHERE {_id(key_col)} IS NOT NULL "
                              f"GROUP BY {_id(key_col)} HAVING COUNT(*) > 1" if rk else None))
            else:  # no target column holds the business key, so rows cannot be matched one by one: compare the loaded row count instead
                add("ROWS_LOADED", entity, table, "", f"SELECT COUNT(*) FROM {tgt}", "at_least", e.get("expected_rows"),
                    detail=f"The key field {e.get('key') or ''} is not mapped to a column of {table}, so rows are compared by count.")
                if e.get("expected_sql"):  # stable-pipeline package: compare with the canonical table the mirror was built from
                    plan[-1]["expected_sql"] = f"SELECT COUNT(*) FROM {staging}"
            for col in sorted(set(t.get("required", []))):
                cond = f"{_id(col)} IS NULL OR LTRIM(RTRIM(CAST({_id(col)} AS nvarchar(4000)))) = ''"
                add("REQUIRED_NOT_BLANK", entity, table, col, f"SELECT COUNT(*) FROM {tgt} WHERE {cond}", "zero", keys_sql=keys(cond))
            # Values must fit the live column: its type (date, number) and its length. The mirror holds them as text, so a value that would
            # break the promotion is found here, before any sign-off.
            for col, kind in sorted((t.get("types") or {}).items()):
                target_type = TYPE_SQL.get(str(kind).lower())
                if target_type:
                    val = f"NULLIF(LTRIM(RTRIM(CAST({_id(col)} AS nvarchar(4000)))), '')"
                    cond = f"{val} IS NOT NULL AND TRY_CONVERT({target_type}, {val}) IS NULL"
                    add("VALUES_FIT_TYPE", entity, table, col, f"SELECT COUNT(*) FROM {tgt} WHERE {cond}", "zero", expected=str(kind).lower(), keys_sql=keys(cond))
            for col, length in sorted((t.get("max_lengths") or {}).items()):
                if isinstance(length, int) and length > 0:
                    cond = f"LEN(CAST({_id(col)} AS nvarchar(4000))) > {int(length)}"
                    add("VALUES_FIT_LENGTH", entity, table, col, f"SELECT COUNT(*) FROM {tgt} WHERE {cond}", "zero", expected=int(length), keys_sql=keys(cond))
            # Reference comparison: populated coded values must be in the target system's own reference list.
            for col, codes in sorted((t.get("code_lists") or {}).items()):
                codes = sorted({str(c) for c in codes if str(c).strip()})
                if not codes:
                    continue
                if len(codes) > MAX_LIST_CODES:
                    add("VALUES_IN_REFERENCE_LIST", entity, table, col, None, "static",
                        detail=f"The reference list has {len(codes)} codes, more than the {MAX_LIST_CODES} checked inline; review it separately.")
                    continue
                val = f"LTRIM(RTRIM(CAST({_id(col)} AS nvarchar(4000))))"
                cond = f"{_id(col)} IS NOT NULL AND {val} <> '' AND {val} NOT IN ({', '.join(_code_literal(c) for c in codes)})"
                add("VALUES_IN_REFERENCE_LIST", entity, table, col, f"SELECT COUNT(*) FROM {tgt} WHERE {cond}", "zero", expected=len(codes), keys_sql=keys(cond))
        if e.get("unmapped_required"):
            add("REQUIRED_FIELDS_MAPPED", entity, "", ", ".join(e["unmapped_required"]), None, "warn_static",
                detail="Required target field(s) with no approved mapping: " + ", ".join(e["unmapped_required"]) + ".")
    for src_e, src_f, dst_e, dst_f in _relationships():
        s, d = entities.get(src_e), entities.get(dst_e)
        if not s or not d:
            continue
        pairs = [(st, tt["columns"][src_f]) for st, tt in s["tables"].items() if src_f in tt["columns"]]
        dpairs = [(dt, tt["columns"][dst_f]) for dt, tt in d["tables"].items() if dst_f in tt["columns"]]
        if not pairs or not dpairs:
            continue
        (st, sc), (dt, dc) = pairs[0], dpairs[0]
        orphan = f"a.{_id(sc)} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {sch}.{_id(dt)} h WHERE h.{_id(dc)} = a.{_id(sc)})"
        rk = s["tables"][st].get("row_key")
        add("NO_ORPHANS", src_e, st, f"{sc} → {dst_e}.{dc}", f"SELECT COUNT(*) FROM {sch}.{_id(st)} a WHERE {orphan}", "zero",
            keys_sql=(f"SELECT TOP ({MAX_ROW_KEYS}) CAST(a.{_id(rk)} AS nvarchar(400)) FROM {sch}.{_id(st)} a WHERE {orphan}" if rk else None))
    return plan


def evaluate(plan: List[Dict[str, Any]], execute: Callable[[str], int], run_summary: Optional[Dict[str, Any]],
             fetch_keys: Optional[Callable[[str], List[Any]]] = None) -> List[Dict[str, Any]]:
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
            if p.get("expected_sql"):
                p = {**p, "expected": int(execute(p["expected_sql"]))}
        except Exception as exc:
            checks.append({**base, "status": "NOT_EVALUATED", "detail": f"The query could not run: {type(exc).__name__}: {str(exc)[:200]}"})
            continue
        if p["check"] == "STAGING_ROW_COUNT":
            if p["expected"] is None:
                checks.append({**base, "status": "WARN", "detail": f"Staging holds {n} row(s); the platform has no expected count to compare."})
            else:
                ok = n == p["expected"]
                source = "the raw file delivered" if p.get("expected_sql") else "the platform wrote"
                what = "The canonical table holds" if p.get("expected_sql") else "Staging holds"
                checks.append({**base, "status": "PASS" if ok else "FAIL",
                               "detail": f"{what} {n} row(s); {source} {p['expected']}." + ("" if ok else " They should match (duplicate keys or dropped rows).")})
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
        elif p["check"] == "VALUES_FIT_TYPE":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": f"Every populated value converts to the live column's type ({p['expected']})." if n == 0
                           else f"{n} row(s) hold a value that does not convert to {p['expected']}; the promotion would fail on them."})
        elif p["check"] == "VALUES_FIT_LENGTH":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": f"Every value fits the live column ({p['expected']} characters)." if n == 0
                           else f"{n} row(s) are longer than the live column allows ({p['expected']} characters)."})
        elif p["check"] == "VALUES_IN_REFERENCE_LIST":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": f"Every populated value is one of the {p['expected']} codes in the target's reference list." if n == 0
                           else f"{n} target row(s) hold a value that is not in the target's reference list ({p['expected']} codes)."})
        elif p["check"] == "DUPLICATES_RESOLVED":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": "No two canonical records share this match value; every duplicate was resolved to one record." if n == 0
                           else f"{n} match value(s) are still shared by more than one canonical record."})
        elif p["check"] == "VALUES_IN_CODE_SET":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": f"Every populated value is a canonical code ({p['expected']} codes in the set)." if n == 0
                           else f"{n} canonical row(s) hold a value with no approved canonical code; map it on Business Rules → Code sets."})
        elif p["check"] == "NO_ORPHANS":
            checks.append({**base, "status": "PASS" if n == 0 else "FAIL",
                           "detail": f"Every row points at an existing {_target_of(p)}." if n == 0 else f"{n} row(s) point at a {_target_of(p)} that is not in the target."})
    status = (run_summary or {}).get("status")
    checks.append({"check": "PIPELINE_RUN", "entity": "", "table": "", "field": "",
                   "status": "PASS" if status == "SUCCEEDED" else ("NOT_EVALUATED" if not status or status in ("NO_RUN", "UNREADABLE") else "FAIL"),
                   "detail": {"SUCCEEDED": "The latest pipeline run succeeded with no failed step.", "RUNNING": "The latest pipeline run is still running.",
                              "FAILED": "The latest pipeline run has a failed step.", "STALLED": "The latest pipeline run never finished."}.get(status or "", "No pipeline run has been read from the log yet.")})
    if fetch_keys:  # name the failing rows by their keys (never other values)
        by_ref = {(p["check"], p["entity"], p["table"], p["field"]): p for p in plan}
        for c in checks:
            p = by_ref.get((c["check"], c["entity"], c["table"], c["field"]))
            if c["status"] == "FAIL" and p and p.get("keys_sql"):
                try:
                    c["row_keys"] = [str(v) for v in fetch_keys(p["keys_sql"])][:MAX_ROW_KEYS]
                except Exception:
                    pass
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

    def rows(sql: str) -> list:
        cur = db.cursor()
        cur.execute(sql)
        return [r[0] for r in cur.fetchall()]
    execute.rows = rows  # the same sign-in reads the failing rows' keys
    return execute, db.close, cfg["schema_name"]


def run(conn: sqlite3.Connection, migration_id: str, actor: str, entities: Dict[str, Dict[str, Any]], staging_schema: str = "staging",
        connect: Callable[..., Any] = sql_target._default_connect, schema: Optional[str] = None) -> Dict[str, Any]:
    """Run the predefined checks against the loaded database and store the result. Raises ValueError when the database cannot be read."""
    ensure_table(conn)
    execute, close, cfg_schema = _scalar_runner(conn, connect)
    schema = schema or cfg_schema
    try:
        plan = build_plan(schema=schema, staging_schema=staging_schema, entities=entities)
        checks = evaluate(plan, execute, run_monitor.stored(conn, migration_id), getattr(execute, "rows", None))
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
