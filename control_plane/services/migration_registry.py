"""Registry of migrations, derived from the durable workflow event log (no new tables).

Every saved setup publishes MIGRATION_CONTEXT_UPDATED with the migration_setup it saved, so the earlier migrations and
their configuration can be listed and reused. A migration used as a TEMPLATE contributes its configuration (target,
contract, scope, knowledge references) and the work of stages 01 to 03: the loaded source files, the entity decisions on them,
the knowledge assets and the approved code sets (business rules are platform-wide, so they are shared already). Everything
from stage 04 on (mappings, match rules, validation, runs) is never copied: it is mapped and approved by people from scratch.
A migration started without a template is empty.
"""
import json
from typing import Any, Dict, List

# Configuration that is the same for the next migration into the same target.
CARRIED_FIELDS = ("target_system", "canonical_version", "target_owner", "entities", "target_entities", "knowledge_schema_ref", "knowledge_rules_ref",
                  "knowledge_sme", "relationship_notes", "source_acquisition")


def _events(conn) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT migration_id, payload_json, actor, occurred_utc FROM workflow_events "
                        "WHERE event_type = 'MIGRATION_CONTEXT_UPDATED' ORDER BY occurred_utc ASC, event_id ASC").fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except ValueError:
            payload = {}
        out.append({"id": str(r["migration_id"]), "payload": payload if isinstance(payload, dict) else {}, "actor": r["actor"] or "", "when": str(r["occurred_utc"])})
    return out


def archived_ids(conn) -> set:
    """Workspaces the firm retired from the list. The event log keeps every record; archiving only hides the workspace."""
    rows = conn.execute("SELECT DISTINCT migration_id FROM workflow_events WHERE event_type = 'MIGRATION_WORKSPACE_ARCHIVED'").fetchall()
    return {str(r["migration_id"]) for r in rows}


def list_migrations(conn, active_id: str = "") -> List[Dict[str, Any]]:
    """Newest first. One row per migration, taken from its latest saved setup. Archived workspaces are left out."""
    found: Dict[str, Dict[str, Any]] = {}
    hidden = archived_ids(conn) - {active_id}
    for ev in _events(conn):
        mid = ev["id"]
        if not mid or mid == "MIG-UNCONFIGURED" or mid in hidden:
            continue
        row = found.setdefault(mid, {"id": mid, "created_utc": ev["when"], "created_by": ev["actor"], "setup": {}, "name": ""})
        setup = ev["payload"].get("migration_setup") or {}
        if setup:
            row["setup"] = setup
            row["name"] = str(setup.get("migration_name") or ev["payload"].get("migration_name") or "")
            row["created_by"] = row["created_by"] or ev["actor"]
    approved = {r["migration_id"]: r["n"] for r in conn.execute(
        "SELECT migration_id, COUNT(*) AS n FROM canonical_mappings WHERE is_confirmed = 1 GROUP BY migration_id").fetchall()}
    files = {r["migration_id"]: r["n"] for r in conn.execute(
        "SELECT migration_id, COUNT(*) AS n FROM workflow_events WHERE event_type = 'SOURCE_FILE_UPLOADED' GROUP BY migration_id").fetchall()}
    out = []
    for mid, row in found.items():
        s = row["setup"]
        if not s and mid != active_id:
            continue  # started but never configured: nothing to show or reuse
        out.append({"id": mid, "name": row["name"] or "Unnamed migration", "source_firm": s.get("source_firm", ""), "target_system": s.get("target_system", ""),
                    "entities": list(s.get("entities") or []), "created_utc": row["created_utc"], "created_by": row["created_by"],
                    "template_id": s.get("template_migration_id", ""), "configured": bool(s), "active": mid == active_id,
                    "approved_mappings": int(approved.get(mid, 0)), "files": int(files.get(mid, 0))})
    out.sort(key=lambda m: m["created_utc"], reverse=True)
    out.sort(key=lambda m: not m["active"])  # the active workspace first, then newest to oldest
    names = {m["id"]: m["name"] for m in out}
    for m in out:
        m["template_name"] = names.get(m["template_id"], "")  # an earlier workspace that was deleted is not named
    return out


def carry_over(conn, migration_id: str) -> Dict[str, Any]:
    """Configuration to pre-fill a new migration from an earlier one. Empty when the migration is unknown or never configured."""
    setup: Dict[str, Any] = {}
    for ev in _events(conn):
        if ev["id"] == migration_id and ev["payload"].get("migration_setup"):
            setup = ev["payload"]["migration_setup"]
    if not setup:
        return {}
    carried = {k: setup[k] for k in CARRIED_FIELDS if k in setup}
    carried["template_migration_id"] = migration_id
    return carried


def latest_context(conn, migration_id: str) -> Dict[str, Any]:
    """The migration's context as last saved (later events override earlier ones), or {} when it is unknown or never configured."""
    context: Dict[str, Any] = {}
    for ev in _events(conn):
        if ev["id"] == migration_id:
            context.update(ev["payload"])
    return context if context.get("migration_setup") else {}


# Event types that make up stages 01 to 03 of a migration; replayed under the new migration id.
FOUNDATION_EVENTS = ("SOURCE_FILE_UPLOADED", "ENTITY_INFERENCE_REVIEWED", "KNOWLEDGE_ASSET_REGISTERED")


def copy_foundation(conn, from_id: str, to_id: str, actor: str) -> Dict[str, int]:
    """Copy stages 01 to 03 of `from_id` into `to_id`: files, entity decisions, knowledge assets and code sets (scope is set when the new workspace configuration is saved).
    Source files are shared by their stored location (never rewritten). Returns counts for the confirmation message."""
    from control_plane.services.workflow_event_engine import publish_event, read_events

    counts = {"events": 0, "code_values": 0}
    for ev in read_events(from_id, limit=5000):
        if ev["event_type"] not in FOUNDATION_EVENTS:
            continue
        payload = dict(ev.get("payload") or {})
        if "migration_id" in payload:
            payload["migration_id"] = to_id
        publish_event(ev["event_type"], to_id, ev["aggregate_type"], ev["aggregate_id"], payload, ev.get("actor") or actor)
        counts["events"] += 1
    from control_plane.services import code_mappings
    code_mappings.ensure_tables(conn)
    rows = conn.execute("SELECT code_set, source_value, canonical_code, status, reason, fields, row_count, proposed_utc, decided_by, decided_utc "
                        "FROM code_mappings WHERE migration_id = ?", (from_id,)).fetchall()
    for r in rows:
        conn.execute("INSERT INTO code_mappings (migration_id, code_set, source_value, canonical_code, status, reason, fields, row_count, "
                     "proposed_utc, decided_by, decided_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (to_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]))
        counts["code_values"] += 1
    conn.commit()
    return counts
