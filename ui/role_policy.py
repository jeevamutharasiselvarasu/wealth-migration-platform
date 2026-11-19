"""Server-side role policy for state-changing routes.

Roles come from the authenticated session, never from the request. Every POST route must be
classified here (see tests/test_role_policy.py); an unclassified route fails the test suite.
"""
from __future__ import annotations

import re
from typing import Optional, Set

ADMIN = "Platform Admin"
IMO_LEAD = "IMO Lead"                                 # creates and configures migration workspaces (suite template role IL)
COMPLIANCE = "Compliance Reviewer"                   # read-only evidence and audit access (suite template role CR); holds no write capability
ANALYST = "Migration Analyst"
SME = "Domain SME"
VALIDATOR = "Validation Specialist"

# Roles are personas: a user may hold several (a normal user holds Analyst + Validation Specialist,
# the Domain SME holds both plus the canonical-model persona) and acts under the persona chosen in
# the session. The Platform Admin is infrastructure-focused: it holds only the platform-level
# capabilities below and none of the migration-side ones, so it never sees or acts on migration data.
# The IMO Lead persona carries both the Migration Analyst and the Validation Specialist rights. Rules that need two different people
# (an exception is decided by someone other than its proposer) still apply by person, whatever the persona.
PLATFORM_ROLES: Set[str] = {ADMIN}                    # platform setup (Azure, Git, SQL, blob, target system), users, demo load, replay
SETUP_ROLES: Set[str] = {IMO_LEAD, ANALYST}                     # migration setup, scope, source environments
DISCOVERY_ROLES: Set[str] = {ANALYST, IMO_LEAD}       # upload, profiling, mapping generation
SCOPE_REVIEW_ROLES: Set[str] = {ANALYST, IMO_LEAD}    # confirm/reject entity inference
DEFINITION_ROLES: Set[str] = {ANALYST, SME, IMO_LEAD} # answer information requests (SME: canonical meaning)
APPROVAL_ROLES: Set[str] = {ANALYST, IMO_LEAD}        # mapping/transformation approval, overrides
CANONICAL_ROLES: Set[str] = {ADMIN, SME}              # canonical model / kernel changes, decide change requests (platform level)
CHANGE_REQUEST_ROLES: Set[str] = {ANALYST, IMO_LEAD}  # raise a canonical change request
VALIDATION_ROLES: Set[str] = {VALIDATOR, IMO_LEAD}    # validation-side evaluation
TARGET_PREVIEW_ROLES: Set[str] = {ANALYST, VALIDATOR, IMO_LEAD} # read-only canonical-to-target dry run
RUN_FAILURE_ROLES: Set[str] = {ANALYST, VALIDATOR, IMO_LEAD}    # raise an information request from a failed pipeline step
PLATFORM_KNOWLEDGE_ROLES: Set[str] = {ADMIN, SME}     # add/retire platform-level knowledge shared by every migration

# Pages the Platform Admin may open: platform setup and administration only. Everything else (migration
# workspaces, files, mappings, runs, validation) is migration data and is not shown to the Platform Admin.
ADMIN_VIEW_PREFIXES = (
    "/static", "/login", "/logout", "/register", "/healthz", "/persona-selection", "/suite", "/setup",
    "/migrations", "/migration-setup",  # the workspace list and setup (configuration only, read-only), never the data inside a workspace
    "/azure-setup", "/github-setup", "/target-setup", "/admin/users", "/governance", "/tenant", "/models",
    "/platform-knowledge", "/api/platform-knowledge", "/canonical-changes", "/canonical-model", "/api/canonical-model", "/api/admin", "/api/azure", "/api/git", "/api/sql-target",
    "/api/users", "/api/target-system", "/api/blob/layout", "/api/blob/container",
)


# Platform setup pages only the Platform Admin opens. Migration users get what they need from these settings through
# the migration setup (filled in from platform setup) and the stage pages, never the setup forms themselves.
PLATFORM_ONLY_PAGES = ("/azure-setup", "/github-setup", "/target-setup")


def platform_only_page(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PLATFORM_ONLY_PAGES)


def admin_may_view(path: str) -> bool:
    """True when the Platform Admin persona may open this path (platform setup/administration, never migration data).
    "/" is the migration dashboard, so it is not one of them."""
    return any(path == p or path.startswith(p + "/") for p in ADMIN_VIEW_PREFIXES)


# Routes any authenticated user may POST (authentication/persona handled by their own checks).
OPEN_POST_PATHS = {"/login", "/persona-selection", "/register"}

