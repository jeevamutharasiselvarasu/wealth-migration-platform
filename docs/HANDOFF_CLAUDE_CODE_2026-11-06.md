# XODUS handoff to Claude Code (2026-11-06)

Branch: feature/xodus-three-entity-pilot-integrated (base a40827f). ~31 files changed, UNCOMMITTED, not pushed. Full suite: 492 passed. Nothing browser-tested.
Also read `docs/claude_memory/` (owner-stated context; start with preferences.md; note it says to work directly on this branch, no new branches).
Full decisions and change list: project doc `claude/XODUS_ADVISOR_REVIEW_2026-11-06.md` (Claude project "XODUS").

## Binding decisions (from advisor review; do not deviate)
1. Platform Admin sees only platform setup, never migration data.
2. Target mapping stage stays but shows only the inference from platform-level target setup (no setup UI in migration flow).
3. `xodus-amp` = private ADF-only repo; platform code lives in this repo. No per-migration repos.
4. Failed load stops the whole load (all or nothing).
5. Service-principal workaround stays (publish to git, `.ready` marker, log-table polling) until an ADF service principal exists.
6. Next phase, NOT now: default load sequence, waves/delta loads, entities beyond Account/Client/Household.
7. Deployment (App Service etc.) only after all other changes are done.
8. Agents recommend, deterministic services check, humans approve. No LLM in platform.

## Implemented (uncommitted)
- `ui/role_policy.py`, `ui/auth.py`, `ui/app.py`: Admin restricted to platform pages (`ADMIN_VIEW_PREFIXES`, `admin_may_view`); GET to migration pages redirects to /setup; demo load = Migration Analyst; git publish analyst-only.
- New `/target-setup` page (`ui/templates/target_setup.html`); `/target-mapping` now read-only "Inferred target model".
- `adf_package.py`: `all_or_nothing()` transaction wrapper for merges.
- `sql_target.run_log_status()`; git publish blocks if `xodus_run_log` verifiably missing, warns if unverifiable.
- `.ready` marker now carries `batch_id` + acquisition (source firm, target).
- New `control_plane/services/validation_signoff.py` + `POST /api/target-validation/signoff` (needs PASS, comment, latest run) + reconciliation.html form; test `tests/test_validation_signoff.py`.
- Many tests updated for the new role matrix.

## Deferred items (status 2026-11-06, Claude Code; owner decisions in brackets)
- C1/C3 DONE: `/platform-knowledge` (service `platform_knowledge.py`): built-in `platform_knowledge/` files plus documents the
  Platform Admin or Domain SME adds (text stored in the platform DB); read-only for migration users. /firm-knowledge is workspace-only
  and no longer admin-viewable.
- A3 DONE: migration setup fills target system, owner and canonical contract (and the Azure source storage) from platform setup,
  read-only, and saving enforces them. /azure-setup, /github-setup, /target-setup are Platform Admin only; stage 01 and the setup hub
  no longer link to them for migration users.
- G4 DONE [compare against the target's reference lists]: stage 10 check VALUES_IN_REFERENCE_LIST (count only) for every mapped target
  column with a code list in the confirmed target model. Azure SQL inference currently yields no code lists, so it runs on targets
  whose model has them (e.g. the simulated target) until lookup tables are read as lists.
- C5 DONE [ALTER script approved]: staging tables carry xodus_migration_id, xodus_run_id, xodus_source_file (Copy additionalColumns,
  $$FILEPATH) and xodus_loaded_utc; `sql/03_trace_columns.sql` adds them to older staging tables (add-only, rerunnable). Data Factory has
  no source row number, so the trace is file + run + load time.
- F2 DONE [raise to vendor / RFI only]: on stage 09 each failed step can be raised as an information request (entity picked by the user,
  question built server-side from the stored diagnosis); answered in stage 04 like any other request.
- D4 DONE [versioned release + notice]: an applied canonical change is live only in the workspace that raised it; the SME/Admin publishes
  numbered releases; each workspace stays on its pinned release (new workspaces start on the latest) and shows a notice until a
  Migration Analyst adopts. Overlay entries applied before this change are unreleased until the first release is published.
- F4 BLOCKED: direct ADF API trigger needs an ADF service principal.
- Still open: /canonical-changes lists the open workspace's change requests and is admin-viewable (pre-existing).

## Next steps
1. Run the app (`docker compose up --build` or `run_local.bat`) and browser-test: /target-setup as Admin, admin redirect, sign-off form as Validation Specialist.
2. Review `git diff`, then commit on a focused branch/PR. Do not merge or push without explicit approval.
3. Pick deferred items above in order: C1/C3, A3, G4.

## Walk-through on the laptop (2026-11-06, Claude Code)
App run locally (uvicorn, SQLite, isolated data dir); admin, analyst and validator accounts registered and approved through the UI routes.
- Admin GET to migration pages redirects to /setup: OK. /target-setup (admin) and read-only /target-mapping (analyst): render.
- Found and fixed: the Platform Admin could still see migration data. `/` (Migration Dashboard) was on the admin allow-list, and every
  platform page drew the migration sidebar (workspace name/id, pipeline stages). Now `/` redirects the admin to /setup, the page frame
  shows a "Platform setup" sidebar for the admin (`platform_only` in `render_template_with_context`), and the setup hub/tabs hide
  workspace configuration from the admin. Covered in `tests/test_role_policy.py`.
- Sign-off form needs a PASS run (needs a real SQL target), so it is covered by a new route test in `tests/test_validation_signoff.py`
  (form shown to Validation Specialist only, comment required, SIGNED OFF shown after).
- Still open for C1/C3: /firm-knowledge is admin-viewable and lists workspace knowledge assets.
- On Windows, run tests with `PYTHONUTF8=1` (several tests read files without an explicit encoding).

## Env notes
Docker could not be built in the Claude cloud sandbox (Docker Hub blocked) or the device VM (no docker). Audio transcription work (`audio/`) is separate and can be ignored.
