"""Validator-defined validation scenarios and governed validation exceptions.

Scenarios are declarative (an allow-listed check type plus validated parameters) and are evaluated on
canonical-shaped rows in memory; nothing is executed as code. A Validation Specialist defines scenarios
and proposes exceptions; a Migration Analyst or Platform Admin approves or rejects an exception, and never
their own proposal.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import transformation_rules as tr

CHECK_TYPES: Dict[str, str] = {
    "NOT_BLANK": "Field is populated in every row",
    "UNIQUE": "Field values are unique within a file",
    "ALLOWED_VALUES": "Field only holds listed values (case-insensitive)",
    "MAX_LENGTH": "Field is at most N characters",
    "NUMERIC_RANGE": "Field is a number within min and max",
    "DATE_PARSEABLE": "Field parses as a date in a declared format",
    "ROW_COUNT_MIN": "File set supplies at least N rows for the entity",
}
STAGES = ("PRE", "POST")
ENTITIES = ("Account", "Client", "Household")
FIELD_OK = tr.FIELD_PATTERN

_DDL = (
    """CREATE TABLE IF NOT EXISTS validation_scenarios (
        scenario_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, entity_name TEXT NOT NULL, check_type TEXT NOT NULL,
        field_name TEXT, params_json TEXT NOT NULL, stage TEXT NOT NULL, description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE', created_by TEXT NOT NULL, created_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS validation_exceptions (
        exception_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, run_id TEXT NOT NULL, check_ref TEXT NOT NULL,
        reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PROPOSED', proposed_by TEXT NOT NULL, proposed_utc TEXT NOT NULL,
        decided_by TEXT, decided_utc TEXT, decision_comments TEXT, UNIQUE (migration_id, run_id, check_ref))""",
)


def ensure_tables(conn: sqlite3.Connection) -> None:
    for ddl in _DDL:
        conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_params(check_type: str, field: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if check_type not in CHECK_TYPES:
        raise ValueError(f"Unsupported check type '{check_type}'.")
    if check_type != "ROW_COUNT_MIN" and not FIELD_OK.match(field or ""):
        raise ValueError("Field must be a canonical field name such as ACCT_NUM.")
    if check_type == "ALLOWED_VALUES":
        vals = [str(v).strip() for v in params.get("values", []) if str(v).strip()]
        if not vals or len(vals) > 200:
            raise ValueError("ALLOWED_VALUES needs 1 to 200 values.")
        return {"values": sorted(set(vals))}
    if check_type in ("MAX_LENGTH", "ROW_COUNT_MIN"):
        key = "max" if check_type == "MAX_LENGTH" else "min"
        n = params.get(key)
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 1_000_000:
            raise ValueError(f"{check_type} needs an integer {key} from 0 to 1,000,000.")
        return {key: n}
    if check_type == "NUMERIC_RANGE":
        lo, hi = params.get("min"), params.get("max")
        if lo is None and hi is None:
            raise ValueError("NUMERIC_RANGE needs a min, a max, or both.")
        for v in (lo, hi):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise ValueError("NUMERIC_RANGE min and max must be numbers.")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("NUMERIC_RANGE min must not exceed max.")
        return {"min": lo, "max": hi}
    if check_type == "DATE_PARSEABLE":
        formats = params.get("input_formats")
        if not isinstance(formats, list) or not formats or any(f not in tr.DATE_FORMATS for f in formats):
            raise ValueError("DATE_PARSEABLE needs declared formats: " + ", ".join(tr.DATE_FORMATS))
        return {"input_formats": list(dict.fromkeys(formats))}
    return {}


def create_scenario(conn: sqlite3.Connection, migration_id: str, entity: str, check_type: str, field: str,
                    params: Dict[str, Any], stage: str, description: str, actor: str) -> Dict[str, Any]:
    ensure_tables(conn)
    if entity not in ENTITIES:
        raise ValueError("Entity must be Account, Client or Household.")
    if stage not in STAGES:
        raise ValueError("Stage must be PRE or POST.")
    if not str(description or "").strip():
        raise ValueError("A description of the business reason is required.")
    field = str(field or "").strip().upper()
    clean = validate_params(check_type, field, params)
    sid = "SCN-" + uuid.uuid4().hex[:8].upper()
    conn.execute("INSERT INTO validation_scenarios (scenario_id, migration_id, entity_name, check_type, field_name, params_json, stage, description, "
                 "created_by, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (sid, migration_id, entity, check_type, field or None, json.dumps(clean, sort_keys=True), stage, description.strip(), actor, _now()))
    return {"scenario_id": sid}


def list_scenarios(conn: sqlite3.Connection, migration_id: str, active_only: bool = False) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    rows = conn.execute("SELECT * FROM validation_scenarios WHERE migration_id = ? " + ("AND status = 'ACTIVE' " if active_only else "") +
                        "ORDER BY created_utc", (migration_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json"))
        out.append(d)
    return out


def retire_scenario(conn: sqlite3.Connection, scenario_id: str) -> None:
    ensure_tables(conn)
    if not conn.execute("UPDATE validation_scenarios SET status = 'RETIRED' WHERE scenario_id = ? AND status = 'ACTIVE'", (scenario_id,)).rowcount:
        raise ValueError("No active scenario with that id.")


def _blank(v: Any) -> bool:
    return v is None or str(v).strip() == ""


def evaluate(scenario: Dict[str, Any], filename: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluate one scenario against one file's canonical rows. Returns status and a count-only detail (no values)."""
    t, f, p = scenario["check_type"], scenario.get("field_name"), scenario["params"]
    vals = [r.get(f) for r in rows] if f else []
    filled = [str(v).strip() for v in vals if not _blank(v)]
    if t == "NOT_BLANK":
        bad = len(vals) - len(filled)
        return _res(bad, f"{bad} blank value(s) in {len(vals)} row(s).", f"All {len(vals)} row(s) populated.")
    if t == "UNIQUE":
        dup = len(filled) - len(set(filled))
        return _res(dup, f"{dup} duplicate value(s).", "No duplicates.")
    if t == "ALLOWED_VALUES":
        allowed = {v.upper() for v in p["values"]}
        bad = sum(1 for v in filled if v.upper() not in allowed)
        return _res(bad, f"{bad} value(s) outside the allowed list.", "All values are in the allowed list.")
    if t == "MAX_LENGTH":
        bad = sum(1 for v in filled if len(v) > p["max"])
        return _res(bad, f"{bad} value(s) longer than {p['max']}.", f"All values within {p['max']} characters.")
    if t == "NUMERIC_RANGE":
        bad = 0
        for v in filled:
            try:
                n = float(v.replace(",", ""))
            except ValueError:
                bad += 1
                continue
            bad += (p.get("min") is not None and n < p["min"]) or (p.get("max") is not None and n > p["max"])
        return _res(bad, f"{bad} value(s) not numeric or out of range.", "All values numeric and in range.")
    if t == "DATE_PARSEABLE":
        from datetime import datetime as dt
        def ok(v: str) -> bool:
            return any(_try(dt, v, tr.DATE_FORMATS[n]) for n in p["input_formats"])
        bad = sum(1 for v in filled if not ok(v))
        return _res(bad, f"{bad} value(s) not in a declared date format.", "All values parse as dates.")
    if t == "ROW_COUNT_MIN":
        n = len(rows)
        return _res(max(0, p["min"] - n), f"{n} row(s); at least {p['min']} expected.", f"{n} row(s) supplied.")
    raise ValueError(f"Unsupported check type '{t}'.")


