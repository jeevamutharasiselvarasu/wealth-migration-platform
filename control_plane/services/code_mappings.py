"""Code-set mapping: source values -> canonical codes (per migration, approved by the analyst on Business Rules) and canonical codes ->
target values (per target system, set by the Platform Admin on Target setup and reused by every migration into that target).

Both are data, not logic: the package writes them into xodus_ref tables that the generated procedures read, so a new source value is a
reviewed data change. Values are compared normalised (canonical_model.normalise); only distinct values are kept, never whole rows.
"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from control_plane.services import canonical_model

STATUSES = {"PROPOSED", "APPROVED", "REJECTED"}
_DDL = (
    """CREATE TABLE IF NOT EXISTS code_mappings (
        migration_id TEXT NOT NULL, code_set TEXT NOT NULL, source_value TEXT NOT NULL, canonical_code TEXT, status TEXT NOT NULL,
        reason TEXT, fields TEXT, row_count INTEGER NOT NULL DEFAULT 0, proposed_utc TEXT NOT NULL, decided_by TEXT, decided_utc TEXT,
        PRIMARY KEY (migration_id, code_set, source_value)
    )""",
    """CREATE TABLE IF NOT EXISTS target_code_crosswalk (
        target_system TEXT NOT NULL, code_set TEXT NOT NULL, canonical_code TEXT NOT NULL, target_value TEXT NOT NULL,
        updated_by TEXT NOT NULL, updated_utc TEXT NOT NULL, PRIMARY KEY (target_system, code_set, canonical_code)
    )""",
)


def ensure_tables(conn: sqlite3.Connection) -> None:
    for ddl in _DDL:
        conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def profile(model: Dict[str, Any], rows: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """{code_set: {normalised value: {"rows": n, "fields": {"Entity.FIELD"}}}} from canonical-shaped rows (blank values are skipped)."""
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for entity, fields in canonical_model.coded_fields(model).items():
        for row in rows.get(entity, []):
            for field, code_set in fields.items():
                v = canonical_model.normalise(row.get(field))
                if not v:
                    continue
                rec = out.setdefault(code_set, {}).setdefault(v, {"rows": 0, "fields": set()})
                rec["rows"] += 1
                rec["fields"].add(f"{entity}.{field}")
    return out


def propose(conn: sqlite3.Connection, migration_id: str, model: Dict[str, Any], rows: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Profile the values and propose a canonical code for every value not seen before. Decided values keep their decision (their counts
    are refreshed; an undecided value the mappings no longer produce is dropped). Returns {"values", "new", "suggested"}."""
    ensure_tables(conn)
    seen = profile(model, rows)
    new = suggested = 0
    # A value nobody has decided and the approved mappings no longer produce (its source mapping was reopened or rejected) is dropped.
    for r in conn.execute("SELECT code_set, source_value FROM code_mappings WHERE migration_id = ? AND status = 'PROPOSED'", (migration_id,)).fetchall():
        if r["source_value"] not in seen.get(r["code_set"], {}):
            conn.execute("DELETE FROM code_mappings WHERE migration_id = ? AND code_set = ? AND source_value = ? AND status = 'PROPOSED'",
                         (migration_id, r["code_set"], r["source_value"]))
    for code_set, values in seen.items():
        for value, rec in values.items():
            fields = ", ".join(sorted(rec["fields"]))
            row = conn.execute("SELECT status FROM code_mappings WHERE migration_id = ? AND code_set = ? AND source_value = ?",
                               (migration_id, code_set, value)).fetchone()
            if row:
                conn.execute("UPDATE code_mappings SET row_count = ?, fields = ? WHERE migration_id = ? AND code_set = ? AND source_value = ?",
                             (rec["rows"], fields, migration_id, code_set, value))
                continue
            code, reason = canonical_model.suggest(model, code_set, value)
            conn.execute("INSERT INTO code_mappings (migration_id, code_set, source_value, canonical_code, status, reason, fields, row_count, proposed_utc) "
                         "VALUES (?, ?, ?, ?, 'PROPOSED', ?, ?, ?, ?)", (migration_id, code_set, value, code, reason, fields, rec["rows"], _now()))
            new += 1
            suggested += 1 if code else 0
    return {"values": sum(len(v) for v in seen.values()), "new": new, "suggested": suggested}