POST_POLICY = [
    (r"^/api/migration/setup$", SETUP_ROLES),
    (r"^/api/azure/connection(/test)?$", PLATFORM_ROLES),
    (r"^/api/git/connection(/test)?$", PLATFORM_ROLES),
    (r"^/api/sql-target(/test)?$", PLATFORM_ROLES),
    (r"^/api/live-target(/test)?$", PLATFORM_ROLES),
    (r"^/api/git/pull-request$", APPROVAL_ROLES),
    (r"^/api/git/pull-request/[^/]+/refresh$", TARGET_PREVIEW_ROLES),
    (r"^/api/git/pull-request/[^/]+/merge$", APPROVAL_ROLES),
    (r"^/api/data-plane/(setup|start|promote)$", APPROVAL_ROLES),
    (r"^/api/data-plane/deployment-check$", TARGET_PREVIEW_ROLES),
    (r"^/api/live-validation/run$", VALIDATION_ROLES),
    (r"^/api/data-plane/runs/[^/]+/refresh$", TARGET_PREVIEW_ROLES),
    (r"^/api/git/publish$", APPROVAL_ROLES),
    (r"^/api/users/[^/]+/(decision|delete)$", PLATFORM_ROLES),
    (r"^/api/migration/new$", SETUP_ROLES),
    (r"^/api/migration/open$", SETUP_ROLES),
    (r"^/api/migration/archive$", SETUP_ROLES),
    (r"^/api/entities/save-scopes$", SETUP_ROLES),
    (r"^/api/adls/upload$", SETUP_ROLES),
    (r"^/api/adf/trigger$", SETUP_ROLES),
    (r"^/api/workflow/replay$", PLATFORM_ROLES),
    (r"^/api/kernel/update$", CANONICAL_ROLES),
    (r"^/api/canonical-changes$", CHANGE_REQUEST_ROLES),
    (r"^/api/canonical-changes/[^/]+/apply$", CANONICAL_ROLES),
    (r"^/api/canonical-changes/[^/]+/decision$", CANONICAL_ROLES),
    (r"^/api/canonical-releases$", CANONICAL_ROLES),
    (r"^/api/canonical-releases/adopt$", APPROVAL_ROLES),
    (r"^/api/canonical-model/release$", CANONICAL_ROLES),
    (r"^/api/code-sets/profile$", DISCOVERY_ROLES),
    (r"^/api/code-sets/(decide|approve-suggested)$", APPROVAL_ROLES),
    (r"^/api/target-system/code-values$", PLATFORM_ROLES),
    (r"^/api/target-system/adapter/(draft|decide|approve-proposed|confirm)$", PLATFORM_ROLES),
    (r"^/api/target-mapping/adopt-adapter$", APPROVAL_ROLES),
    (r"^/api/entity-resolution/suggest$", DISCOVERY_ROLES),
    (r"^/api/entity-resolution/(decide|account-roles)$", APPROVAL_ROLES),
    (r"^/api/entity-resolution/preview$", TARGET_PREVIEW_ROLES),
    (r"^/api/knowledge/(submit-questionnaire|file-upload|confluence|remove|web-link|resync|feedback|retrieval/(tune|retire))$", DISCOVERY_ROLES),
    (r"^/api/platform-knowledge/sources/.+$", PLATFORM_ROLES),
    (r"^/api/platform-knowledge(/[^/]+/retire)?$", PLATFORM_KNOWLEDGE_ROLES),
    (r"^/api/agents/analyze/[^/]+$", DISCOVERY_ROLES),
    (r"^/api/relationships/review$", DISCOVERY_ROLES),
    (r"^/api/entities/upload-sample-file$", DISCOVERY_ROLES),
    (r"^/api/blob/load$", DISCOVERY_ROLES),
    (r"^/api/blob/layout$", PLATFORM_ROLES),
    (r"^/api/blob/(container|test)$", PLATFORM_ROLES),
    (r"^/api/entities/map-(uploaded-file|approved-files|mcl)$", DISCOVERY_ROLES),
    (r"^/api/pipeline/run$", DISCOVERY_ROLES),
    (r"^/api/entities/(confirm-(entity|inference)|review-selected)$", SCOPE_REVIEW_ROLES),
    (r"^/api/mapping-gaps/[^/]+/response$", DEFINITION_ROLES),
    (r"^/api/mapping-suggestions/(approve-entity|review-selected|[^/]+/review)$", APPROVAL_ROLES),
    (r"^/api/mappings/reopen(-rejected)?$", APPROVAL_ROLES),
    (r"^/api/pipeline/override-agent$", APPROVAL_ROLES),
    (r"^/api/hitl/(approve|reject)$", APPROVAL_ROLES),
    (r"^/api/waves/evaluate-record$", VALIDATION_ROLES),
    (r"^/api/validation/run$", VALIDATION_ROLES),
    (r"^/api/execution/run$", VALIDATION_ROLES),
    (r"^/api/adf/simulated-(deploy|trigger)$", VALIDATION_ROLES),
    (r"^/api/execution/runs/[^/]+/failures/[^/]+/review$", APPROVAL_ROLES),
    (r"^/api/execution/recommendations/[^/]+/decide$", APPROVAL_ROLES),
    (r"^/api/execution/recommendations/bulk-decide$", APPROVAL_ROLES),
    (r"^/api/execution/runs/[^/]+/failures/bulk-review$", APPROVAL_ROLES),
    (r"^/api/execution/runs/[^/]+/promote$", APPROVAL_ROLES),
    (r"^/api/execution/start-pipeline$", APPROVAL_ROLES),
    (r"^/api/execution/raise-request$", RUN_FAILURE_ROLES),
    (r"^/api/target-validation/run$", VALIDATION_ROLES),
    (r"^/api/target-validation/exceptions$", VALIDATION_ROLES),
    (r"^/api/target-validation/signoff$", VALIDATION_ROLES),
    (r"^/api/target-validation/exceptions/[^/]+/decision$", APPROVAL_ROLES),
    (r"^/api/validation-rules$", VALIDATION_ROLES),
    (r"^/api/validation-rules/[^/]+/[^/]+/(dry-run|submit)$", VALIDATION_ROLES),
    (r"^/api/validation-rules/[^/]+/[^/]+/decision$", APPROVAL_ROLES),
    (r"^/api/validation-rules/[^/]+/retire$", APPROVAL_ROLES),
    (r"^/api/wave-validation/exceptions$", VALIDATION_ROLES),
    (r"^/api/wave-validation/exceptions/[^/]+/decision$", APPROVAL_ROLES),
    (r"^/api/validation/scenarios$", VALIDATION_ROLES),
    (r"^/api/validation/scenarios/(suggest|[^/]+/adopt)$", VALIDATION_ROLES),
    (r"^/api/validation/scenarios/[^/]+/retire$", VALIDATION_ROLES),
    (r"^/api/validation/exceptions$", VALIDATION_ROLES),
    (r"^/api/validation/exceptions/[^/]+/decision$", APPROVAL_ROLES),
    (r"^/api/target-system/(configure|configure-azure-sql|infer|confirm|reopen|review-field|review-selected|review-all|move-table|schema)$", PLATFORM_ROLES),
    (r"^/api/target-mapping/gaps/raise$", CHANGE_REQUEST_ROLES),
    (r"^/api/target-mapping/propose$", DISCOVERY_ROLES),
    (r"^/api/target-mapping/preview$", TARGET_PREVIEW_ROLES),
    (r"^/api/target-mapping/adf-package$", TARGET_PREVIEW_ROLES),
    (r"^/api/target-mapping/stage-canonical$", TARGET_PREVIEW_ROLES),
    (r"^/api/target-mapping/create-scripts$", TARGET_PREVIEW_ROLES),
    (r"^/api/packages/generate$", TARGET_PREVIEW_ROLES),
    (r"^/api/validation/signoff-evidence/save$", TARGET_PREVIEW_ROLES),
    (r"^/api/target-mapping/(review-selected|revise-selected|[^/]+/review)$", APPROVAL_ROLES),
    (r"^/api/transformation-rules/preview$", DISCOVERY_ROLES),
    (r"^/api/transformation-rules/infer$", DISCOVERY_ROLES),
    (r"^/api/transformation-rules/(infer-preview|suggestion)$", DISCOVERY_ROLES),
    (r"^/api/transformation-rules$", DISCOVERY_ROLES),
    (r"^/api/derivations/(set|remove)$", APPROVAL_ROLES),
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


# Later-phase actions are blocked until every earlier pipeline stage is done: {route pattern: the stage the action belongs to}.
STAGE_GATES = [
    (r"^/api/target-mapping/(adf-package|stage-canonical|create-scripts)$", "07"),
    (r"^/api/packages/generate$", "07"),
    (r"^/api/git/(publish|pull-request)$", "08"),
    (r"^/api/git/pull-request/[^/]+/merge$", "08"),
    (r"^/api/data-plane/(setup|deployment-check)$", "08"),
    (r"^/api/data-plane/start$", "09"),
    (r"^/api/execution/(run|start-pipeline)$", "09"),
    (r"^/api/adf/(trigger|simulated-(deploy|trigger))$", "09"),
    (r"^/api/data-plane/promote$", "10"),
    (r"^/api/execution/runs/[^/]+/promote$", "10"),
    (r"^/api/target-validation/(run|signoff)$", "10"),
    (r"^/api/live-validation/run$", "10"),
]
_GATES = [(re.compile(pattern), stage) for pattern, stage in STAGE_GATES]


def gated_stage_for(method: str, path: str) -> Optional[str]:
    """The pipeline stage a state-changing route belongs to, when its earlier stages must be done first."""
    if method.upper() != "POST":
        return None
    for pattern, stage in _GATES:
        if pattern.match(path):
            return stage
    return None
