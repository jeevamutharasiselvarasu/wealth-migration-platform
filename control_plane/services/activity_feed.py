"""Recent activity for the active migration: who did what, when.

Built from the durable workflow event log plus validation runs and canonical change
requests. Only whitelisted, display-safe payload fields are used (never stored paths
or file contents).
"""
import json
import sqlite3
from typing import Any, Dict, List

from control_plane.db import get_db_connection

_ICONS = {"file": "📄", "check": "✓", "map": "⇄", "ask": "?", "run": "▶", "change": "✎", "setup": "⚙", "doc": "📘"}


def _describe(event_type: str, payload: Dict[str, Any]) -> Dict[str, str]:
    g = payload.get
    if event_type == "SOURCE_FILE_UPLOADED":
        return {"icon": "file", "title": "Source file added", "detail": str(g("filename", ""))}
    if event_type == "ENTITY_INFERENCE_REVIEWED":
        verb = "confirmed" if str(g("decision", "")).upper().startswith("APPROVE") else "rejected"
        return {"icon": "check", "title": f"Entity {verb}", "detail": f"{g('filename', '')} → {g('entity', '')}"}
    if event_type == "MAPPING_PROPOSALS_GENERATED":
        return {"icon": "map", "title": "Mapping proposals generated", "detail": f"{g('source_file', '')} · {g('proposal_count', 0)} proposal(s)"}
    if event_type == "MAPPING_REVIEWED":
        decision = str(g("decision", "")).lower()
        return {"icon": "map", "title": f"Mapping {decision}", "detail": f"{g('entity', '')}: {g('source_field', '')} → {g('canonical_field', '')}"}
    if event_type == "ENTITY_MAPPINGS_APPROVED":
        return {"icon": "check", "title": "Entity mappings approved", "detail": f"{g('approved_count', 0)} mapping(s)"}
    if event_type == "MAPPING_INFORMATION_REQUEST_CHANGED":
        return {"icon": "ask", "title": "Information request " + str(g("status", "updated")).lower(), "detail": f"{g('entity', '')}.{g('canonical_field', '')}"}
    if event_type == "KNOWLEDGE_ASSET_REGISTERED":
        return {"icon": "doc", "title": "Knowledge asset registered", "detail": str(g("filename", ""))}
    if event_type == "MIGRATION_CONTEXT_UPDATED":
        return {"icon": "setup", "title": "Migration setup saved", "detail": str(g("migration_name", ""))}
    if event_type == "ENTITY_SCOPES_UPDATED":
        return {"icon": "setup", "title": "Entity scope updated", "detail": ""}
    return {"icon": "run", "title": event_type.replace("_", " ").capitalize(), "detail": ""}


def recent_activity(migration_id: str, limit: int = 8) -> List[Dict[str, str]]:
    items: List[Dict[str, Any]] = []
    try:
        conn = get_db_connection()
        try:
            for row in conn.execute(
                    "SELECT event_type, payload_json, actor, occurred_utc FROM workflow_events WHERE migration_id = ? "
                    "ORDER BY occurred_utc DESC, event_id DESC LIMIT 200", (migration_id,)).fetchall():
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except ValueError:
                    payload = {}
                items.append({**_describe(row["event_type"], payload if isinstance(payload, dict) else {}),
                              "kind": row["event_type"], "actor": row["actor"] or "", "when": str(row["occurred_utc"])})
            for row in conn.execute(
                    "SELECT run_id, status, executed_by, executed_utc FROM validation_runs WHERE migration_id = ?", (migration_id,)).fetchall():
                items.append({"icon": "run", "title": "Validation run", "detail": f"{row['run_id']} · {row['status']}",
                              "kind": "VALIDATION_RUN", "actor": row["executed_by"], "when": str(row["executed_utc"])})
            for row in conn.execute(
                    "SELECT request_id, entity_name, canonical_field, status, requested_by, created_utc, decided_by, decided_utc "
                    "FROM canonical_change_requests WHERE migration_id = ?", (migration_id,)).fetchall():
                items.append({"icon": "change", "title": "Canonical change requested", "detail": f"{row['entity_name']}.{row['canonical_field']}",
                              "kind": "CCR_RAISED", "actor": row["requested_by"], "when": str(row["created_utc"])})
                if row["decided_utc"]:
                    items.append({"icon": "change", "title": f"Canonical change {str(row['status']).lower()}",
                                  "detail": f"{row['entity_name']}.{row['canonical_field']}", "kind": "CCR_DECIDED",
                                  "actor": row["decided_by"] or "", "when": str(row["decided_utc"])})
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    items.sort(key=lambda i: i["when"], reverse=True)
    collapsed: List[Dict[str, Any]] = []
    for item in items:
        last = collapsed[-1] if collapsed else None
        if last and last["kind"] == item["kind"] and last["actor"] == item["actor"] and last["when"][:16] == item["when"][:16]:
            last["count"] += 1
            continue
        collapsed.append({**item, "count": 1})
    out = []
    for item in collapsed[:limit]:
        title = item["title"] + (f" ×{item['count']}" if item["count"] > 1 else "")
        out.append({"icon": _ICONS.get(item["icon"], "•"), "title": title, "detail": item["detail"] if item["count"] == 1 else "",
                    "actor": item["actor"], "when": item["when"][:16].replace("T", " ")})
    return out
