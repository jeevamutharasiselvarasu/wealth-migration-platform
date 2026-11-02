"""Deterministic source-to-canonical validation for the P0 entities.

Reads approved mappings and the confirmed source files; never executes transformations.
Values are checked as supplied in the source, before any registered transformation.
No target-side or load verification is performed (out of scope for the P0 pilot).
"""
from typing import Any, Dict, List, Optional

from control_plane.services.validation_scenarios import evaluate as scenario_eval
from control_plane.services.mcl_validation import _key_check, _present, _relationship_check

ENTITIES = ("Account", "Client", "Household")
RELATIONSHIPS = (
    ("Account", "HH_ID", "Household", "HH_ID"),
    ("Account", "CLIENT_ID", "Client", "CLIENT_ID"),
)


def _canonical_rows(rows: List[Dict[str, Any]], source_to_canonical: Dict[str, str]) -> List[Dict[str, Any]]:
    return [{canon: row.get(src) for src, canon in source_to_canonical.items() if src in row} for row in rows]


def run_checks(
    contract: Dict[str, Dict[str, List[str]]],
    mappings: Dict[str, Dict[str, str]],
    files: Dict[str, List[Dict[str, Any]]],
    scenarios: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """contract[entity] = {"required": [...], "keys": [...]};
    mappings[entity] = {source_field: canonical_field} (approved only);
    files[entity] = [{"filename": str, "records": [dict]}] (confirmed files only)."""
    checks: List[Dict[str, Any]] = []
    merged: Dict[str, List[Dict[str, Any]]] = {}
    per_file: Dict[str, List[tuple]] = {}
    for entity in ENTITIES:
        required = contract.get(entity, {}).get("required", [])
        keys = contract.get(entity, {}).get("keys", [])
        source_to_canonical = mappings.get(entity, {})
        mapped_canonical = set(source_to_canonical.values())
        missing = [f for f in required if f not in mapped_canonical]
        checks.append({
            "check": "REQUIRED_FIELDS_MAPPED", "entity": entity, "status": "FAIL" if missing else "PASS",
            "detail": ("No approved mapping for: " + ", ".join(missing)) if missing else "Every required canonical field has an approved mapping.",
        })
        entity_rows: List[Dict[str, Any]] = []
        for item in files.get(entity, []):
            canonical = _canonical_rows(item["records"], source_to_canonical)
            entity_rows.extend(canonical)
            present_here = set().union(*[set(r) for r in canonical]) if canonical else set()
            per_file.setdefault(entity, []).append((item["filename"], canonical, present_here))
            blanks = {f: sum(1 for r in canonical if not _present(r.get(f))) for f in required if f in present_here}
            blanks = {f: n for f, n in blanks.items() if n}
            if present_here & set(required):
                checks.append({
                    "check": "REQUIRED_VALUES_PRESENT", "entity": entity, "file": item["filename"],
                    "status": "FAIL" if blanks else "PASS",
                    "detail": ("Blank values: " + ", ".join(f"{f} ({n} row(s))" for f, n in blanks.items())) if blanks
                    else f"Required fields supplied by this file are populated in all {len(canonical)} row(s).",
                })
            for key in keys:
                if key in present_here:
                    res = _key_check(entity, canonical, key)
                    checks.append({
                        "check": "KEY_UNIQUE", "entity": entity, "file": item["filename"], "field": key,
                        "status": res["status"], "detail": res["message"],
                    })
        merged[entity] = entity_rows
    for src_entity, src_field, dst_entity, dst_field in RELATIONSHIPS:
        # Only rows from files that carry the column take part; other files cannot be judged on it.
        src_rows = [r for r in merged.get(src_entity, []) if src_field in r]
        dst_rows = [r for r in merged.get(dst_entity, []) if dst_field in r]
        res = _relationship_check(src_entity, src_rows, src_field, dst_entity, dst_rows, dst_field)
        checks.append({
            "check": "RELATIONSHIP", "entity": src_entity, "field": f"{src_field} → {dst_entity}.{dst_field}",
            "status": res["status"], "detail": res["message"],
            "orphans": res.get("orphan_values", [])[:10],
        })
    for sc in scenarios or []:
        label = f"SCENARIO {sc['scenario_id']}"
        if sc["stage"] == "POST":
            checks.append({"check": label, "entity": sc["entity_name"], "field": sc.get("field_name") or "", "status": "NOT_EVALUATED",
                           "detail": f"{sc['description']} (post-execution check; needs data-plane results)."})
            continue
        if sc["check_type"] == "ROW_COUNT_MIN":
            res = scenario_eval(sc, "", merged.get(sc["entity_name"], []))
            checks.append({"check": label, "entity": sc["entity_name"], **res, "detail": f"{sc['description']}: {res['detail']}"})
            continue
        hit = False
        for filename, canonical, present in per_file.get(sc["entity_name"], []):
            if sc["field_name"] in present:
                hit = True
                res = scenario_eval(sc, filename, canonical)
                checks.append({"check": label, "entity": sc["entity_name"], "file": filename, "field": sc["field_name"], **res,
                               "detail": f"{sc['description']}: {res['detail']}"})
        if not hit:
            checks.append({"check": label, "entity": sc["entity_name"], "field": sc["field_name"], "status": "NOT_EVALUATED",
                           "detail": f"{sc['description']}: no confirmed file supplies {sc['field_name']}."})
    for c in checks:
        c["ref"] = "|".join(str(c.get(k, "")) for k in ("check", "entity", "file", "field"))
    statuses = {c["status"] for c in checks}
    overall = "FAIL" if "FAIL" in statuses else ("WARN" if statuses & {"WARN", "NOT_EVALUATED"} else "PASS")
    summary = {s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "WARN", "FAIL", "NOT_EVALUATED")}
    return {"status": overall, "summary": summary, "checks": checks}
