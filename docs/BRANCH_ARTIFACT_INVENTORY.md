# Branch Artifact Inventory and Removal Assessment

- Branch: `feature/xodus-three-entity-pilot-integrated`
- Audited tree snapshot: `e781285e32c5eea27e7771271cfcee7f20140e1e`
- Method: Git recursive tree (**123 tracked files**; blob entries counted separately from tree/directory entries), plus inspection of `ui/app.py`, active templates, P0 boundary tests, README, demo/test fixtures, scripts, `.gitignore`, Compose configuration, and Python import/test references.
- This report is committed separately from the audited tree snapshot; update the snapshot/count after any subsequent repository changes.
- Scope baseline: P0 source-to-canonical mapping for Account, Household, Client. Target loading, reverse mapping, actual wave assignment, cutover, sleeve/allocation processing, and production connectors are deferred.
- **Removal decisions are evidence-based and scope-limited.** Source modules still require import, runtime route, test, deployment, and documentation checks before removal.

## Status legend

- **KEEP** — clear active P0, build, test, demo, or infrastructure role.
- **KEEP / REVIEW** — plausible active use; verify before changing.
- **REMOVE CANDIDATE** — appears legacy, duplicated, or outside current P0; remove only after dependency confirmation.
- **REMOVE FROM GIT IF PRESENT** — generated/local artifact not in the current audited tree. No such file should be inferred to exist from an older diff.

## Findings that change the cleanup plan

1. The audited recursive tree has **123 tracked file blobs** (directory entries are counted separately) and is not truncated.
2. The 11 files under `ui/static/components/` were removed after checking all active templates and the base template: none loads these files, and the templates contain no references to their paths. This removes orphaned assets, not the active server-rendered workflow.
3. The current tree does **not** contain `App_Data/controlplane.db`, a `dist/` package, generated `App_Data/Storage/` outputs, or tracked `__pycache__/` files. Do not schedule deletions for files absent from this snapshot.
4. `ui/app.py` currently imports the orchestrator, schema mapper, cutover agent, audit agent, storage manager, profiler, MCL validation, source adapter, entity inference, wave rules, ADF client/exporter, and database layer. These modules are **not safe to remove solely because they sound legacy**.
5. `docker-compose.yml` references both Dockerfiles, `App_Data`, and `data_plane/storage`; `README.md` references scripts and multiple legacy capabilities. Deployment/docs must be aligned before removing those references.
6. The main sidebar no longer exposes the old standalone Knowledge Studio or HITL route, but route handlers and templates may still exist. Treat them as candidates for consolidation, not proof of dead code.
7. Sample CSV sets overlap across `demo_data/source_files/`, `knowledge/sample_file/`, `sample_data/p0_mapping_tests/`, `test_files/`, and `data_plane/storage/`. Keep only the canonical demo fixtures and test fixtures actually referenced by tests/scripts; do not delete them before checking those references.

## Complete inventory

### Repository / deployment / docs

| Path | Disposition | Reason / action |
|---|---|---|
| `.github/workflows/tests.yml` | KEEP | CI workflow |
| `.gitignore` | KEEP | Ignore generated/local outputs; ensure rules cover `*.py[cod]`, `__pycache__/`, local DBs and runtime storage |
| `Dockerfile.control_plane` | KEEP / REVIEW | Referenced by Compose; verify image starts the current app |
| `Dockerfile.data_plane` | REMOVED | Retired worker image depended on the legacy cohort orchestrator; Compose now runs only the control-plane demo app |
| `README.md` | KEEP — UPDATED | Documents the sample-file P0 boundary and removal of legacy connectors/target adapters |
| `architecture.html` | KEEP / REVIEW | Architecture reference; align to generic P0 flow and label future-state features |
| `docker-compose.yml` | KEEP — UPDATED | Runs the control-plane demo app only; legacy worker and fixture mounts removed |
| `requirements.txt` | KEEP — REVIEW DEPENDENCIES | Some packages may only serve deferred connectors; remove dependencies only after import scan |
| `pytest.ini` | KEEP | Test configuration |
| `docs/P0_FOUNDATION_CORRECTNESS.md` | KEEP — UPDATE | P0 correctness decisions |
| `docs/runbook_mock_to_cutover.md` | REMOVE CANDIDATE / REWRITE | Cutover-oriented legacy runbook conflicts with P0 if presented as implemented; retain only if explicitly labeled future-state |

### Agent modules

