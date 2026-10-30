# XODUS — Current Status Snapshot

> Compact handoff note, not a live repository status. Verify branch, HEAD, working tree and CI before relying on it. Last updated 2026-10-30 (see the 2026-10-30 section; earlier sections are older).

## Project
XODUS / XODUS-AMP — WealthAI Labs wealth-management business migration POC. Demo deadline 13 Nov 2026.

## Intended branch
`feature/xodus-three-entity-pilot-integrated`

## State at last update (verify)
- Pushed by the user: `origin` HEAD was `ee84434` (feat(canonical): change-request register), in sync with local at that time.
- CI (GitHub Actions, public API): run #627 on `ee84434` completed with success; #626 (`fd3af56`) and #625 (`57dbaa1`) also success.
- Local branch `chore/repo-cleanup` (not pushed) holds two cleanup commits on top of `ee84434`: `b318269` and `0440ac3`. Full suite on that branch: 178 passed locally (run in a copy). CI has not run on it.
- Working tree on the user's machine may show files as modified only because of CRLF/LF line-ending noise. Commit files with LF.

## Repo cleanup (branch `chore/repo-cleanup`)
- Removed modules nothing imported or tested: AI Foundry mapper, `ui/models.py`, `agents/logic/`, cutover and audit agents, compiler expressions/rulebook, registry JSONs, telemetry, sleeve engine, ReconcilerAgent and AUM ReconciliationEngine, the unused `agent_studio.html` template.
- Removed legacy sleeve/wave knowledge, duplicate fixtures (`test_files/`, `sample_data/`, `evaluation/`), stale cutover runbook and branch inventory, stale `data_plane/connectors/`.
- Moved `context/` to `docs/project/` and `architecture.html` to `docs/architecture.html` (rewritten to the current workflow). Firm profiles in `knowledge/firm/` rewritten. Added `platform_knowledge/wealth_management_research.md`.
- Kept on purpose: `infrastructure/` and the ADF/ADLS scaffolding (Azure is the next phase).

## Implemented this cycle
- Firm config (`config/firm_config.yaml`) stamped on setup and demo load; per-entity required-field RFIs; mapping kinds; relationship dispositions; evidence-based proposals (confidence capped, <70% needs individual review).
- Role model (see project brief): personas per user (`roles` in `demo_data/demo_users.json`), server-side policy in `ui/role_policy.py`, Analyst owns setup and mapping approval, Validator owns data-plane validation and wave evaluation, Domain SME owns canonical model, Admin holds all and alone runs demo load/replay.
- Forms a persona cannot use are hidden on setup, discovery and mapping pages.
- Confirm/reject classification shows a notice after redirect.
- Fixed: `kernel_specs` missing from the state store made the SME kernel update return 500.
- Canonical Change Requests screen (`/canonical-changes`): Analyst/Admin raise, SME/Admin approve/reject/defer; audited; backlog only.

## Verified in a real browser (2026-10-26, local server, before the cleanup branch)
- Normal user (Analyst + Validator personas): setup, upload, classification confirm, mapping generation, approval as Analyst; 403s as Validator.
- Domain SME: three personas offered; SME persona 403 on migration actions; kernel update works; approved a canonical change request.
- Analyst-only and Validator-only users see one persona; spoofing another persona is 403.
- Platform Admin: all four personas offered; setup editable; "Load demo data set" clicked (9 files, 2 knowledge assets, lands on the dashboard with a notice). Kernel update, workflow replay, mapping approval endpoint, raising and deciding a canonical change request were exercised by fetch calls from the logged-in page, not by clicks.
- Persistence across a server reload (Admin): approvals, rejections, deferred change request, file scope reviews and information requests were identical after a forced dev-server reload.
- Not verified: persistence across a full process stop/start; Excel/JSON uploads in the browser; the app after the cleanup commits (tests only).

