"""Server-side role policy for state-changing routes.

Roles come from the authenticated session, never from the request. Every POST route must be
classified here (see tests/test_role_policy.py); an unclassified route fails the test suite.
"""
from __future__ import annotations

import re
from typing import Optional, Set

ADMIN = "Platform Admin"
ANALYST = "Migration Analyst"
SME = "Domain SME"
VALIDATOR = "Validation Specialist"

SETUP_ROLES: Set[str] = {ADMIN}                      # workspace, scope, environments, loads
DISCOVERY_ROLES: Set[str] = {ADMIN, ANALYST}         # upload, profiling, mapping generation
SCOPE_REVIEW_ROLES: Set[str] = {ADMIN, ANALYST, SME}  # confirm/reject entity inference
DEFINITION_ROLES: Set[str] = {ADMIN, ANALYST, SME}    # answer information requests
APPROVAL_ROLES: Set[str] = {ADMIN, SME}               # mapping approval, overrides, kernel edits
VALIDATION_ROLES: Set[str] = {ADMIN, VALIDATOR}       # validation-side evaluation

# Routes any authenticated user may POST (authentication/persona handled by their own checks).
OPEN_POST_PATHS = {"/login", "/persona-selection"}

POST_POLICY = [
    (r"^/api/migration/setup$", SETUP_ROLES),
    (r"^/api/demo/load$", SETUP_ROLES),
    (r"^/api/entities/save-scopes$", SETUP_ROLES),
    (r"^/api/adls/upload$", SETUP_ROLES),
    (r"^/api/adf/trigger$", SETUP_ROLES),
    (r"^/api/workflow/replay$", SETUP_ROLES),
    (r"^/api/kernel/update$", APPROVAL_ROLES),
    (r"^/api/knowledge/(submit-questionnaire|file-upload)$", DISCOVERY_ROLES),
    (r"^/api/agents/analyze/[^/]+$", DISCOVERY_ROLES),
    (r"^/api/entities/upload-sample-file$", DISCOVERY_ROLES),
    (r"^/api/entities/map-(uploaded-file|approved-files|mcl)$", DISCOVERY_ROLES),
    (r"^/api/pilot/(account|household|client)/(profile|map)$", DISCOVERY_ROLES),
    (r"^/api/pipeline/run$", DISCOVERY_ROLES),
    (r"^/api/entities/confirm-(entity|inference)$", SCOPE_REVIEW_ROLES),
    (r"^/api/mapping-gaps/[^/]+/response$", DEFINITION_ROLES),
    (r"^/api/mapping-suggestions/(approve-entity|[^/]+/review)$", APPROVAL_ROLES),
    (r"^/api/pipeline/override-agent$", APPROVAL_ROLES),
    (r"^/api/hitl/(approve|reject)$", APPROVAL_ROLES),
    (r"^/api/waves/evaluate-record$", VALIDATION_ROLES),
]
_COMPILED = [(re.compile(pattern), roles) for pattern, roles in POST_POLICY]


def allowed_roles_for(method: str, path: str) -> Optional[Set[str]]:
    """Roles allowed to call a state-changing route; None when the route is not classified/restricted."""
    if method.upper() != "POST" or path in OPEN_POST_PATHS:
        return None
    for pattern, roles in _COMPILED:
        if pattern.match(path):
            return roles
    return None


def is_classified(path: str) -> bool:
    return path in OPEN_POST_PATHS or any(pattern.match(path) for pattern, _ in _COMPILED)
