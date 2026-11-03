"""Registry of migrations, derived from the durable workflow event log (no new tables).

Every saved setup publishes MIGRATION_CONTEXT_UPDATED with the migration_setup it saved, so the earlier migrations and
their configuration can be listed and reused. A migration used as a TEMPLATE contributes only configuration (target,
contract, scope, knowledge references). Mappings, approvals, files and rule decisions are never copied: a new migration
from a different firm is mapped and approved by people from scratch.
"""
import json
from typing import Any, Dict, List

# Configuration that is the same for the next migration into the same target.
CARRIED_FIELDS = ("target_system", "canonical_version", "target_owner", "entities", "knowledge_schema_ref", "knowledge_rules_ref",
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


def list_migrations(conn, active_id: str = "") -> List[Dict[str, Any]]:
    """Newest first. One row per migration, taken from its latest saved setup."""
    found: Dict[str, Dict[str, Any]] = {}
    for ev in _events(conn):
        mid = ev["id"]
        if not mid or mid == "MIG-UNCONFIGURED":
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
        m["template_name"] = names.get(m["template_id"], m["template_id"])
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
