"""Role-based "My queue": what needs this persona right now, with a direct link.

Pure function over the workflow counts and the role policy, so the same rules that gate the
POST routes decide who sees an actionable item. Items for a role never include actions that
role may not perform; roles that only follow an item see it as informational.
"""
from typing import Any, Dict, List

from ui.role_policy import (APPROVAL_ROLES, PLATFORM_ROLES, CANONICAL_ROLES, CHANGE_REQUEST_ROLES, DEFINITION_ROLES, DISCOVERY_ROLES,
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
    if not c.get("target_provisioned"):
        if role in PLATFORM_ROLES:
            add("Complete the one-time target setup", "/target-mapping#target-system", "Configure, infer and confirm the target system before canonical-to-target mapping.")
        elif role in DISCOVERY_ROLES:
            add("Target setup awaits a Platform Admin", "/target-mapping#target-system", "One-time platform step; mapping starts once it is done.", actionable=False)
    if c.get("target_pending") and role in APPROVAL_ROLES:
        n = c["target_pending"]
        add(f"Approve {_plural(n, 'target mapping')}", "/target-mapping", "Canonical-to-target proposals waiting for a decision.", n)
    if (c.get("er_pending") or c.get("roles_needed")) and role in APPROVAL_ROLES:
        n = c.get("er_pending") or 0
        add(f"Decide {_plural(n, 'match rule')}" if n else "Decide where account roles come from", "/entity-resolution",
            "Entity resolution: which rows are the same record, and who holds each account.", n)
    if (c.get("codes_pending") or c.get("codes_needed")) and role in APPROVAL_ROLES:
        n = c.get("codes_pending") or 0
        add(f"Map {_plural(n, 'code value')}" if n else "Read the code values", "/code-sets", "Each source value of a coded field needs a canonical code.", n)
    if c.get("mappings_approved") and role in VALIDATION_ROLES:
        status = c.get("validation_status") or ""
        if status != "PASS":
            label = {"FAIL": "Review failed validation", "WARN": "Review validation warnings", "STALE": "Run validation again"}.get(status, "Run validation")
            add(label, "/reconciliation",
                "Mappings changed after the last run." if status == "STALE" else
                ("Check keys, required fields and relationships." if status else "Mappings are approved; no validation run yet."))
    # Stages 07-10 (data plane): the current stage, for the role that owns it.
    current = next((st["number"] for st in c.get("stages") or [] if st.get("state") == "current"), None)
    owner = {"07": APPROVAL_ROLES, "08": APPROVAL_ROLES, "09": APPROVAL_ROLES, "10": VALIDATION_ROLES}.get(current)
    nxt = c.get("next") or {}
    if owner and role in owner and nxt.get("href"):
        add(nxt.get("label", "Continue"), nxt["href"], nxt.get("detail", ""))
    return items