| Path | Disposition | Reason / action |
|---|---|---|
| `agents/__init__.py` | KEEP | Python package |
| `agents/discovery/__init__.py` | KEEP | Python package |
| `agents/discovery/ai_foundry_mapper.py` | KEEP / REVIEW | Potential AI integration; no AI agent is currently part of P0. Retain only if referenced or clearly future-state |
| `agents/discovery/extractor_agent.py` | REMOVED | Removed with the legacy cohort orchestrator and custodian-specific connectors |
| `agents/discovery/schema_mapper_agent.py` | KEEP | Imported by `ui/app.py`; mapping logic |
| `agents/logic/__init__.py` | KEEP | Python package |
| `agents/logic/compliance_validator_agent.py` | KEEP / REVIEW | Trace import and tests; compliance checks may be future-state |
| `agents/logic/data_quality_agent.py` | KEEP / REVIEW | Data-quality functionality may support validation; trace usage |
| `agents/logic/sleeve_math_agent.py` | REMOVED | Wrapper had no references in the scanned application, test, script, or infrastructure files. The separate `SleeveMathEngine` and its tests remain untouched.
| `agents/operational/__init__.py` | KEEP | Python package |
| `agents/operational/audit_agent.py` | KEEP | Imported by `ui/app.py`; audit responsibility |
| `agents/operational/cutover_agent.py` | KEEP / REVIEW | Imported and instantiated by `ui/app.py`; cutover feature is deferred, so remove only after decoupling active import |
| `agents/operational/loader_agent.py` | REMOVED | No references found in the scanned application, test, script, or infrastructure files; target loading is deferred from P0.
| `agents/operational/reconciler_agent.py` | KEEP / REVIEW | Reconciliation is in P0; confirm active implementation path vs `data_plane/validation/reconciler.py` |
| `agents/orchestrator.py` | REMOVED | Legacy Sleeve/Position/Transaction cohort path retired; UI endpoint now returns an explicit P0-boundary response |

### Control plane

| Path | Disposition | Reason / action |
|---|---|---|
| `control_plane/__init__.py` | KEEP | Python package |
| `control_plane/compiler/expressions.py` | KEEP / REVIEW | Rule execution/compiler dependency |
| `control_plane/compiler/inference.py` | KEEP / REVIEW | Inference/compiler helper; trace usage |
| `control_plane/compiler/rulebook.py` | KEEP / REVIEW | Mapping rule representation; trace usage |
| `control_plane/db.py` | KEEP | Imported by `ui/app.py`; persistence |
| `control_plane/kernel/wealth_kernel.py` | KEEP / REVIEW | Core kernel; confirm active route/test usage |
| `control_plane/registry/crosswalk_catalog.json` | KEEP / REVIEW | Reusable mapping/crosswalk config; confirm loader |
| `control_plane/registry/source_asset_inventory.json` | KEEP / REVIEW | Source inventory config; confirm loader |
| `control_plane/services/__init__.py` | KEEP | Python package |
| `control_plane/services/adf_client.py` | KEEP / REVIEW | Imported by `ui/app.py`; cloud calls should remain optional in sample mode |
| `control_plane/services/adf_exporter.py` | KEEP / REVIEW | Imported by `ui/app.py`; ADF export is future/optional in local P0 |
| `control_plane/services/entity_inference.py` | KEEP | P0 file inference |
| `control_plane/services/mcl_validation.py` | KEEP | MCL relationship validation for Account/Household/Client |
| `control_plane/services/profiler.py` | KEEP | Imported by `ui/app.py`; profiling supports discovery |
| `control_plane/services/source_file_adapter.py` | KEEP | Imported by `ui/app.py`; source file parsing |
| `control_plane/services/wave_rules.py` | KEEP / REVIEW | Imported by `ui/app.py` for displaying condition catalogue. Keep condition definitions only; no P0 wave assignment/execution |

### Data plane

