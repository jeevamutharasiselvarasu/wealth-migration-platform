"""Role-based "My queue": what needs this persona right now, with a direct link.

Pure function over the workflow counts and the role policy, so the same rules that gate the
POST routes decide who sees an actionable item. Items for a role never include actions that
role may not perform; roles that only follow an item see it as informational.
"""
from typing import Any, Dict, List

from ui.role_policy import (APPROVAL_ROLES, CANONICAL_ROLES, CHANGE_REQUEST_ROLES, DEFINITION_ROLES, DISCOVERY_ROLES,
                            SCOPE_REVIEW_ROLES, SETUP_ROLES, VALIDATION_ROLES)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def build_queue(role: str, c: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    def add(label: str, href: str, detail: str, count: int = 0, actionable: bool = True) -> None:
        items.append({"label": label, "href": href, "detail": detail, "count": count, "actionable": actionable})

    configured = bool(c.get("scope"))
    if not configured and role in SETUP_ROLES:
        add("Set up the migration", "/migration-setup", "Choose the entities in scope and register knowledge.")
    if configured and not c.get("files_total") and role in DISCOVERY_ROLES:
        add("Add source files", "/entity-sample-files", "Upload CoreCap files or load the demo data set.")
    if c.get("files_pending") and role in SCOPE_REVIEW_ROLES:
        n = c["files_pending"]
        add(f"Confirm {_plural(n, 'source file')}", "/entity-sample-files", "Review the inferred entity for each file.", n)
    if c.get("proposals_pending") and role in APPROVAL_ROLES:
        n = c["proposals_pending"]
        add(f"Review {_plural(n, 'mapping proposal')}", "/agent-pipeline", "Approve or reject source-to-canonical mappings.", n)
    if c.get("rfi_open") and role in DEFINITION_ROLES:
        n = c["rfi_open"]
        add(f"Answer {_plural(n, 'information request')}", "/agent-pipeline#information-requests", "Required canonical fields have no source yet.", n)
    if c.get("changes_open"):
        n = c["changes_open"]
        if role in CANONICAL_ROLES:
            add(f"Decide {_plural(n, 'change request')}", "/canonical-changes", "Approve, defer or reject, then apply approved ones.", n)
        elif role in CHANGE_REQUEST_ROLES:
            add(f"{_plural(n, 'change request')} awaiting the Domain SME", "/canonical-changes", "Follow progress; regenerate target proposals once applied.", n, actionable=False)
    if configured and not c.get("target_provisioned") and role in DISCOVERY_ROLES:
        add("Provision the simulated target system", "/target-mapping", "One-time step before canonical-to-target mapping.")
    if c.get("target_pending") and role in APPROVAL_ROLES:
        n = c["target_pending"]
        add(f"Approve {_plural(n, 'target mapping')}", "/target-mapping", "Canonical-to-target proposals waiting for a decision.", n)
    if c.get("mappings_approved") and role in VALIDATION_ROLES:
        status = c.get("validation_status") or ""
        if status != "PASS":
            add("Review failed validation" if status == "FAIL" else "Run validation", "/reconciliation",
                "Check keys, required fields and relationships." if status else "Mappings are approved; no validation run yet.")
    return items
