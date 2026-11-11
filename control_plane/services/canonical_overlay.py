"""Approved canonical change requests applied as a versioned, audited overlay.

The overlay sits on top of config/firm_config.yaml: it can add optional (nullable,
string) canonical fields that become valid mapping targets. It never changes keys,
required fields or types of the configured contract. The YAML snippet is provided so
the contract in the repository can be updated to match, after which the overlay entry
is redundant. Source-to-canonical only; P0 entities only.

Releases: an applied change is live at once only in the workspace that raised it. The Domain SME (or Platform Admin)
publishes the applied changes as a numbered canonical release. Every workspace is pinned to a release and keeps it until
a Migration Analyst adopts a newer one, so a release never changes a running migration's mapping targets by surprise.
"""
from control_plane.services.firm_config import P0_ENTITIES as _P0
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from control_plane.db import get_db_connection, table_columns

# The active migration workspace, set by the application. Services and agents call active_added_fields without knowing it.
ACTIVE_MIGRATION: Callable[[], Optional[str]] = lambda: None

P0_ENTITIES = _P0
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


_RELEASES_DDL = """CREATE TABLE IF NOT EXISTS canonical_releases (
    release_no INTEGER PRIMARY KEY, released_by TEXT NOT NULL, released_utc TEXT NOT NULL, notes TEXT NOT NULL, changes_json TEXT NOT NULL)"""
_PINS_DDL = """CREATE TABLE IF NOT EXISTS workspace_canonical_release (
    migration_id TEXT PRIMARY KEY, release_no INTEGER NOT NULL, adopted_by TEXT NOT NULL, adopted_utc TEXT NOT NULL)"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    conn.execute(_RELEASES_DDL)
    conn.execute(_PINS_DDL)
    if "release_no" not in table_columns(conn, "canonical_model_overlay"):
        conn.execute("ALTER TABLE canonical_model_overlay ADD COLUMN release_no INTEGER")
        # Changes applied before releases existed were live in every workspace: keep it that way as release 1.
        old = conn.execute("SELECT request_id, entity_name, canonical_field, change_type FROM canonical_model_overlay ORDER BY applied_utc").fetchall()
        if old:
            changes = [{"entity": r["entity_name"], "field": r["canonical_field"], "change_type": r["change_type"], "request_id": r["request_id"]} for r in old]
            conn.execute("INSERT INTO canonical_releases (release_no, released_by, released_utc, notes, changes_json) VALUES (1, 'system', ?, ?, ?)",
                         (datetime.now(timezone.utc).isoformat(), "Changes applied before canonical releases existed.", json.dumps(changes)))
            conn.execute("UPDATE canonical_model_overlay SET release_no = 1")


def latest_release(conn: sqlite3.Connection) -> int:
    ensure_table(conn)
    row = conn.execute("SELECT MAX(release_no) AS n FROM canonical_releases").fetchone()
    return int(row["n"] or 0)


def pinned_release(conn: sqlite3.Connection, migration_id: str) -> int:
    """The release a workspace is on. A workspace seen for the first time is pinned to the latest release."""
    ensure_table(conn)
    row = conn.execute("SELECT release_no FROM workspace_canonical_release WHERE migration_id = ?", (migration_id,)).fetchone()
    if row:
        return int(row["release_no"])
    n = latest_release(conn)
    conn.execute("INSERT INTO workspace_canonical_release (migration_id, release_no, adopted_by, adopted_utc) VALUES (?, ?, 'system', ?)",
                 (migration_id, n, datetime.now(timezone.utc).isoformat()))
    return n


def unreleased(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute(
        "SELECT request_id, migration_id, entity_name, canonical_field, change_type, applied_by, applied_utc FROM canonical_model_overlay "
        "WHERE release_no IS NULL ORDER BY applied_utc").fetchall()]


def releases(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [{**dict(r), "changes": json.loads(r["changes_json"])} for r in conn.execute("SELECT * FROM canonical_releases ORDER BY release_no DESC").fetchall()]


def publish_release(conn: sqlite3.Connection, actor: str, notes: str) -> Dict[str, Any]:
    """Bundle every applied, unreleased change into the next release. Workspaces see a notice; none moves until it adopts."""
    pending = unreleased(conn)
    if not pending:
        raise ValueError("There are no applied changes waiting for a release.")
    notes = " ".join(str(notes or "").split())[:500]
    if not notes:
        raise ValueError("Release notes are required.")
    n = latest_release(conn) + 1
    changes = [{"entity": p["entity_name"], "field": p["canonical_field"], "change_type": p["change_type"], "request_id": p["request_id"]} for p in pending]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO canonical_releases (release_no, released_by, released_utc, notes, changes_json) VALUES (?, ?, ?, ?, ?)",
                 (n, actor, now, notes, json.dumps(changes)))
    conn.execute("UPDATE canonical_model_overlay SET release_no = ? WHERE release_no IS NULL", (n,))
    return {"release_no": n, "released_by": actor, "released_utc": now, "notes": notes, "changes": changes}


def notice(conn: sqlite3.Connection, migration_id: str) -> Optional[Dict[str, Any]]:
    """What a workspace has not adopted yet: None when it is on the latest release."""
    pinned, latest = pinned_release(conn, migration_id), latest_release(conn)
    if latest <= pinned:
        return None
    newer = [r for r in releases(conn) if r["release_no"] > pinned]
    return {"pinned": pinned, "latest": latest, "releases": sorted(newer, key=lambda r: r["release_no"])}


def adopt_latest(conn: sqlite3.Connection, migration_id: str, actor: str) -> int:
    pinned, latest = pinned_release(conn, migration_id), latest_release(conn)
    if latest <= pinned:
        raise ValueError(f"This workspace is already on canonical release {pinned}.")
    conn.execute("UPDATE workspace_canonical_release SET release_no = ?, adopted_by = ?, adopted_utc = ? WHERE migration_id = ?",
                 (latest, actor, datetime.now(timezone.utc).isoformat(), migration_id))
    return latest


def active_added_fields(entity: str, migration_id: Optional[str] = None) -> List[str]:
    """Canonical fields added through approved ADD_FIELD requests that the workspace may map to: those in a release it has
    adopted, plus its own applied changes. With no workspace at all, every applied field (empty if unavailable)."""
    migration_id = migration_id if migration_id is not None else ACTIVE_MIGRATION()
    try:
        conn = get_db_connection()
        try:
            ensure_table(conn)
            if migration_id:
                pin = pinned_release(conn, str(migration_id))
                conn.commit()
                rows = conn.execute(
                    "SELECT canonical_field FROM canonical_model_overlay WHERE entity_name = ? AND change_type = 'ADD_FIELD' "
                    "AND ((release_no IS NOT NULL AND release_no <= ?) OR migration_id = ?) ORDER BY applied_utc", (entity, pin, str(migration_id))).fetchall()
            else:
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
        if field in existing_fields or field in active_added_fields(entity, migration_id=""):
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
