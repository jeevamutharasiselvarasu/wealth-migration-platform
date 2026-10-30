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

# Roles are personas: a user may hold several (a normal user holds Analyst + Validation Specialist,
# the Domain SME holds both plus the canonical-model persona) and acts under the persona chosen in
# the session. Platform Admin holds every capability.
PLATFORM_ROLES: Set[str] = {ADMIN}                    # demo data load, workflow replay (platform maintenance)
SETUP_ROLES: Set[str] = {ADMIN, ANALYST}              # migration setup, scope, source environments
DISCOVERY_ROLES: Set[str] = {ADMIN, ANALYST}          # upload, profiling, mapping generation
SCOPE_REVIEW_ROLES: Set[str] = {ADMIN, ANALYST}       # confirm/reject entity inference
DEFINITION_ROLES: Set[str] = {ADMIN, ANALYST, SME}    # answer information requests (SME: canonical meaning)
APPROVAL_ROLES: Set[str] = {ADMIN, ANALYST}           # mapping/transformation approval, overrides
CANONICAL_ROLES: Set[str] = {ADMIN, SME}              # canonical model / kernel changes, decide change requests
CHANGE_REQUEST_ROLES: Set[str] = {ADMIN, ANALYST}     # raise a canonical change request
VALIDATION_ROLES: Set[str] = {ADMIN, VALIDATOR}       # validation-side evaluation
TARGET_PREVIEW_ROLES: Set[str] = {ADMIN, ANALYST, VALIDATOR}  # read-only canonical-to-target dry run

# Routes any authenticated user may POST (authentication/persona handled by their own checks).
OPEN_POST_PATHS = {"/login", "/persona-selection"}

POST_POLICY = [
    (r"^/api/migration/setup$", SETUP_ROLES),
    (r"^/api/demo/load$", PLATFORM_ROLES),
    (r"^/api/migration/new$", SETUP_ROLES),
    (r"^/api/entities/save-scopes$", SETUP_ROLES),
    (r"^/api/adls/upload$", SETUP_ROLES),
    (r"^/api/adf/trigger$", SETUP_ROLES),
    (r"^/api/workflow/replay$", PLATFORM_ROLES),
    (r"^/api/kernel/update$", CANONICAL_ROLES),
    (r"^/api/canonical-changes$", CHANGE_REQUEST_ROLES),
    (r"^/api/canonical-changes/[^/]+/apply$", CANONICAL_ROLES),
    (r"^/api/canonical-changes/[^/]+/decision$", CANONICAL_ROLES),
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
    (r"^/api/validation/run$", VALIDATION_ROLES),
    (r"^/api/validation/scenarios$", VALIDATION_ROLES),
    (r"^/api/validation/scenarios/[^/]+/retire$", VALIDATION_ROLES),
    (r"^/api/validation/exceptions$", VALIDATION_ROLES),
    (r"^/api/validation/exceptions/[^/]+/decision$", APPROVAL_ROLES),
    (r"^/api/target-system/(configure|infer|confirm|reopen)$", PLATFORM_ROLES),
    (r"^/api/target-mapping/gaps/raise$", CHANGE_REQUEST_ROLES),
    (r"^/api/target-mapping/propose$", DISCOVERY_ROLES),
    (r"^/api/target-mapping/preview$", TARGET_PREVIEW_ROLES),
    (r"^/api/target-mapping/adf-package$", TARGET_PREVIEW_ROLES),
    (r"^/api/target-mapping/[^/]+/review$", APPROVAL_ROLES),
    (r"^/api/transformation-rules/preview$", DISCOVERY_ROLES),
    (r"^/api/transformation-rules/infer$", DISCOVERY_ROLES),
    (r"^/api/transformation-rules$", DISCOVERY_ROLES),
    (r"^/api/transformation-rules/[^/]+/[^/]+/(decision|retire)$", APPROVAL_ROLES),
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