| Path | Disposition | Reason / action |
|---|---|---|
| `data_plane/__init__.py` | KEEP | Python package |
| `data_plane/adapters/__init__.py` | REMOVED | Empty package removed after target adapters were retired |
| `data_plane/adapters/base_adapter.py` | REMOVED | No active P0 target-adapter interface remains |
| `data_plane/adapters/orion_eclipse_adapter.py` | REMOVED | Target loading is deferred; no active P0 imports found |
| `data_plane/adapters/salesforce_sparc_adapter.py` | REMOVED | Target loading is deferred; no active P0 imports found |
| `data_plane/connectors/amplify_connector.py` | REMOVED | Removed after retiring `ExtractorAgent` and the legacy cohort pipeline |
| `data_plane/connectors/blackdiamond_connector.py` | REMOVED | Removed after retiring `ExtractorAgent` and the legacy cohort pipeline |
| `data_plane/core/sleeve_engine.py` | REMOVE CANDIDATE | Sleeve/allocation execution is deferred |
| `data_plane/storage/amplify_positions.csv` | REMOVED | Legacy mock output removed with generator and connector chain |
| `data_plane/storage/amplify_sleeves.csv` | REMOVED | Legacy mock output removed with generator and connector chain |
| `data_plane/storage/blackdiamond_performance.csv` | REMOVED | Legacy mock output removed with generator and connector chain |
| `data_plane/storage_manager.py` | KEEP / REVIEW | Imported by `ui/app.py`; separate runtime storage from committed fixture data |
| `data_plane/telemetry/instrumentation.py` | KEEP / REVIEW | Cross-cutting telemetry; trace runtime use |
| `data_plane/validation/reconciler.py` | KEEP / REVIEW | Reconciliation supports P0; check for duplicate reconciler implementation |

### Demo configuration and source files

| Path | Disposition | Reason / action |
|---|---|---|
| `demo_data/demo_migration.json` | KEEP | Demo loader input |
| `demo_data/demo_users.json` | KEEP | Demo-mode user configuration; password must remain hashed/configured |
| `demo_data/knowledge/canonical_contract.md` | KEEP | Canonical contract demo knowledge |
| `demo_data/knowledge/source_mapping_notes.md` | KEEP | Source mapping context |
| `demo_data/source_files/account_registration.csv` | KEEP | Account demo fixture |
| `demo_data/source_files/account_relationships.csv` | KEEP | Account relationship demo fixture |
| `demo_data/source_files/accounts.csv` | KEEP | Account demo fixture |
| `demo_data/source_files/client_contacts.csv` | KEEP | Client demo fixture |
| `demo_data/source_files/client_tax_profile.csv` | KEEP | Client demo fixture |
| `demo_data/source_files/clients.csv` | KEEP | Client demo fixture |
| `demo_data/source_files/household_accounts.csv` | KEEP | Household demo fixture |
| `demo_data/source_files/household_members.csv` | KEEP | Household demo fixture |
| `demo_data/source_files/households.csv` | KEEP | Household demo fixture |

### Knowledge and rules

| Path | Disposition | Reason / action |
|---|---|---|
| `knowledge/amplify_dictionary.json` | REMOVE CANDIDATE / REWRITE | Legacy source-specific dictionary; retain only if intentionally used as a sample dictionary or move into generic source knowledge |
| `knowledge/discovery_questionnaire.json` | KEEP / REVIEW | Confirm current setup/discovery code consumes it |
| `knowledge/entity_inference_rules.json` | KEEP | Editable inference rules used by P0 inference |
| `knowledge/firm/corecap-profile.md` | KEEP / REVIEW | Firm-specific profile may be legacy demo content; keep only if loaded as knowledge for current demo |
| `knowledge/firm/simplicity-profile.md` | KEEP / REVIEW | Firm-specific profile may be legacy demo content; keep only if loaded as knowledge for current demo |
| `knowledge/migration/MIG-CORECAP-2026/mappings/MAP-SLEEVE-001.md` | REMOVE CANDIDATE | Sleeve mapping outside P0; retain only as explicitly labeled future-state reference |
| `knowledge/migration/MIG-CORECAP-2026/migration-overview.md` | KEEP / REVIEW | Migration-specific legacy document; verify whether loaded by current knowledge flow |
| `knowledge/migration/MIG-CORECAP-2026/validations/VAL-AUM-DRIFT-001.md` | REMOVE CANDIDATE / REVIEW | AUM drift validation is not part of current three-entity mapping flow unless explicitly used as a business rule |
| `knowledge/migration/MIG-CORECAP-2026/waves/WAVE-PLAN-001.md` | REMOVE CANDIDATE | P0 must not assign waves; keep only as future-state design reference outside active demo knowledge |
| `knowledge/p0_wealth_entity_model.md` | KEEP | P0 entity model reference |
| `knowledge/sample_file/accounts.csv` | REMOVED | Duplicate legacy fixture removed; canonical Account fixture remains under `demo_data/source_files/` |
| `knowledge/sample_file/amplify_sleeves.csv` | REMOVED | Out-of-scope sleeve fixture removed |
| `knowledge/sample_file/terminated_accounts.csv` | REMOVED | Legacy terminated-account fixture removed; P0 exception handling remains covered by mapping/validation tests |
| `knowledge/sample_file/transactions.csv` | REMOVED | Out-of-scope transaction fixture removed |
| `knowledge/source_profile.json` | KEEP / REVIEW | Legacy source profile; confirm whether discovery consumes it |

