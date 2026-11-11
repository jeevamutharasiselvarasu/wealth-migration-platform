"""Target adapter: canonical -> target mappings reviewed once per target system by the Platform Admin and reused by every migration.

The admin drafts the adapter (from the target model's own hints, or from one migration's approved target mappings), decides each
field, and confirms a version. A confirmed version is immutable. A migration adopts the latest confirmed version on stage 06: every
field the adapter maps becomes an approved target mapping of that migration (recorded as coming from the adapter), and the migration
reviews only what the adapter leaves open. Canonical model v2 field names are the same for every source, so the adapter does not
depend on any source.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import target_mapping

_DDL = (
    """CREATE TABLE IF NOT EXISTS target_adapter_fields (
        target_system TEXT NOT NULL, entity_name TEXT NOT NULL, target_field TEXT NOT NULL, canonical_field TEXT,
        transformation_expression TEXT NOT NULL DEFAULT 'DIRECT_COPY', status TEXT NOT NULL DEFAULT 'PROPOSED', origin TEXT NOT NULL,
        reviewer TEXT, reviewed_utc TEXT, PRIMARY KEY (target_system, entity_name, target_field)
    )""",
    """CREATE TABLE IF NOT EXISTS target_adapter_versions (
        target_system TEXT NOT NULL, version INTEGER NOT NULL, spec_version TEXT NOT NULL, fields_json TEXT NOT NULL,
        confirmed_by TEXT NOT NULL, confirmed_utc TEXT NOT NULL, notes TEXT, PRIMARY KEY (target_system, version)
    )""",
    """CREATE TABLE IF NOT EXISTS target_adapter_adoptions (
        migration_id TEXT NOT NULL, target_system TEXT NOT NULL, version INTEGER NOT NULL, adopted_by TEXT NOT NULL, adopted_utc TEXT NOT NULL,
        applied INTEGER NOT NULL, PRIMARY KEY (migration_id, target_system, version)
    )""",
)
STATUSES = ("PROPOSED", "APPROVED", "REJECTED")


def ensure_tables(conn: sqlite3.Connection) -> None:
    for ddl in _DDL:
        conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fields(conn: sqlite3.Connection, target_system: str) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM target_adapter_fields WHERE target_system = ? ORDER BY entity_name, target_field",
                                          (target_system,)).fetchall()]


def draft(conn: sqlite3.Connection, spec: Dict[str, Any], canonical_fields: Dict[str, List[str]],
          from_mappings: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
    """Add a PROPOSED row for every target field the adapter does not hold yet. With from_mappings (one migration's target mappings),
    the canonical field and rule of each approved mapping are proposed; otherwise the target model's own hint, when that canonical
    field exists. Decided rows are never touched. Returns {"added", "with_source"}."""
    ensure_tables(conn)
    system = spec["target_system"]
    seed = {(m["entity_name"], m["target_field"]): m for m in (from_mappings or []) if m.get("status") in ("APPROVED", "REJECTED")}
    added = with_source = 0
    for entity, body in spec["entities"].items():
        known = set(canonical_fields.get(entity, []))
        for f in body["fields"]:
            if conn.execute("SELECT 1 FROM target_adapter_fields WHERE target_system = ? AND entity_name = ? AND target_field = ?",
                            (system, entity, f["name"])).fetchone():
                continue
            m = seed.get((entity, f["name"]))
            if m and m["status"] == "APPROVED" and m.get("canonical_field"):
                canonical, expr, origin = m["canonical_field"], m["transformation_expression"] or "DIRECT_COPY", f"migration {m.get('migration_id', '')}".strip()
            elif m:  # rejected in that migration: never fall back to the target model's hint for it
                canonical, expr, origin = None, "DIRECT_COPY", f"rejected in migration {m.get('migration_id', '')}".strip()
            else:
                hint = target_mapping._source(f)
                canonical, expr, origin = (hint if hint in known else None), "DIRECT_COPY", "target model hint"
            conn.execute("INSERT INTO target_adapter_fields (target_system, entity_name, target_field, canonical_field, transformation_expression, status, origin) "
                         "VALUES (?, ?, ?, ?, ?, 'PROPOSED', ?)", (system, entity, f["name"], canonical, expr, origin))
            added += 1
            with_source += 1 if canonical else 0
    return {"added": added, "with_source": with_source}


def decide(conn: sqlite3.Connection, target_system: str, entity: str, target_field: str, decision: str, actor: str,
           canonical_field: str = "", known_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """APPROVE (optionally with another canonical field of the entity), REJECT (the adapter leaves this field to each migration) or REOPEN."""
    ensure_tables(conn)
    row = conn.execute("SELECT * FROM target_adapter_fields WHERE target_system = ? AND entity_name = ? AND target_field = ?",
                       (target_system, entity, target_field)).fetchone()
    if not row:
        raise ValueError("That target field is not in the adapter draft.")
    decision = str(decision or "").upper()
    canonical = str(canonical_field or "").strip().upper() or row["canonical_field"]
    if decision == "APPROVE":
        if not canonical:
            raise ValueError(f"Choose the canonical field for {target_field}.")
        if known_fields is not None and canonical not in known_fields:
            raise ValueError(f"'{canonical}' is not a canonical {entity} field.")
        status = "APPROVED"
    elif decision == "REJECT":
        status = "REJECTED"
    elif decision == "REOPEN":
        status = "PROPOSED"
    else:
        raise ValueError("Unknown decision.")
    conn.execute("UPDATE target_adapter_fields SET status = ?, canonical_field = ?, reviewer = ?, reviewed_utc = ? "
                 "WHERE target_system = ? AND entity_name = ? AND target_field = ?",
                 (status, canonical, None if status == "PROPOSED" else actor, None if status == "PROPOSED" else _now(), target_system, entity, target_field))
    return {**dict(row), "status": status, "canonical_field": canonical}


def counts(conn: sqlite3.Connection, target_system: str) -> Dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for r in fields(conn, target_system):
        out[r["status"]] += 1
    return out


def confirm(conn: sqlite3.Connection, spec: Dict[str, Any], actor: str, notes: str = "") -> Dict[str, Any]:
    """Snapshot the approved fields as the next immutable version. Every field must be decided first."""
    ensure_tables(conn)
    system = spec["target_system"]
    rows = fields(conn, system)
    if not rows:
        raise ValueError("Draft the adapter first.")
    pending = [r["target_field"] for r in rows if r["status"] == "PROPOSED"]
    if pending:
        raise ValueError(f"{len(pending)} field(s) are still waiting for a decision.")
    approved = [{k: r[k] for k in ("entity_name", "target_field", "canonical_field", "transformation_expression")} for r in rows if r["status"] == "APPROVED"]
    if not approved:
        raise ValueError("Approve at least one field before confirming the adapter.")
    last = latest(conn, system)
    if last and last["fields"] == approved and last["spec_version"] == str(spec["spec_version"]):
        raise ValueError(f"Nothing changed since version {last['version']}.")
    version = (last["version"] if last else 0) + 1
    conn.execute("INSERT INTO target_adapter_versions (target_system, version, spec_version, fields_json, confirmed_by, confirmed_utc, notes) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (system, version, str(spec["spec_version"]), json.dumps(approved), actor, _now(), str(notes or "")[:500]))
    return latest(conn, system)


def latest(conn: sqlite3.Connection, target_system: str) -> Optional[Dict[str, Any]]:
    ensure_tables(conn)
    r = conn.execute("SELECT * FROM target_adapter_versions WHERE target_system = ? ORDER BY version DESC LIMIT 1", (target_system,)).fetchone()
    if not r:
        return None
    out = dict(r)
    out["fields"] = json.loads(out.pop("fields_json"))
    return out


def versions(conn: sqlite3.Connection, target_system: str) -> List[Dict[str, Any]]:
    ensure_tables(conn)
    return [{**dict(r), "fields": len(json.loads(r["fields_json"]))} for r in conn.execute(
        "SELECT target_system, version, spec_version, fields_json, confirmed_by, confirmed_utc, notes FROM target_adapter_versions "
        "WHERE target_system = ? ORDER BY version DESC", (target_system,)).fetchall()]


def adopted(conn: sqlite3.Connection, migration_id: str, target_system: str) -> Optional[Dict[str, Any]]:
    ensure_tables(conn)
    r = conn.execute("SELECT * FROM target_adapter_adoptions WHERE migration_id = ? AND target_system = ? ORDER BY version DESC LIMIT 1",
                     (migration_id, target_system)).fetchone()
    return dict(r) if r else None


def adopt(conn: sqlite3.Connection, migration_id: str, spec: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Apply the latest confirmed adapter to a migration's target mappings. A field the adapter maps becomes APPROVED with the adapter's
    canonical field and rule (a decided mapping that differs is reopened first, its decision kept in the history). Fields the adapter
    does not map are left as they are, for the migration to review. Returns {"version", "applied", "unchanged", "open"}."""
    system = spec["target_system"]
    ver = latest(conn, system)
    if not ver:
        raise ValueError("No adapter is confirmed for this target system yet. The Platform Admin confirms it under Platform setup → Target adapter.")
    if ver["spec_version"] != str(spec["spec_version"]):
        raise ValueError(f"Adapter version {ver['version']} was confirmed against target model {ver['spec_version']}; the target model is now "
                         f"{spec['spec_version']}. The Platform Admin confirms a new adapter version first.")
    target_mapping.propose(conn, migration_id, {}, spec)  # make sure every target field has a row
    rows = {(m["entity_name"], m["target_field"]): m for m in target_mapping.list_mappings(conn, migration_id, spec)}
    applied = unchanged = 0
    note = f"From target adapter version {ver['version']} ({system})."
    for f in ver["fields"]:
        m = rows.get((f["entity_name"], f["target_field"]))
        if not m:
            continue
        if m["status"] == "APPROVED" and m["canonical_field"] == f["canonical_field"] and m["transformation_expression"] == f["transformation_expression"]:
            unchanged += 1
            continue
        if m["status"] in ("APPROVED", "REJECTED"):
            target_mapping.revise(conn, m["target_mapping_id"], actor, f"Replaced by target adapter version {ver['version']}.")
        conn.execute("UPDATE target_mappings SET status = 'APPROVED', canonical_field = ?, transformation_expression = ?, reviewer = ?, "
                     "review_comments = ?, reviewed_utc = ? WHERE target_mapping_id = ?",
                     (f["canonical_field"], f["transformation_expression"], actor, note, _now(), m["target_mapping_id"]))
        applied += 1
    mapped = {(f["entity_name"], f["target_field"]) for f in ver["fields"]}
    still_open = sum(1 for k, m in rows.items() if k not in mapped and m["status"] == "PROPOSED")
    conn.execute("DELETE FROM target_adapter_adoptions WHERE migration_id = ? AND target_system = ? AND version = ?", (migration_id, system, ver["version"]))
    conn.execute("INSERT INTO target_adapter_adoptions (migration_id, target_system, version, adopted_by, adopted_utc, applied) VALUES (?, ?, ?, ?, ?, ?)",
                 (migration_id, system, ver["version"], actor, _now(), applied))
    return {"version": ver["version"], "applied": applied, "unchanged": unchanged, "open": still_open}
