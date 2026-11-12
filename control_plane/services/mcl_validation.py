from collections import Counter
from typing import Any, Dict, List


def _present(value: Any) -> bool:
    if value is None:
        return False
    try:
        # pandas/numpy null values without requiring pandas as a dependency here
        if value != value:
            return False
    except Exception:
        pass
    return str(value).strip() != ""


def _key_values(records: List[Dict[str, Any]], field: str) -> List[str]:
    return [str(row.get(field)).strip() for row in records if _present(row.get(field))]


def _key_check(entity: str, records: List[Dict[str, Any]], field: str) -> Dict[str, Any]:
    values = _key_values(records, field)
    missing = sum(1 for row in records if not _present(row.get(field)))
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    status = "FAIL" if missing or duplicates else "PASS"
    return {
        "check": "CANONICAL_KEY",
        "entity": entity,
        "field": field,
        "status": status,
        "record_count": len(records),
        "missing_count": missing,
        "duplicate_values": duplicates,
        "message": (
            f"{field} has {missing} missing value(s) and {len(duplicates)} duplicate value(s)."
            if status == "FAIL" else f"{field} is present and unique."
        ),
    }


def _relationship_check(
    source_entity: str,
    source_records: List[Dict[str, Any]],
    source_field: str,
    target_entity: str,
    target_records: List[Dict[str, Any]],
    target_field: str,
) -> Dict[str, Any]:
    source_has_field = any(source_field in row for row in source_records)
    if not source_records or not source_has_field:
        return {
            "check": "RELATIONSHIP",
            "from_entity": source_entity,
            "from_field": source_field,
            "to_entity": target_entity,
            "to_field": target_field,
            "status": "NOT_EVALUATED",
            "checked_count": 0,
            "orphan_count": 0,
            "orphan_values": [],
            "message": f"{source_entity}.{source_field} is not present in the mapped source; relationship was not inferred.",
        }

    target_keys = set(_key_values(target_records, target_field))
    source_values = [str(row.get(source_field)).strip() for row in source_records if _present(row.get(source_field))]
    orphans = sorted({value for value in source_values if value not in target_keys})
    missing = sum(1 for row in source_records if not _present(row.get(source_field)))
    status = "FAIL" if orphans else ("WARN" if missing else "PASS")
    return {
        "check": "RELATIONSHIP",
        "from_entity": source_entity,
        "from_field": source_field,
        "to_entity": target_entity,
        "to_field": target_field,
        "status": status,
        "checked_count": len(source_values),
        "missing_count": missing,
        "orphan_count": len(orphans),
        "orphan_values": orphans[:50],
        "message": (
            f"{len(orphans)} orphan reference(s) found."
            if orphans else (f"{missing} relationship value(s) are missing." if missing else "All populated references resolve.")
        ),
    }


def _relationship_candidate_check(
    candidates: List[Dict[str, Any]],
    target_records: List[Dict[str, Any]],
    target_entity: str,
    target_field: str,
    from_entity: str = "",
    from_field: str = "",
) -> Dict[str, Any]:
    target_keys = set(_key_values(target_records, target_field))
    values = [str(item.get("target_key_value")).strip() for item in candidates if _present(item.get("target_key_value"))]
    orphans = sorted({value for value in values if value not in target_keys})
    missing = len(candidates) - len(values)
    status = "FAIL" if orphans else ("WARN" if missing else "PROPOSED")
    return {
        "check": "RELATIONSHIP_CANDIDATE",
        "from_entity": from_entity,
        "from_field": from_field,
        "to_entity": target_entity,
        "to_field": target_field,
        "status": status,
        "rule_status": "PROPOSED_REQUIRES_SOURCE_CONFIRMATION",
        "checked_count": len(values),
        "missing_count": missing,
        "orphan_count": len(orphans),
        "orphan_values": orphans[:50],
        "message": (
            f"{len(orphans)} {from_entity}-to-{target_entity} candidate reference(s) do not resolve."
            if orphans else (f"{missing} candidate relationship value(s) are missing." if missing else "Candidate references resolve, but the source relationship meaning still requires confirmation and approval.")
        ),
    }


def validate_mcl_relationships(mapped_by_entity: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Validate key uniqueness and only those cross-entity links evidenced by mapped fields.

    Which fields are keys, and which fields point at another entity's key, come from the firm configuration (not from a list here).
    Missing relationship columns are reported as NOT_EVALUATED rather than guessed. A source column a person must confirm as a link
    (a relationship candidate the mapper found) is reported as a candidate, never as a fact.
    """
    from control_plane.services.firm_config import FirmConfigError, key_fields, reference_fields
    records = {e: (m or {}).get("mapped_records", []) for e, m in mapped_by_entity.items()}
    try:
        refs = reference_fields()
        keys = {e: key_fields(e) for e in records}
    except FirmConfigError:
        refs, keys = {}, {e: [] for e in records}

    key_checks = [_key_check(entity, records[entity], key) for entity in records for key in keys.get(entity, [])]
    relationship_checks = [
        _relationship_check(entity, records[entity], field, other, records[other], other_key)
        for entity in records for field, (other, other_key) in (refs.get(entity) or {}).items() if other in records
    ]
    for entity, mapped in mapped_by_entity.items():
        by_target: Dict[tuple, List[Dict[str, Any]]] = {}
        for item in (mapped or {}).get("relationship_candidates", []) or []:
            by_target.setdefault((item.get("target_entity"), item.get("target_field"), item.get("source_field")), []).append(item)
        for (target_entity, target_field, source_field), items in by_target.items():
            if target_entity in records:
                relationship_checks.append(_relationship_candidate_check(items, records[target_entity], target_entity, target_field,
                                                                         entity, source_field))
    all_checks = key_checks + relationship_checks
    statuses = {check["status"] for check in all_checks}
    if "FAIL" in statuses:
        status = "FAIL"
    elif "WARN" in statuses or "NOT_EVALUATED" in statuses or "PROPOSED" in statuses:
        status = "WARN"
    else:
        status = "PASS"
    return {
        "status": status,
        "key_checks": key_checks,
        "relationship_checks": relationship_checks,
        "summary": {
            "checks_total": len(all_checks),
            "failed": sum(check["status"] == "FAIL" for check in all_checks),
            "warnings": sum(check["status"] in {"WARN", "NOT_EVALUATED", "PROPOSED"} for check in all_checks),
            "passed": sum(check["status"] == "PASS" for check in all_checks),
        },
    }
