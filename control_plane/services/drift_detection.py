"""Source drift: an approved mapping whose source field no longer exists in the entity's current source files.

This happens when a file is replaced or re-uploaded with different columns after the mapping was approved. The finding is
evidence for a reviewer (names only, no values); mappings are never changed automatically.
"""
from typing import Any, Dict, Iterable, List


def _norm(name: str) -> str:
    return "".join(ch for ch in str(name).upper() if ch.isalnum())


def detect(approved_mappings: Iterable[Dict[str, Any]], entity_source_files: Dict[str, Dict[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    current: Dict[str, Dict[str, List[str]]] = {}
    for entity, group in (entity_source_files or {}).items():
        cols: Dict[str, List[str]] = {}
        for rec in group.values():
            if rec.get("inference_approval_status") == "REJECTED":
                continue
            for c in rec.get("columns") or []:
                cols.setdefault(_norm(c), []).append(rec.get("filename", "file"))
        current[entity] = cols
    findings = []
    for m in approved_mappings:
        entity, field = m["entity_name"], m["source_field"]
        if entity not in current:
            continue  # no files registered for the entity: nothing to compare against
        if _norm(field) not in current[entity]:
            findings.append({"entity": entity, "source_field": field, "canonical_field": m.get("canonical_field"),
                             "detail": "Approved source field is not present in any current source file for this entity."})
    return findings