def _try(dt, v, fmt) -> bool:
    try:
        dt.strptime(v, fmt)
        return True
    except ValueError:
        return False


def _res(bad: int, fail_msg: str, ok_msg: str) -> Dict[str, Any]:
    return {"status": "FAIL" if bad else "PASS", "detail": fail_msg if bad else ok_msg}


def propose_exception(conn: sqlite3.Connection, migration_id: str, run_id: str, check_ref: str, reason: str, actor: str) -> str:
    ensure_tables(conn)
    if not str(reason or "").strip():
        raise ValueError("A reason is required to propose an exception.")
    eid = "EXC-" + uuid.uuid4().hex[:8].upper()
    try:
        conn.execute("INSERT INTO validation_exceptions (exception_id, migration_id, run_id, check_ref, reason, proposed_by, proposed_utc) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (eid, migration_id, run_id, check_ref, reason.strip(), actor, _now()))
    except sqlite3.IntegrityError:
        raise ValueError("An exception already exists for that check in this run.")
    return eid


def decide_exception(conn: sqlite3.Connection, exception_id: str, decision: str, actor: str, comments: str) -> Dict[str, Any]:
    ensure_tables(conn)
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("Decision must be APPROVED or REJECTED.")
    row = conn.execute("SELECT * FROM validation_exceptions WHERE exception_id = ?", (exception_id,)).fetchone()
    if not row:
        raise ValueError("Exception not found.")
    if row["status"] != "PROPOSED":
        raise ValueError(f"Exception is already {row['status']}.")
    if row["proposed_by"] == actor:
        raise ValueError("You cannot decide an exception you proposed.")
    if decision == "REJECTED" and not str(comments or "").strip():
        raise ValueError("A comment is required to reject an exception.")
    conn.execute("UPDATE validation_exceptions SET status = ?, decided_by = ?, decided_utc = ?, decision_comments = ? WHERE exception_id = ?",
                 (decision, actor, _now(), str(comments or "").strip(), exception_id))
    return dict(conn.execute("SELECT * FROM validation_exceptions WHERE exception_id = ?", (exception_id,)).fetchone())


def list_exceptions(conn: sqlite3.Connection, migration_id: str, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    q, a = "SELECT * FROM validation_exceptions WHERE migration_id = ?", [migration_id]
    if run_id:
        q, a = q + " AND run_id = ?", a + [run_id]
    return [dict(r) for r in conn.execute(q + " ORDER BY proposed_utc", a).fetchall()]


def effective_status(checks: List[Dict[str, Any]], exceptions: List[Dict[str, Any]]) -> str:
    """Overall status after removing findings covered by an APPROVED exception."""
    accepted = {e["check_ref"] for e in exceptions if e["status"] == "APPROVED"}
    statuses = {c["status"] for c in checks if c.get("ref") not in accepted or c["status"] == "PASS"}
    return "FAIL" if "FAIL" in statuses else ("WARN" if statuses & {"WARN", "NOT_EVALUATED"} else "PASS")
