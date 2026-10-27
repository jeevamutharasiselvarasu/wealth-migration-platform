# XODUS — Wealth Migration Platform

## Business purpose
XODUS is a WealthAI Labs proof of concept for migrating wealth-management business data after an acquisition. The acquired firm's data must be understood and aligned to the target firm's canonical model with traceability, explainable recommendations, and human validation.

## Current P0 boundary
- Source system: CoreCap.
- Target system: Simplicity.
- Entities: Account, Client, Household only. Do not introduce Party.
- Mapping direction: source-to-canonical only.
- Execution: local, sample-file-based proof of concept.
- Human approval is required before mappings and transformations are treated as approved.
- Agents recommend and explain; deterministic services perform bounded checks.
- Uncertain classifications must be flagged for clarification, not silently guessed.

## Roles and personas (decided 2026-10-26)
XODUS is a platform: the migration team uses it to set up and run a migration; the platform itself, including the canonical model, is maintained by the platform owner. Roles are personas, and one user can hold several. The server enforces the active persona on every state-changing route (`ui/role_policy.py`); reviewer and requester identity always come from the authenticated session.

- **Platform Admin** maintains the platform: firm config, canonical model definition, users and their personas, demo data load and workflow replay. Holds every capability.
- **Migration Analyst** runs the migration up to approved rules: migration setup and scope, source environments, sample upload, file profiling and classification confirmation, mapping generation, approving or rejecting mappings and transformations (single, bulk, overrides), answering information requests, and raising canonical change requests.
- **Validation Specialist** runs the data plane after the transformation rules are approved: validation and reconciliation review, exceptions, wave evaluation. Read-only on the control plane.
- **Domain SME / Data Steward** owns the canonical model: decides canonical change requests, updates the kernel, resolves canonical meaning (answers information requests). Does not do migration set-up or mapping approval under this persona.
- **Normal user** holds Analyst + Validation Specialist by default. An admin can restrict a user to one of them. The Domain SME user also holds Analyst and Validation Specialist and switches persona to do migration work.
- Persona choices are limited to the personas assigned to the logged-in user; selecting another persona is a 403. The UI hides forms the active persona cannot use.

## Workflow
1. Persona selection (only assigned personas are offered).
2. Migration Setup: target/canonical configuration, source mode, and knowledge registration.
3. Migration Dashboard.
4. Source Discovery & Entity Inference: profile files, propose entity classification, and ask a human to confirm uncertain results.
5. Mapping Studio & Transformation Rules: source field profiling, mapping proposals, lineage, and supported transformation suggestions.
6. Mapping Review & Information Requests: resolve missing definitions and approve/reject proposals.
7. Validation & Exception Management: inspect relationship and data-quality findings and available reconciliation evidence.
8. Canonical Change Requests (`/canonical-changes`): when the migration needs something the canonical model does not provide, an Analyst raises a request; the Domain SME approves, rejects or defers it with a note.

## Canonical change requests
- Raised by Migration Analyst or Platform Admin: entity (Account/Household/Client), canonical field, change type (add field, change definition, add reference value, other), proposed change, reason.
- Decided by Domain SME or Platform Admin: approve, reject or defer; reject and defer require a note; a decided request cannot be decided again (a deferred one can).
- Audited with requester and decider from the session. Persisted in `canonical_change_requests`.
- P0 behavior: approved requests are a versioned backlog for the platform team. The screen does not edit the canonical model or the mapper.

## Inputs and knowledge
- Expected file formats include CSV, TSV, XLSX, XLSM, XLS, and JSON.
- Demo data (`demo_data/`) is kept separate from platform-provided knowledge (`platform_knowledge/`, `knowledge/`).
- Platform knowledge includes the canonical contract, source-mapping guidance and the wealth-management research (`platform_knowledge/wealth_management_research.md`).
- Source files and mapping recommendations must retain lineage to their origin.

## Out of scope unless explicitly approved
- Production target loading or cutover.
- Reverse mapping (target to canonical). Forward canonical-to-target mapping was approved by the user on 2026-10-27 for P0 against a labelled demo target spec (`platform_knowledge/simplicity_target_demo.yaml`): proposals, human approval, governed rules and a read-only preview only; no target loading.
- Actual wave/cohort assignment.
- Live Azure/ADF integration or claiming cloud connectivity without verification (the Azure phase is next; scaffolding exists).
- Autonomous approval of consequential mappings.
- Automatically applying approved canonical change requests to the canonical model.

## Engineering expectations
- Inspect the current branch and working tree before editing.
- Make the smallest correct change; avoid broad rewrites and unnecessary dependencies.
- Add focused regression tests.
- Run targeted tests first; run the full suite when appropriate.
- Report actual results and clearly state what was not verified.
- Preserve unrelated changes.
- Do not merge, push, or perform external actions without explicit authorization.

## Repository
https://github.com/jeevamutharasiselvarasu/wealth-migration-platform

Primary working branch (verify before use):
`feature/xodus-three-entity-pilot-integrated`

Useful repository references:
- `README.md`
- `docs/P0_FOUNDATION_CORRECTNESS.md`
- `docs/architecture.html`
- `config/firm_config.yaml` (canonical contract used at runtime)
- `platform_knowledge/canonical_contract.md` and `platform_knowledge/source_mapping_notes.md` (demo seed knowledge)
- `ui/role_policy.py` (role matrix, enforced server-side)
