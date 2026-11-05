"""Canonical derivations: a canonical field computed from other canonical fields after source-to-canonical mapping.

Used where the target needs a value no single source column supplies (LEGAL_NAME from the three name parts,
CUSTODIAN_CODE from the custodian name). Each derivation pins an APPROVED governed rule (RULE:<ID>@<version>);
no free-form code is ever executed. Unmapped or blank inputs are reported as blanks, never defaulted.
"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import transformation_rules as rules

_DDL = """
CREATE TABLE IF NOT EXISTS canonical_derivations (
    migration_id TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    canonical_field TEXT NOT NULL,
    input_field TEXT NOT NULL,
    rule_expression TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'APPROVED',
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_utc TEXT NOT NULL,
    PRIMARY KEY (migration_id, entity_name, canonical_field)
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_derivation(conn, migration_id: str, entity: str, canonical_field: str, input_field: str,
                   rule_expression: str, decided_by: str, reason: str) -> Dict[str, Any]:
    """Approve (or replace) the derivation of one canonical field. The rule must be an APPROVED version."""
    ensure_table(conn)
    parsed = rules.parse_expression(rule_expression)
    rule = rules.get_rule(conn, parsed[0], parsed[1]) if parsed else None
    if not rule or rule["status"] != "APPROVED":
        raise ValueError("Pick an approved governed rule (RULE:<ID>@<version>).")
    if not str(reason).strip():
        raise ValueError("A reason is required.")
    if not canonical_field or not input_field:
        raise ValueError("Canonical field and input field are required.")
    mapped = conn.execute(
        "SELECT 1 FROM canonical_mappings WHERE migration_id = ? AND entity_name = ? AND canonical_field = ? AND is_confirmed = 1",
        (migration_id, entity, canonical_field)).fetchone()
    if mapped:
        raise ValueError(f"{canonical_field} already has an approved source mapping; derive only fields with no source column.")
    conn.execute("DELETE FROM canonical_derivations WHERE migration_id = ? AND entity_name = ? AND canonical_field = ?",
                 (migration_id, entity, canonical_field))
    conn.execute(
        "INSERT INTO canonical_derivations (migration_id, entity_name, canonical_field, input_field, rule_expression, status, decided_by, reason, updated_utc) "
        "VALUES (?, ?, ?, ?, ?, 'APPROVED', ?, ?, ?)",
        (migration_id, entity, canonical_field, input_field, rules.make_expression(rule["rule_id"], int(rule["version"])),
         decided_by, str(reason).strip(), _now()))
    return {"entity": entity, "canonical_field": canonical_field, "input_field": input_field,
            "rule_expression": rules.make_expression(rule["rule_id"], int(rule["version"]))}


def remove_derivation(conn, migration_id: str, entity: str, canonical_field: str) -> None:
    ensure_table(conn)
    conn.execute("DELETE FROM canonical_derivations WHERE migration_id = ? AND entity_name = ? AND canonical_field = ?",
                 (migration_id, entity, canonical_field))


def list_derivations(conn, migration_id: str, entity: Optional[str] = None) -> List[Dict[str, Any]]:
    """Approved derivations with their rule resolved (rule_type/params); entries whose rule is no longer APPROVED are dropped."""
    ensure_table(conn)
    sql = "SELECT * FROM canonical_derivations WHERE migration_id = ? AND status = 'APPROVED'"
    args: List[Any] = [migration_id]
    if entity:
        sql += " AND entity_name = ?"
        args.append(entity)
    out = []
    for row in conn.execute(sql + " ORDER BY entity_name, canonical_field", args).fetchall():
        parsed = rules.parse_expression(row["rule_expression"])
        rule = rules.get_rule(conn, parsed[0], parsed[1]) if parsed else None
        if not rule or rule["status"] != "APPROVED":
            continue
        out.append({"entity_name": row["entity_name"], "canonical_field": row["canonical_field"], "input_field": row["input_field"],
                    "rule_expression": row["rule_expression"], "rule_type": rule["rule_type"], "params": rule["params"],
                    "decided_by": row["decided_by"], "reason": row["reason"], "updated_utc": row["updated_utc"]})
    return out


def apply_to_rows(rows: List[Dict[str, Any]], derivations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return rows with each derived field computed from the row's canonical values. Rows are copied, not mutated."""
    if not derivations:
        return rows
    out = []
    for row in rows:
        new = dict(row)
        for d in derivations:
            value, _warning = rules.apply_rule(d["rule_type"], d["params"], new.get(d["input_field"]), record=new)
            new[d["canonical_field"]] = value
        out.append(new)
    return out