### Sample mapping tests

| Path | Disposition | Reason / action |
|---|---|---|
| `sample_data/p0_mapping_tests/README.md` | KEEP | Explains mapping approval/rejection fixtures |
| `sample_data/p0_mapping_tests/account_master_approve.csv` | KEEP | Mapping approval test fixture |
| `sample_data/p0_mapping_tests/account_master_review.csv` | KEEP | Mapping review/rejection fixture |
| `sample_data/p0_mapping_tests/account_name_address.csv` | KEEP | Account source fixture |
| `sample_data/p0_mapping_tests/account_type.csv` | KEEP | Account source fixture |
| `sample_data/p0_mapping_tests/client_master.csv` | KEEP | Client source fixture |
| `sample_data/p0_mapping_tests/client_model.csv` | KEEP / REVIEW | Check whether this duplicates `client_master.csv` or tests a separate canonical shape |
| `sample_data/p0_mapping_tests/household_mapping.csv` | KEEP | Household mapping fixture |
| `sample_data/p0_mapping_tests/household_master.csv` | KEEP | Household source fixture |

### Scripts

| Path | Disposition | Reason / action |
|---|---|---|
| `scripts/create_test_sample_files.py` | REMOVED | Unreferenced legacy generator removed with its out-of-scope generated fixtures; canonical demo and mapping-test fixtures remain |
| `scripts/generate_mock_data.py` | REMOVED | Legacy data generator retired with the cohort orchestrator and its tests |

### Additional test inputs

| Path | Disposition | Reason / action |
|---|---|---|
| `test_files/accounts.csv` | REMOVE CANDIDATE | Likely overlaps other account fixtures; confirm references |
| `test_files/amplify_sleeves.csv` | REMOVED | Out-of-scope sleeve fixture removed |
| `test_files/clients.csv` | REMOVE CANDIDATE / REVIEW | Likely overlaps demo and mapping-test client fixtures; keep if tests reference it |
| `test_files/households.csv` | REMOVE CANDIDATE / REVIEW | Likely overlaps other household fixtures; keep if tests reference it |
| `test_files/terminated_accounts.csv` | REMOVED | Legacy terminated-account fixture removed |
| `test_files/transactions.csv` | REMOVED | Out-of-scope transaction fixture removed |

### Tests

| Path | Disposition | Reason / action |
|---|---|---|
| `tests/__init__.py` | KEEP | Python package |
| `tests/test_entity_inference.py` | KEEP | P0 inference behavior |
| `tests/test_knowledge_asset_extraction.py` | KEEP / REVIEW | Keep if current knowledge upload/extraction is supported |
| `tests/test_mcl_api.py` | KEEP | MCL API coverage |
| `tests/test_mcl_flow.py` | KEEP | MCL workflow coverage |
| `tests/test_orchestrator.py` | REMOVED | Obsolete legacy pipeline tests replaced by P0/deferred-execution boundary tests |
| `tests/test_p0_entity_scope_contract.py` | KEEP | P0 scope contract |
| `tests/test_p0_ui_boundary.py` | KEEP | Current UI/workflow regression tests |\n| `tests/test_deferred_execution_boundaries.py` | KEEP | Confirms legacy pipeline retirement, no wave assignment, and honest ADF mock behavior |
| `tests/test_pilot_account_mapping.py` | KEEP | Account mapping regression tests |
| `tests/test_pilot_households_clients.py` | KEEP | Household and Client mapping regression tests |
| `tests/test_reconciliation.py` | KEEP | Validation/reconciliation coverage |
| `tests/test_sleeve_math.py` | REMOVE CANDIDATE | Tests a deferred capability; remove with sleeve math only after import/test-suite dependencies are reconciled |
| `tests/test_source_file_adapter.py` | KEEP | Source-file parsing behavior |

### UI backend and templates

