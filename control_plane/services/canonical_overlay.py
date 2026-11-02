"""Approved canonical change requests applied as a versioned, audited overlay.

The overlay sits on top of config/firm_config.yaml: it can add optional (nullable,
string) canonical fields that become valid mapping targets. It never changes keys,
required fields or types of the configured contract. The YAML snippet is provided so
the contract in the repository can be updated to match, after which the overlay entry
is redundant. Source-to-canonical only; P0 entities only.
"""
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List

from control_plane.db import get_db_connection

P0_ENTITIES = ("Account", "Client", "Household")
FIELD_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

_DDL = """
CREATE TABLE IF NOT EXISTS canonical_model_overlay (
    overlay_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    migration_id TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    canonical_field TEXT NOT NULL,
    change_type TEXT NOT NULL,
    definition TEXT NOT NULL,
    applied_by TEXT NOT NULL,
    applied_utc TEXT NOT NULL
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def active_added_fields(entity: str) -> List[str]:
    """Canonical fields added through approved ADD_FIELD requests (empty if unavailable)."""
    try:
        conn = get_db_connection()
        try:
            ensure_table(conn)
            rows = conn.execute(
                "SELECT canonical_field FROM canonical_model_overlay WHERE entity_name = ? AND change_type = 'ADD_FIELD' "
                "ORDER BY applied_utc", (entity,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [str(r["canonical_field"]) for r in rows]


def applied_request_ids(conn: sqlite3.Connection) -> Dict[str, str]:
    ensure_table(conn)
    return {r["request_id"]: r["applied_utc"] for r in conn.execute(
        "SELECT request_id, applied_utc FROM canonical_model_overlay").fetchall()}


def yaml_snippet(entity: str, field: str, definition: str) -> str:
    desc = " ".join(str(definition).split()).replace('"', "'")[:200]
    return f'# config/firm_config.yaml -> entities.{entity}.fields\n- {{name: {field}, type: string, nullable: true, description: "{desc}"}}'


def apply_request(conn: sqlite3.Connection, request: Dict[str, str], actor: str, existing_fields: List[str]) -> Dict[str, str]:
    """Record an APPROVED request as applied. Raises ValueError with a user-facing reason."""
    ensure_table(conn)
    if request["status"] != "APPROVED":
        raise ValueError("Only approved change requests can be applied.")
    entity = request["entity_name"]
    if entity not in P0_ENTITIES:
        raise ValueError("Only Account, Client and Household are in scope.")
    field = str(request["canonical_field"]).strip().upper()
    if request["change_type"] == "ADD_FIELD":
        if not FIELD_PATTERN.match(field):
            raise ValueError("Field names must be UPPER_SNAKE_CASE, 2 to 64 characters, starting with a letter.")
        if field in existing_fields or field in active_added_fields(entity):
            raise ValueError(f"{entity}.{field} already exists in the canonical model.")
    if conn.execute("SELECT 1 FROM canonical_model_overlay WHERE request_id = ?", (request["request_id"],)).fetchone():
        raise ValueError("Already applied.")
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "overlay_id": "OVL-" + request["request_id"], "request_id": request["request_id"],
        "migration_id": request["migration_id"], "entity_name": entity, "canonical_field": field,
        "change_type": request["change_type"], "definition": request["proposed_change"],
        "applied_by": actor, "applied_utc": now,
    }
    conn.execute(
        "INSERT INTO canonical_model_overlay (overlay_id, request_id, migration_id, entity_name, canonical_field, "
        "change_type, definition, applied_by, applied_utc) VALUES (:overlay_id, :request_id, :migration_id, :entity_name, "
        ":canonical_field, :change_type, :definition, :applied_by, :applied_utc)", record)
    return record
