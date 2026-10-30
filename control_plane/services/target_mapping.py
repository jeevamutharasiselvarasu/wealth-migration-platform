"""Canonical-to-target mapping (forward direction only; P0 entities).

Proposals are generated deterministically from the confirmed, inferred target model's canonical hints, then a human
approves or rejects each one. Approved mappings may use an approved governed transformation rule.
Preview is read-only and returns counts and findings, never record values. Nothing is loaded
into any target system.
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import transformation_rules

ENTITIES = ("Account", "Client", "Household")

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


def _source(field: Dict[str, Any]) -> Optional[str]:
    """Canonical field a target field draws from: the inferred match, else the name a change request would add."""
    return field.get("canonical_hint") or field.get("suggested_canonical_field")


def gap_report(spec: Dict[str, Any], canonical_fields: Dict[str, List[str]]) -> Dict[str, Any]:
    """Compare the target with the canonical model (deterministic; no record data).

    missing: target fields whose canonical source does not exist yet (candidates for a canonical change
    request). unused: canonical fields no target field draws from. code_lists: fields where the target
    enforces its own code list, which a governed value-map rule must translate.
    """
    missing, unused, code_lists = [], [], []
    for entity, body in spec["entities"].items():
        known = list(canonical_fields.get(entity, []))
        hints = {_source(f) for f in body["fields"] if _source(f)}
        for f in body["fields"]:
            if _source(f) not in known:
                missing.append({"entity": entity, "target_field": f["name"], "required": f["required"], "type": f["type"],
                                "suggested_canonical_field": _source(f)})
            if f.get("code_list"):
                code_lists.append({"entity": entity, "target_field": f["name"], "canonical_field": _source(f), "codes": f["codes"]})
        unused.extend({"entity": entity, "canonical_field": c} for c in known if c not in hints)
    return {"missing": missing, "unused": unused, "code_lists": code_lists}


def coverage(spec: Optional[Dict[str, Any]], mappings: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-entity target coverage split by how each field is populated. Only APPROVED mappings count.

    direct: approved DIRECT_COPY. transformed: approved with a governed rule. effective = direct + transformed.
    pending: still PROPOSED (never counted). unmapped: no approved mapping. Required fields are counted separately.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for entity, body in (spec or {"entities": {}})["entities"].items():
        by_field = {m["target_field"]: m for m in mappings if m["entity_name"] == entity}
        row = {"total": 0, "direct": 0, "transformed": 0, "pending": 0, "unmapped": 0, "required_total": 0, "required_effective": 0}
        for f in body["fields"]:
            m = by_field.get(f["name"])
            state = "unmapped"
            if m and m["status"] == "APPROVED":
                state = "direct" if m["transformation_expression"] == "DIRECT_COPY" else "transformed"
            elif m and m["status"] == "PROPOSED":
                state = "pending"
            row["total"] += 1
            row[state] += 1
            if f.get("required"):
                row["required_total"] += 1
                row["required_effective"] += state in ("direct", "transformed")
        row["effective"] = row["direct"] + row["transformed"]
        row["effective_pct"] = round(100 * row["effective"] / row["total"]) if row["total"] else 0
        out[entity] = row
    return out


def propose(conn: sqlite3.Connection, migration_id: str, canonical_fields: Dict[str, List[str]],
            spec: Dict[str, Any]) -> int:
    """Create PROPOSED rows for target fields not yet proposed. Idempotent. Returns rows created."""
    ensure_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    created = blocked = 0
    for entity, body in spec["entities"].items():
        known = set(canonical_fields.get(entity, []))
        for field in body["fields"]:
            hint = _source(field)
            canonical = hint if hint in known else None  # never guess when the canonical field does not exist
            cur = conn.execute(
                "INSERT OR IGNORE INTO target_mappings (target_mapping_id, migration_id, entity_name, target_field, canonical_field, "
                "transformation_expression, status, spec_version, created_utc) VALUES (?, ?, ?, ?, ?, 'DIRECT_COPY', 'PROPOSED', ?, ?)",
                ("TGT-" + uuid.uuid4().hex[:12].upper(), migration_id, entity, field["name"], canonical, str(spec["spec_version"]), now))
            created += cur.rowcount
            if not cur.rowcount and not conn.execute(
                    "SELECT 1 FROM target_mappings WHERE migration_id = ? AND entity_name = ? AND target_field = ? AND spec_version = ?",
                    (migration_id, entity, field["name"], str(spec["spec_version"]))).fetchone():
                blocked += 1
            if canonical and not cur.rowcount:
                conn.execute("UPDATE target_mappings SET canonical_field = ? WHERE migration_id = ? AND entity_name = ? "
                             "AND target_field = ? AND canonical_field IS NULL AND status = 'PROPOSED' AND spec_version = ?",
                             (canonical, migration_id, entity, field["name"], str(spec["spec_version"])))
    if blocked:
        raise ValueError(f"{blocked} target mapping(s) in this migration were made against an earlier target model. "
                         "Start a new migration (or reset the demo) to map to the confirmed target system.")
    return created


def list_mappings(conn: sqlite3.Connection, migration_id: str, spec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ensure_table(conn)
    if not spec:
        return []
    # Rows from an earlier target specification version stay in the table for audit but are not current mappings.
    rows = conn.execute("SELECT * FROM target_mappings WHERE migration_id = ? AND spec_version = ? ORDER BY entity_name, target_field",
                        (migration_id, str(spec["spec_version"]))).fetchall()
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


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def merge_by_key(rows_by_file: List[List[Dict[str, Any]]], key_field: Optional[str]) -> Dict[str, Any]:
    """Join an entity's files on its canonical key: one row per key, first non-blank value wins.

    Rows without the key (or from files that do not carry it) stay separate and are counted, so
    a field living in a different file is not reported as blank for the wrong rows. Conflicting
    non-blank values for the same key are counted, never silently overwritten.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    unkeyed: List[Dict[str, Any]] = []
    conflicts = 0
    for rows in rows_by_file:
        for row in rows:
            key = None if not key_field or _blank(row.get(key_field)) else str(row[key_field]).strip()
            if key is None:
                unkeyed.append(dict(row))
                continue
            target = merged.setdefault(key, {})
            for field, value in row.items():
                if _blank(value):
                    target.setdefault(field, value)
                elif _blank(target.get(field)):
                    target[field] = value
                elif str(target[field]) != str(value):
                    conflicts += 1
    return {"rows": list(merged.values()) + unkeyed, "keyed_rows": len(merged), "unkeyed_rows": len(unkeyed), "conflicts": conflicts}


def preview(spec: Dict[str, Any], approved: List[Dict[str, Any]], canonical_rows: Dict[str, List[Dict[str, Any]]],
            join_info: Optional[Dict[str, Dict[str, int]]] = None) -> Dict[str, Any]:
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
            "join": (join_info or {}).get(entity),
            "uncovered_required": [f["target_field"] for f in fields if f["required"] and not f["approved"]],
            "blank_required": [f["target_field"] for f in fields if f["required"] and f["approved"] and f["blank_values"]],
            "warnings": [f"{note} ({n} value{'s' if n != 1 else ''})" for note, n in sorted(warnings.items())][:10],
        }
    return out