| Path | Disposition | Reason / action |
|---|---|---|
| `ui/__init__.py` | KEEP | Python package |
| `ui/app.py` | KEEP | Main application and route handlers |
| `ui/auth.py` | KEEP | Session authentication and persona boundaries |
| `ui/models.py` | KEEP / REVIEW | Pydantic models; trace usage |
| `ui/state.py` | KEEP | Workspace state |
| `ui/static/components/discovery.js` | REMOVED | No active template or base-shell script reference; orphaned asset removed |
| `ui/static/components/execution.js` | REMOVED | No active template or base-shell script reference; orphaned asset removed |
| `ui/static/components/ingestion.js` | REMOVED | Active ingestion is server-rendered; no template or base-shell script reference |
| `ui/static/components/login.js` | REMOVED | Login template has no external component-script reference |
| `ui/static/components/migration-knowledge.js` | REMOVED | Knowledge UI is server-rendered; no template or base-shell script reference |
| `ui/static/components/migration-setup.js` | REMOVED | Migration setup template has no external component-script reference |
| `ui/static/components/reconciliation.js` | REMOVED | Reconciliation template has no external component-script reference |
| `ui/static/components/role-selection.js` | REMOVED | Persona selection is server-rendered; no external component-script reference |
| `ui/static/components/rule-editor.js` | REMOVED | Mapping studio template has no external component-script reference |
| `ui/static/components/rulebook-editor.js` | REMOVED | No active template or base-shell script reference; orphaned asset removed |
| `ui/static/components/workspace-shell.js` | REMOVED | Base template does not load the shell component; orphaned asset removed |
| `ui/static/prototype.css` | REMOVE CANDIDATE | Prototype styling file; keep only if prototype page or active template loads it |
| `ui/static/prototype.html` | REMOVE CANDIDATE | Standalone prototype; not part of named P0 navigation unless explicitly linked |
| `ui/static/prototype.js` | REMOVE CANDIDATE | Prototype behavior; remove with prototype page if unreferenced |
| `ui/templates/agent_pipeline.html` | KEEP | Mapping Studio and mapping review |
| `ui/templates/base.html` | KEEP | Active global navigation/layout |
| `ui/templates/dashboard.html` | KEEP | Migration overview |
| `ui/templates/entity_card.html` | KEEP / REVIEW | Check Jinja include/render usage |
| `ui/templates/entity_menu_card.html` | KEEP / REVIEW | Check Jinja include/render usage |
| `ui/templates/entity_sample_files.html` | KEEP | Source discovery and entity inference |
| `ui/templates/entity_selection.html` | REMOVE CANDIDATE | Route redirects to source discovery; remove template if no direct include/render references |
| `ui/templates/firm_knowledge.html` | REMOVE CANDIDATE / REVIEW | Standalone Knowledge Studio is not in P0 sidebar, but `/firm-knowledge` route still renders it; consolidate knowledge into Migration Setup before removal |
| `ui/templates/hitl_queue.html` | REMOVE CANDIDATE | Legacy wave/cutover HITL queue; remove after verifying no active route renders it |
| `ui/templates/knowledge_ingestion.html` | REMOVE CANDIDATE / REVIEW | Not a named P0 stage; check route/include references |
| `ui/templates/knowledge_studio.html` | REMOVE CANDIDATE | Separate Knowledge Studio is not in the current P0 workflow; verify route/include references |
| `ui/templates/login.html` | KEEP | Login |
| `ui/templates/migration_setup.html` | KEEP | Migration configuration and knowledge/source references |
| `ui/templates/persona_selection.html` | KEEP | Separate persona-selection stage |
| `ui/templates/questionnaire.html` | REMOVE CANDIDATE / REVIEW | Legacy questionnaire screen; keep only if an active route renders it |
| `ui/templates/reconciliation.html` | KEEP | Mapping evidence, lineage and validation/reconciliation |

### Infrastructure

| Path | Disposition | Reason / action |
|---|---|---|
| `infrastructure/adf/pipelines/adf_pipeline_account_transaction.yml` | REMOVE CANDIDATE / REVIEW | Transaction pipeline is outside P0 and uses a legacy entity name; retain only as explicitly labeled future-state ADF example |
| `infrastructure/bicep/main.bicep` | KEEP / REVIEW | Infrastructure as code; verify it provisions the documented current environment |
| `infrastructure/bicep/modules/resources.bicep` | KEEP / REVIEW | Resource module referenced by Bicep entry point |

## Ordered removal queue

