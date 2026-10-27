"""Canonical-to-target mapping (forward direction only; P0 entities; demo target spec).

Proposals are generated deterministically from the target spec's canonical hints, then a human
approves or rejects each one. Approved mappings may use an approved governed transformation rule.
Preview is read-only and returns counts and findings, never record values. Nothing is loaded
into any target system.
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from control_plane.services import transformation_rules

ENTITIES = ("Account", "Client", "Household")
SPEC_PATH = Path(__file__).resolve().parents[2] / "platform_knowledge" / "simplicity_target_demo.yaml"

_DDL = """
CREATE TABLE IF NOT EXISTS target_mappings (
    target_mapping_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    target_field TEXT NOT NULL,
    canonical_field TEXT,
    transformation_expression TEXT NOT NULL DEFAULT 'DIRECT_COPY',
    status TEXT NOT NULL DEFAULT 'PROPOSED',
    reviewer TEXT,
    review_comments TEXT,
    reviewed_utc TEXT,
    spec_version TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    UNIQUE (migration_id, entity_name, target_field)
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def load_spec(path: Optional[Path] = None) -> Dict[str, Any]:
    spec = yaml.safe_load(Path(path or SPEC_PATH).read_text(encoding="utf-8"))
    entities = spec.get("entities") or {}
    if set(entities) - set(ENTITIES):
        raise ValueError("Target spec may only define Account, Client and Household in P0.")
    return spec


def propose(conn: sqlite3.Connection, migration_id: str, canonical_fields: Dict[str, List[str]],
            spec: Optional[Dict[str, Any]] = None) -> int:
    """Create PROPOSED rows for target fields not yet proposed. Idempotent. Returns rows created."""
    ensure_table(conn)
    spec = spec or load_spec()
    now = datetime.now(timezone.utc).isoformat()
    created = 0
    for entity, body in spec["entities"].items():
        known = set(canonical_fields.get(entity, []))
        for field in body["fields"]:
            hint = field.get("canonical_hint")
            canonical = hint if hint in known else None  # never guess when the canonical field does not exist
            cur = conn.execute(
                "INSERT OR IGNORE INTO target_mappings (target_mapping_id, migration_id, entity_name, target_field, canonical_field, "
                "transformation_expression, status, spec_version, created_utc) VALUES (?, ?, ?, ?, ?, 'DIRECT_COPY', 'PROPOSED', ?, ?)",
                ("TGT-" + uuid.uuid4().hex[:12].upper(), migration_id, entity, field["name"], canonical, str(spec["spec_version"]), now))
            created += cur.rowcount
    return created


def list_mappings(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    ensure_table(conn)
    rows = conn.execute("SELECT * FROM target_mappings WHERE migration_id = ? ORDER BY entity_name, target_field", (migration_id,)).fetchall()
    return [dict(r) for r in rows]


def review(conn: sqlite3.Connection, target_mapping_id: str, decision: str, reviewer: str,
           comments: str, rule_override: str = "") -> Dict[str, Any]:
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    ensure_table(conn)
    row = conn.execute("SELECT * FROM target_mappings WHERE target_mapping_id = ?", (target_mapping_id,)).fetchone()
    if row is None:
        raise LookupError("Target mapping not found.")
    if row["status"] != "PROPOSED":
        raise ValueError(f"Target mapping is already {row['status']}.")
    decision = decision.strip().upper()
    if decision not in {"APPROVED", "REJECTED"}:
        raise ValueError("Decision must be APPROVED or REJECTED.")
    if decision == "REJECTED" and not comments.strip():
        raise ValueError("A rejection reason is required.")
    expression = row["transformation_expression"]
    if decision == "APPROVED":
        if not row["canonical_field"]:
            raise ValueError("No canonical source field is proposed for this target field; it cannot be approved.")
        override = rule_override.strip().upper()
        if override:
            if not SchemaMapperAgent.supports_transformation(override):
                raise ValueError(f"'{override}' is not an approved, registered transformation.")
            expression = override
    conn.execute(
        "UPDATE target_mappings SET status = ?, reviewer = ?, review_comments = ?, reviewed_utc = ?, transformation_expression = ? "
        "WHERE target_mapping_id = ? AND status = 'PROPOSED'",
        (decision, reviewer, comments.strip(), datetime.now(timezone.utc).isoformat(), expression, target_mapping_id))
    return dict(conn.execute("SELECT * FROM target_mappings WHERE target_mapping_id = ?", (target_mapping_id,)).fetchone())


def preview(spec: Dict[str, Any], approved: List[Dict[str, Any]], canonical_rows: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Read-only dry run over canonical rows. Returns coverage, blank counts and rule warnings; no values."""
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    out: Dict[str, Any] = {}
    for entity, body in spec["entities"].items():
        rows = canonical_rows.get(entity, [])
        by_target = {m["target_field"]: m for m in approved if m["entity_name"] == entity and m["status"] == "APPROVED"}
        rule_cache: Dict[str, Any] = {}
        warnings: Dict[str, int] = {}
        fields = []
        for field in body["fields"]:
            m = by_target.get(field["name"])
            blanks = 0
            if m:
                expr = m["transformation_expression"]
                rule = rule_cache.get(expr) or transformation_rules.approved_rule_for_expression(expr)
                rule_cache[expr] = rule
                for row in rows:
                    value = row.get(m["canonical_field"])
                    if rule:
                        value, warning = transformation_rules.apply_rule(rule["rule_type"], rule["params"], value, row)
                        if warning:
                            note = f"{m['target_field']} ({expr}): {warning}"
                            warnings[note] = warnings.get(note, 0) + 1
                    elif SchemaMapperAgent.supports_transformation(expr):
                        value = SchemaMapperAgent._apply_approved_transformation(value, expr)
                    if value is None or str(value).strip() == "":
                        blanks += 1
            fields.append({"target_field": field["name"], "required": bool(field.get("required")), "type": field.get("type"),
                           "canonical_field": m["canonical_field"] if m else None, "approved": bool(m),
                           "rule": m["transformation_expression"] if m else None, "blank_values": blanks if m else None})
        out[entity] = {
            "target_table": body.get("target_table"), "row_count": len(rows), "fields": fields,
            "uncovered_required": [f["target_field"] for f in fields if f["required"] and not f["approved"]],
            "blank_required": [f["target_field"] for f in fields if f["required"] and f["approved"] and f["blank_values"]],
            "warnings": [f"{note} ({n} value{'s' if n != 1 else ''})" for note, n in sorted(warnings.items())][:10],
        }
    return out