def list_mappings(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM code_mappings WHERE migration_id = ? ORDER BY code_set, source_value", (migration_id,)).fetchall()]


def counts(conn: sqlite3.Connection, migration_id: str) -> Dict[str, int]:
    ensure_tables(conn)
    out = {s: 0 for s in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM code_mappings WHERE migration_id = ? GROUP BY status", (migration_id,)).fetchall():
        out[r["status"]] = r["n"]
    return out


def decide(conn: sqlite3.Connection, migration_id: str, model: Dict[str, Any], code_set: str, source_value: str, decision: str,
           canonical_code: Optional[str], actor: str) -> Dict[str, Any]:
    """APPROVE with a code of the set (the suggestion or another), REJECT (the value stays unmapped and fails conformance), or REOPEN."""
    ensure_tables(conn)
    decision = str(decision or "").upper()
    row = conn.execute("SELECT * FROM code_mappings WHERE migration_id = ? AND code_set = ? AND source_value = ?",
                       (migration_id, code_set, source_value)).fetchone()
    if not row:
        raise ValueError("That value is not among the profiled values.")
    if decision == "APPROVE":
        code = str(canonical_code or row["canonical_code"] or "").strip().upper()
        if code not in canonical_model.codes(model, code_set):
            raise ValueError(f"Choose a code of {code_set} for '{source_value}'.")
        status, value = "APPROVED", code
    elif decision == "REJECT":
        status, value = "REJECTED", row["canonical_code"]
    elif decision == "REOPEN":
        conn.execute("UPDATE code_mappings SET status = 'PROPOSED', decided_by = NULL, decided_utc = NULL WHERE migration_id = ? AND code_set = ? AND source_value = ?",
                     (migration_id, code_set, source_value))
        return {**dict(row), "status": "PROPOSED"}
    else:
        raise ValueError("Unknown decision.")
    when = _now()
    conn.execute("UPDATE code_mappings SET status = ?, canonical_code = ?, decided_by = ?, decided_utc = ? WHERE migration_id = ? AND code_set = ? AND source_value = ?",
                 (status, value, actor, when, migration_id, code_set, source_value))
    return {**dict(row), "status": status, "canonical_code": value, "decided_by": actor, "decided_utc": when}


def approve_suggested(conn: sqlite3.Connection, migration_id: str, model: Dict[str, Any], actor: str) -> int:
    """Approve every proposed value that has a suggested code (values without a suggestion are left for a person to choose)."""
    rows = [r for r in list_mappings(conn, migration_id) if r["status"] == "PROPOSED" and r["canonical_code"]]
    for r in rows:
        decide(conn, migration_id, model, r["code_set"], r["source_value"], "APPROVE", r["canonical_code"], actor)
    return len(rows)


def approved_source_map(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, str]]:
    return [{"code_set": r["code_set"], "source_value": r["source_value"], "canonical_code": r["canonical_code"]}
            for r in list_mappings(conn, migration_id) if r["status"] == "APPROVED"]


def crosswalk(conn: sqlite3.Connection, target_system: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """{code_set: {canonical_code: {"target_value", "updated_by", "updated_utc"}}} for a target system."""
    ensure_tables(conn)
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in conn.execute("SELECT * FROM target_code_crosswalk WHERE target_system = ?", (target_system,)).fetchall():
        out.setdefault(r["code_set"], {})[r["canonical_code"]] = {"target_value": r["target_value"], "updated_by": r["updated_by"], "updated_utc": r["updated_utc"]}
    return out


def save_crosswalk(conn: sqlite3.Connection, target_system: str, model: Dict[str, Any], code_set: str, values: Dict[str, str], actor: str) -> int:
    """Set the target value of each canonical code of a set; a blank value removes the entry (the canonical code then passes through).
    Returns the number of entries kept."""
    ensure_tables(conn)
    if not str(target_system or "").strip():
        raise ValueError("Confirm the target system first.")
    if code_set not in (model.get("code_sets") or {}):
        raise ValueError(f"Unknown code set {code_set}.")
    allowed = set(canonical_model.codes(model, code_set))
    kept = 0
    for code, value in values.items():
        if code not in allowed:
            raise ValueError(f"{code} is not a code of {code_set}.")
        value = str(value or "").strip()
        if len(value) > 400:
            raise ValueError(f"The target value for {code} is longer than 400 characters.")
        conn.execute("DELETE FROM target_code_crosswalk WHERE target_system = ? AND code_set = ? AND canonical_code = ?", (target_system, code_set, code))
        if value:
            conn.execute("INSERT INTO target_code_crosswalk (target_system, code_set, canonical_code, target_value, updated_by, updated_utc) VALUES (?, ?, ?, ?, ?, ?)",
                         (target_system, code_set, code, value, actor, _now()))
            kept += 1
    return kept


def target_map(conn: sqlite3.Connection, target_system: str) -> List[Dict[str, str]]:
    return [{"code_set": cs, "canonical_code": code, "target_value": v["target_value"]}
            for cs, codes in sorted(crosswalk(conn, target_system).items()) for code, v in sorted(codes.items())]


def unmapped_codes(model: Dict[str, Any], used_sets: Iterable[str], cw: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """Codes of the given sets with no target value (they pass through to the target unchanged)."""
    return {cs: [c for c in canonical_model.codes(model, cs) if c not in cw.get(cs, {})] for cs in sorted(set(used_sets))}
