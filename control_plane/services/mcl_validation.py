from collections import Counter\nfrom typing import Any, Dict, List


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


def validate_mcl_relationships(mapped_by_entity: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Validate MCL key uniqueness and only those cross-entity links evidenced by mapped fields.

    Client is a source entity whose identity fields map to canonical Party fields. Missing
    relationship columns are reported as NOT_EVALUATED rather than guessed.
    """
    account = (mapped_by_entity.get("Account") or {}).get("mapped_records", [])
    household = (mapped_by_entity.get("Household") or {}).get("mapped_records", [])
    client = (mapped_by_entity.get("Client") or {}).get("mapped_records", [])

    key_checks = [
        _key_check("Account", account, "ACCT_NUM"),
        _key_check("Household", household, "HH_ID"),
        _key_check("Client (canonical Party)", client, "PARTY_ID"),
    ]
    relationship_checks = [
        _relationship_check("Account", account, "HH_ID", "Household", household, "HH_ID"),
        _relationship_check("Account", account, "PARTY_ID", "Client (canonical Party)", client, "PARTY_ID"),
        _relationship_check("Household", household, "PRIMARY_PARTY_ID", "Client (canonical Party)", client, "PARTY_ID"),
    ]
    all_checks = key_checks + relationship_checks
    statuses = {check["status"] for check in all_checks}
    if "FAIL" in statuses:
        status = "FAIL"
    elif "WARN" in statuses or "NOT_EVALUATED" in statuses:
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
            "warnings": sum(check["status"] in {"WARN", "NOT_EVALUATED"} for check in all_checks),
            "passed": sum(check["status"] == "PASS" for check in all_checks),
        },
    }