### Tier 1 — high-confidence candidates, after reference scan
- `ui/static/prototype.html`, `ui/static/prototype.css`, `ui/static/prototype.js` as a unit, if nothing links to them.
- `ui/templates/entity_selection.html` because the route redirects to discovery, if no includes render it.
- `ui/templates/hitl_queue.html` if no route renders it.
- `data_plane/core/sleeve_engine.py` and `tests/test_sleeve_math.py` remain a separate legacy sleeve-math group. They are still internally referenced by tests; remove only with an explicit scope decision and coordinated test changes.
- COMPLETED: removed both named target adapters, the unused base adapter interface, and both custodian connectors after retiring the only dependent extractor/orchestrator chain.
- COMPLETED: removed the three tracked legacy connector CSV outputs together with `scripts/generate_mock_data.py` and the obsolete orchestrator tests.

### Tier 2 — consolidate/decide
- The current tree contains no `ui/templates/knowledge_studio.html`, `ui/templates/questionnaire.html`, `ui/templates/entity_selection.html`, or `ui/templates/hitl_queue.html`; do not queue deletions for paths that are absent. `ui/templates/firm_knowledge.html` remains an active route target and must be retained until its knowledge-registration behavior is intentionally consolidated into Migration Setup.
- Duplicate sample data under `knowledge/sample_file/` and `test_files/`: keep canonical demo data under `demo_data/source_files/` and purpose-built mapping fixtures under `sample_data/p0_mapping_tests/`; update references before deleting duplicates.
- Legacy firm/migration-specific knowledge documents: retain only what the current knowledge loader actually registers or what the demo intentionally showcases.

### Tier 3 — do not delete without explicit architecture decision
- `control_plane/services/wave_rules.py`: currently imported to display condition-based wave criteria; retain the condition catalogue while wave assignment remains deferred.
- `agents/operational/cutover_agent.py`: currently imported by the UI; decouple route/import before removing the deferred cutover capability.
- `control_plane/services/adf_client.py` and `control_plane/services/adf_exporter.py`: currently imported; retain optional integration boundary or remove with corresponding route/tests/deployment changes as a coordinated change.
- Dockerfiles, Compose, Bicep, ADF pipeline: update together rather than removing isolated infrastructure files.

## Required final verification before any deletions
1. Search all source, tests, templates, scripts, Dockerfiles and docs for each candidate path/module.
2. Run the complete test suite after each coherent removal group.
3. Run a clean local startup with demo mode enabled and verify Persona → Setup → Dashboard → Discovery → Mapping → Review → Validation.
4. Confirm sample demo loader registers all nine files and knowledge docs.
5. Confirm no target load, reverse mapping or wave assignment is accidentally exposed.
6. Re-run recursive tree inventory and confirm generated outputs are not tracked.


## Cleanup follow-up — 2026-10-21

Current verified branch head before this inventory update: `a46b99b395cbd0b7c2c73d9960d2b38db11da154`. The recursive tree contains **110 tracked files** (counting blobs, not directory tree entries).