## Known gaps and open decisions
- Saving the setup form with a new name reuses the same migration id, so earlier files/approvals carry over (shared state); an accumulated demo database needs a reset procedure.
- `REP-` PREFIX_FORMAT rule turns `R-01` into `REP-R-01` (decision pending).
- `household_accounts.csv` classifies as Account at low confidence; handled via the relationship-deferred disposition.
- Approved canonical change requests are not applied to the canonical model; no kernel edit form exists on the firm-knowledge page.
- Simplicity contract details (keys, types, required fields) remain seed values pending business-owner confirmation.
- Client consent tracking (negative/affirmative consent) is not modelled; see `platform_knowledge/wealth_management_research.md`.

## Important behavior requirements
- Bulk mapping approval is atomic and blocked by uncovered open RFIs or proposals under 70% confidence.
- Reviewer, requester and decider identity come from the authenticated session, never a form field.
- Only the Analyst (and Admin) approve or reject mappings; unsupported transformation families are rejected.
- File identity, content, migration association and lineage survive scope changes and reclassification.
- Approval and information-request state persist and survive restart/replay.
- Demo knowledge and migration demo data remain separate.

## Next smallest step
1. Push `chore/repo-cleanup` and check CI.
2. Decide the migration reset procedure and the `REP-` prefix rule.
3. Decide whether approved canonical change requests should ever update the canonical model.
4. Rehearse the end-to-end demo journey with the demo logins before 13 Nov; then start the Azure phase.


## 2026-10-30 update (verify against the repository before relying on it)
Commits after `3d2d491` (which is pushed and CI-green), all local and unpushed at the time of writing: target coverage split (`da637f4`), XODUS-009 validation scenarios and exception sign-off (`f96a19a`), XODUS-008 rule inference from text (`27124cf`), XODUS-006 cross-file analysis (`6851b95`), XODUS-010/011 draft package contract (`41828b6`), XODUS-012 simulated execution (`c60de2e`). Full local suite 248 passed; CI not yet run on these commits. Not browser-tested except an in-process page smoke (9 pages x 4 roles, all 200) against a copy of the real database.

- Target: separate simulated service; one-time configure, infer and confirm (Platform Admin); a Platform Admin can reopen it with a reason (withdraws the confirmed model until re-confirmed).
- Coverage (target mapping page): direct, transformation-assisted and effective, approved mappings only.
- Validation: Validation Specialist defines allow-listed scenarios (PRE evaluated; POST evaluated on simulated execution output) and proposes exceptions; Migration Analyst or Platform Admin decides, never the proposer. Effective status after approved exceptions is shown; the workflow bar still uses the stored run status.
- Rule inference: business-rule text becomes DRAFT governed rules (deterministic patterns, source sentence kept); approval still required.
- Cross-file analysis (discovery page): equivalent columns, relationship candidates, conflicts, duplicate files; counts only.
- Package: `package.yaml` v0.1-DRAFT (pydantic schema in `package_contract.py`, NOT agreed with the technical owner), versioned per migration, generated from approved configuration only. Simulated ADF zip with fictitious Azure values kept separately (`azure_simulated.py`).
- Execution: SIMULATED data plane (`execution_simulator.py`): telemetry, rule-linked problems, reconciled counts, guarded retry (infrastructure failures only, max 3). No Azure call is made.

Assumptions taken without a stakeholder answer: exception approver = Analyst/Admin; package contract drafted locally; `REP-` prefix rule left unchanged; approved canonical change requests remain backlog only; accounts `owner.demo` and `admin2.demo` are Platform Admin without demo load (only `admin2.demo` has a password hash).
Still open: real package template, Azure sandbox and ADF interface, identity-provider integration, managed-identity connection test and storage setup template, wave and delta, the old `/api/runs/{run_id}/export-package` exporter (hard-coded Azure values, out-of-scope entities).

## Honesty rule
Do not present this status as current without rechecking the repository and CI. Do not claim browser testing succeeded unless it was actually run.