Completed cleanup:
- The active rendered page templates are `login.html`, `persona_selection.html`, `migration_setup.html`, `dashboard.html`, `firm_knowledge.html`, `entity_sample_files.html`, `agent_pipeline.html`, and `reconciliation.html`.
- `entity_card.html` and `entity_menu_card.html` were removed after confirming no active route/template references.
- The prototype trio (`ui/static/prototype.html`, `.css`, `.js`) and 11 unreferenced files under `ui/static/components/` were removed after checking active template references.
- README content was aligned to the three-entity source-to-canonical P0 workflow.
- CI for the preceding inventory commit completed successfully: [Python tests, run 471](https://github.com/jeevamutharasiselvarasu/wealth-migration-platform/actions/runs/39608188188). The subsequent cleanup commits require their own CI confirmation.

Second-pass dependency findings:
- The current `ui/templates/` tree does not contain `knowledge_studio.html`, `questionnaire.html`, `entity_selection.html`, or `hitl_queue.html`; remove these from cleanup queues rather than attempting deletions of absent paths.
- `/firm-knowledge` still renders `firm_knowledge.html`; preserve it until the knowledge-registration flow is intentionally consolidated.
- `agents/logic/sleeve_math_agent.py` was removed after a scan of application, test, script, and infrastructure Python files found no reference to the wrapper. `data_plane.core.sleeve_engine.SleeveMathEngine` remains used by `tests/test_sleeve_math.py` and was not removed.
- `agents/operational/loader_agent.py` was removed after a scan of application, test, script, and infrastructure Python files found no reference to the wrapper. No target-loading routes or execution modules were changed.
- `scripts/create_test_sample_files.py` and legacy `test_files/` sleeve, terminated-account and transaction fixtures were removed. P0 Account/Client/Household files in `test_files/` were retained. The next coordinated pass removed `scripts/generate_mock_data.py`, its tracked `data_plane/storage/` outputs, and the legacy cohort tests together.
- `demo_data/source_files/` contains the nine current Account, Household and Client sample files. `sample_data/p0_mapping_tests/` has separate approval/rejection fixtures described by its README. Keep these distinct.
- The four redundant/out-of-scope CSVs under `knowledge/sample_file/` were removed; the directory no longer serves as a competing source fixture catalog. The legacy connector/sample-data group is now removed. `data_plane/storage_manager.py` remains because the active UI uses it for file persistence; it is separate from the deleted tracked legacy CSV directory.

Do not delete the active ADF/wave/cutover boundaries or Docker/Bicep files in isolation. Current P0 must remain local/demo-capable and must not assign actual waves, load target data, or run cutover.


### Cleanup follow-up — 2026-10-21 (legacy fixture group)

- Removed `scripts/create_test_sample_files.py` and the legacy `test_files/amplify_sleeves.csv`, `test_files/terminated_accounts.csv`, and `test_files/transactions.csv` fixtures. Retained `test_files/accounts.csv`, `test_files/clients.csv`, and `test_files/households.csv` as the P0-shaped inputs.
- Removed all four redundant/out-of-scope CSVs under `knowledge/sample_file/`; the canonical nine-file demo pack remains in `demo_data/source_files/`, and approval/rejection test cases remain in `sample_data/p0_mapping_tests/`.
- The recursive tree snapshot now has 110 tracked file blobs before this inventory update (down from 131). The legacy cohort route is explicitly disabled; entity inference, mapping, lineage, human approval, and validation routes remain the P0 workflow.
- The legacy dependency chain has now been removed as a coordinated change: the UI no longer imports or instantiates `AgenticOrchestrator`; `/api/pipeline/run` returns HTTP 409 with directions to the P0 Mapping Studio flow; the old orchestrator, extractor, Amplify/Black Diamond connectors, target adapters, mock-data generator, tracked legacy storage CSVs, worker Dockerfile, and Compose worker service were removed. `tests/test_deferred_execution_boundaries.py` preserves the wave-disabled and ADF-mock assertions.


### Cleanup follow-up — 2026-10-21 (legacy connector dependency group)

- Removed the legacy cohort orchestration chain and its dependencies: `agents/orchestrator.py`, `agents/discovery/extractor_agent.py`, both custodian connectors, both named target adapters plus their unused base interface, `scripts/generate_mock_data.py`, three tracked legacy storage CSVs, and `Dockerfile.data_plane`.
- Removed the `data_plane_worker` Compose service and its `data_plane/storage` bind mounts. The supported local container entry point is the control-plane UI; uploaded file persistence continues through `data_plane/storage_manager.py` and `App_Data/Storage`.
- The old `/api/pipeline/run` endpoint is retained only as a compatibility response: it now requires a signed-in session and returns HTTP 409 explaining that the legacy cohort pipeline is retired and directing users to Source Discovery, Mapping Studio, and Mapping Review for Account, Household, and Client.
- `tests/test_orchestrator.py` was replaced by `tests/test_deferred_execution_boundaries.py`, retaining the checks that wave conditions are displayed but not assigned and that ADF mock mode never claims a real run. The new test also guards the legacy route boundary and P0 mapping route presence.
- The current recursive tree snapshot contains 110 tracked file blobs before this inventory update. The nine demo source files, dedicated mapping approval/rejection fixtures, inference rules, canonical model, and active P0 workflow code are retained.


## Phase 1 baseline — local end-to-end workflow (2026-10-23)

- Branch HEAD reviewed: `e781285e32c5eea27e7771271cfcee7f20140e1e` (`Align mapping approval contract test with transformation registry`).
- Current recursive tree: `e781285e32c5eea27e7771271cfcee7f20140e1e`; 123 tracked file blobs. This section supersedes the older audited-tree snapshot for current workflow findings; it does not authorize removal by itself.
- CI evidence: workflow run [#580](https://github.com/jeevamutharasiselvarasu/wealth-migration-platform/actions/runs/39837798840) completed successfully, including dependency installation, Python compilation and `pytest -q`. This verifies the automated Python suite at that commit; it does **not** verify a browser-driven journey.
- Current UI entry sequence in `ui/app.py`: login → persona selection → migration setup → dashboard; `/api/demo/load` redirects into Source Discovery. The primary P0 workflow then uses `/entity-sample-files` for upload/inference review, `/api/entities/map-approved-files` to create mapping proposals, `/agent-pipeline` for single/bulk review and information requests, and `/reconciliation` for validation evidence.
- Existing foundations to preserve: `WorkspaceStateStore` stores uploaded files and rehydrates from SQLite/event history; `source_file_adapter.py` reads CSV/TSV/XLSX/XLSM/XLS/JSON; mapping review and information requests are persisted; MCL validation checks Account, Household and Client keys/relationships.
- Contract discrepancy to resolve in tests/docs: `tests/test_mcl_flow.py` still names its Client mapping test `test_client_source_maps_to_party_canonical_fields`. Its assertions use Client canonical fields, but the test name preserves obsolete Party terminology. The P0 contract is **Client only; no Party entity**.
- Workflow duplication to trace before removal: `ui/app.py` contains both the integrated upload → approved-file proposal/review workflow and legacy per-entity `/api/pilot/{account,household,client}/profile|map|lineage` endpoints. Do not delete these handlers until active-template, route, test and import references are verified; the integrated UI should have one clear source of truth.
- State/persistence verification already represented by focused tests: `tests/test_source_file_lifecycle.py`, `tests/test_workflow_state_restart.py`, `tests/test_workflow_event_engine.py`, `tests/test_mapping_request_propagation.py`, and `tests/test_p0_ui_boundary.py`. These tests are useful foundations, but CI passing does not establish that every form/button completes the entire browser journey.
- Phase 1 exit criteria: verify the route-to-handler transitions above; exercise single and bulk approve/reject, registered transformation review, information-request response, mapping/lineage persistence after state-store restart, and validation output from the same approved source files. Then run the journey through a browser or HTTP integration test using a clean temporary SQLite/upload directory. No target loading, reverse mapping, wave assignment or new architecture is introduced in this phase.


### Phase 1 implementation follow-up — 2026-10-23

- Latest verified branch head: `dede33afe2cc319d9205e95486671ab257abcceb`.
- Latest CI run: [#620](https://github.com/jeevamutharasiselvarasu/wealth-migration-platform/actions/runs/40006898901) passed dependency installation, Python compilation, and the complete `pytest -q` suite (141 passed, 9 warnings).
- Demo and manual migration setup now persist `MIGRATION_CONTEXT_UPDATED` and `ENTITY_SCOPES_UPDATED` events. `PENDING_DISCOVERY` is retained during state rehydration rather than being silently discarded.
- Demo ingestion and interactive upload now use the same evidence-based `classify_source_file` classifier. Ambiguous/relationship-grain files are retained for review rather than aborting demo loading; relationship classification retains its source role.
- Demo load now registers the knowledge references in the manifest/setup and publishes durable knowledge events. Its notice reports the count actually registered, not merely the manifest's declared count.
- Source-file events are published after inference and analysis metadata are attached. The event payload strips `sample_value(s)` recursively so sampled client values are not duplicated into the workflow event log.
- State rehydration reapplies `ENTITY_INFERENCE_REVIEWED` decisions, including file reclassification from `Needs clarification` into an approved P0 entity, review status, and entity scope.

- Mapping approvals now require an authenticated `Platform Admin` or `Domain SME`; reviewer identity is taken from the active session rather than trusting the editable form value.
- Added `tests/test_demo_workflow_contract.py` for all nine manifest files, knowledge-reference resolution, ASGI demo load + restart (nine source files, two knowledge assets), safe event payloads, and manual migration-setup persistence. Extended `tests/test_workflow_state_restart.py` for inference-review/reclassification persistence.
- The ASGI route integration now exercises source upload → explicit entity confirmation/reclassification → mapping generation → required-field information request/response → single mapping approval → reconciliation → restart. The journey also verifies that a Migration Analyst is blocked from approving a mapping while a Domain SME can approve it.
- Mapping generation status is now replayed from `MAPPING_PROPOSALS_GENERATED`; reclassification removes the source file from the prior bucket and clears stale entity-sample pointers. The information-request sync commits its upserts before closing the connection, and response handling rechecks durable status in a transaction.
- The new Client bulk-review integration test verifies successful all-at-once approval, reviewer identity from the authenticated session, atomic rejection of a batch containing an unsupported transformation, and successful retry after the rule is corrected. It also verifies that the error popup is accessible (`role=alertdialog`) and the redirect retains `#pending-mappings` so the review queue remains in view.
- **Remaining manual check:** this environment has not launched the user's local browser. Verify visual layout, actual browser scroll position, confirm-dialog interaction, role navigation, and popup dismissal in the running local UI. CI validates the HTTP route journey and rendered HTML, not a real browser session.
