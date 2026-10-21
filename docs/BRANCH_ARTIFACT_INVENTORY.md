# Branch Artifact Inventory and Removal Assessment

- Branch: `feature/xodus-three-entity-pilot-integrated`
- Audited commit: `f1db078687a235a1b614541464a0e12e7b1e012a`
- Method: Git recursive tree (153 tracked files), plus inspection of the active FastAPI entry point, primary navigation, P0 boundary tests, README, dependencies, and Compose configuration.
- Scope baseline: P0 source-to-canonical mapping for Account, Household, Client. Target loading, reverse mapping, actual wave assignment, cutover, sleeve/allocation processing, and production connectors are deferred.
- **This report marks candidates; it does not delete files.** Before removing source modules, verify all imports, runtime routes, test references, deployment use and documentation references.

## Status legend

- **KEEP** — clear active P0, build, test, demo, or infrastructure role.
- **KEEP / REVIEW** — plausible active use; verify before changing.
- **REMOVE CANDIDATE** — appears legacy, duplicated, or outside current P0; remove only after dependency confirmation.
- **REMOVE FROM GIT IF PRESENT** — generated/local artifact not in the current audited tree. No such file should be inferred to exist from an older diff.

## Findings that change the cleanup plan

1. The current recursive tree has **153 files** and is not truncated.
2. The current tree does **not** contain `App_Data/controlplane.db`, a `dist/` package, generated `App_Data/Storage/` outputs, or tracked `__pycache__/` files. Do not schedule deletions for files absent from this snapshot.
3. `ui/app.py` currently imports the orchestrator, schema mapper, cutover agent, audit agent, storage manager, profiler, MCL validation, source adapter, entity inference, wave rules, ADF client/exporter, and database layer. These modules are **not safe to remove solely because they sound legacy**.
4. `docker-compose.yml` references both Dockerfiles, `App_Data`, and `data_plane/storage`; `README.md` references scripts and multiple legacy capabilities. Deployment/docs must be aligned before removing those references.
5. The main sidebar no longer exposes the old standalone Knowledge Studio or HITL route, but route handlers and templates may still exist. Treat them as candidates for consolidation, not proof of dead code.
6. Sample CSV sets overlap across `demo_data/source_files/`, `knowledge/sample_file/`, `sample_data/p0_mapping_tests/`, `test_files/`, and `data_plane/storage/`. Keep only the canonical demo fixtures and test fixtures actually referenced by tests/scripts; do not delete them before checking those references.

## Complete inventory

### Repository / deployment / docs

| Path | Disposition | Reason / action |
|---|---|---|
| `.github/workflows/tests.yml` | KEEP | CI workflow |
| `.gitignore` | KEEP | Ignore generated/local outputs; ensure rules cover `*.py[cod]`, `__pycache__/`, local DBs and runtime storage |
| `Dockerfile.control_plane` | KEEP / REVIEW | Referenced by Compose; verify image starts the current app |
| `Dockerfile.data_plane` | KEEP / REVIEW | Referenced by Compose; data-plane runtime is deferred but Compose currently uses it |
| `README.md` | KEEP — UPDATE | Current docs contain stale claims about legacy agents, target adapters and test counts |
| `architecture.html` | KEEP / REVIEW | Architecture reference; align to generic P0 flow and label future-state features |
| `docker-compose.yml` | KEEP / REVIEW | Deploys two services and mounts runtime storage; align with supported local demo architecture |
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
| `agents/discovery/extractor_agent.py` | KEEP / REVIEW | Extraction helper; trace imports and tests |
| `agents/discovery/schema_mapper_agent.py` | KEEP | Imported by `ui/app.py`; mapping logic |
| `agents/logic/__init__.py` | KEEP | Python package |
| `agents/logic/compliance_validator_agent.py` | KEEP / REVIEW | Trace import and tests; compliance checks may be future-state |
| `agents/logic/data_quality_agent.py` | KEEP / REVIEW | Data-quality functionality may support validation; trace usage |
| `agents/logic/sleeve_math_agent.py` | REMOVE CANDIDATE | Sleeve math is outside the three-entity source-to-canonical P0; keep only if an active import/test requires it |
| `agents/operational/__init__.py` | KEEP | Python package |
| `agents/operational/audit_agent.py` | KEEP | Imported by `ui/app.py`; audit responsibility |
| `agents/operational/cutover_agent.py` | KEEP / REVIEW | Imported and instantiated by `ui/app.py`; cutover feature is deferred, so remove only after decoupling active import |
| `agents/operational/loader_agent.py` | REMOVE CANDIDATE | Target loading is deferred; trace imports/tests before removal |
| `agents/operational/reconciler_agent.py` | KEEP / REVIEW | Reconciliation is in P0; confirm active implementation path vs `data_plane/validation/reconciler.py` |
| `agents/orchestrator.py` | KEEP / REVIEW | Imported by `ui/app.py`; inspect if its legacy multi-agent pipeline is exposed or only compatibility code |

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
| `data_plane/adapters/__init__.py` | KEEP | Python package |
| `data_plane/adapters/base_adapter.py` | KEEP / REVIEW | Adapter abstraction; trace usage |
| `data_plane/adapters/orion_eclipse_adapter.py` | REMOVE CANDIDATE | Named target adapter is outside generic P0; no target loading. Verify imports/deployment first |
| `data_plane/adapters/salesforce_sparc_adapter.py` | REMOVE CANDIDATE | Named target adapter is outside generic P0; verify imports/deployment first |
| `data_plane/connectors/amplify_connector.py` | REMOVE CANDIDATE | Custodian-specific connector outside sample-file-first P0; check tests/imports first |
| `data_plane/connectors/blackdiamond_connector.py` | REMOVE CANDIDATE | Custodian-specific connector outside sample-file-first P0; check tests/imports first |
| `data_plane/core/sleeve_engine.py` | REMOVE CANDIDATE | Sleeve/allocation execution is deferred |
| `data_plane/storage/amplify_positions.csv` | REMOVE CANDIDATE | Legacy connector fixture; keep only if referenced by tests or mock-data flow |
| `data_plane/storage/amplify_sleeves.csv` | REMOVE CANDIDATE | Legacy sleeve fixture; outside P0 unless referenced by tests |
| `data_plane/storage/blackdiamond_performance.csv` | REMOVE CANDIDATE | Legacy connector fixture; keep only if referenced |
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
| `knowledge/sample_file/accounts.csv` | REMOVE CANDIDATE | Duplicate/older account fixture; compare references against canonical demo source files |
| `knowledge/sample_file/amplify_sleeves.csv` | REMOVE CANDIDATE | Sleeve fixture outside P0 |
| `knowledge/sample_file/terminated_accounts.csv` | KEEP / REVIEW | May be useful for exceptions; verify test and loader references |
| `knowledge/sample_file/transactions.csv` | REMOVE CANDIDATE / REVIEW | Transaction-level processing is not a named P0 entity; keep only if tests need it |
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
| `scripts/create_test_sample_files.py` | KEEP / REVIEW | Test fixture generator; keep if reproducible tests use it |
| `scripts/generate_mock_data.py` | KEEP / REVIEW | Legacy connector/sleeve mock data; remove if not part of the supported demo or tests |

### Additional test inputs

| Path | Disposition | Reason / action |
|---|---|---|
| `test_files/accounts.csv` | REMOVE CANDIDATE | Likely overlaps other account fixtures; confirm references |
| `test_files/amplify_sleeves.csv` | REMOVE CANDIDATE | Sleeve fixture outside P0; confirm test references |
| `test_files/clients.csv` | REMOVE CANDIDATE / REVIEW | Likely overlaps demo and mapping-test client fixtures; keep if tests reference it |
| `test_files/households.csv` | REMOVE CANDIDATE / REVIEW | Likely overlaps other household fixtures; keep if tests reference it |
| `test_files/terminated_accounts.csv` | REMOVE CANDIDATE / REVIEW | Potential duplicate exception fixture; confirm references |
| `test_files/transactions.csv` | REMOVE CANDIDATE | Transactions are outside named P0 entity scope unless referenced by tests |

### Tests

| Path | Disposition | Reason / action |
|---|---|---|
| `tests/__init__.py` | KEEP | Python package |
| `tests/test_entity_inference.py` | KEEP | P0 inference behavior |
| `tests/test_knowledge_asset_extraction.py` | KEEP / REVIEW | Keep if current knowledge upload/extraction is supported |
| `tests/test_mcl_api.py` | KEEP | MCL API coverage |
| `tests/test_mcl_flow.py` | KEEP | MCL workflow coverage |
| `tests/test_orchestrator.py` | KEEP / REVIEW | Keep if orchestrator remains part of current application; otherwise remove with module |
| `tests/test_p0_entity_scope_contract.py` | KEEP | P0 scope contract |
| `tests/test_p0_ui_boundary.py` | KEEP | Current UI/workflow regression tests |
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
| `ui/static/components/discovery.js` | KEEP / REVIEW | Check if referenced by current templates |
| `ui/static/components/execution.js` | REMOVE CANDIDATE / REVIEW | Execution UI may be legacy; check script references and active routes |
| `ui/static/components/ingestion.js` | KEEP / REVIEW | Check template references; likely file-ingestion UI |
| `ui/static/components/login.js` | KEEP / REVIEW | Check login template reference |
| `ui/static/components/migration-knowledge.js` | KEEP / REVIEW | Knowledge behavior may be duplicated/consolidated; check script references |
| `ui/static/components/migration-setup.js` | KEEP / REVIEW | Check setup template reference |
| `ui/static/components/reconciliation.js` | KEEP / REVIEW | Validation/reconciliation UI; check script references |
| `ui/static/components/role-selection.js` | KEEP / REVIEW | Check persona template reference |
| `ui/static/components/rule-editor.js` | KEEP / REVIEW | Check if current mapping studio references it |
| `ui/static/components/rulebook-editor.js` | REMOVE CANDIDATE / REVIEW | May be legacy parallel rulebook UI; check script references |
| `ui/static/components/workspace-shell.js` | KEEP / REVIEW | Check active shell references |
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
- `agents/logic/sleeve_math_agent.py`, `data_plane/core/sleeve_engine.py`, `tests/test_sleeve_math.py` as a coordinated removal set if no active P0 dependency remains.
- `data_plane/adapters/orion_eclipse_adapter.py`, `data_plane/adapters/salesforce_sparc_adapter.py`, `data_plane/connectors/amplify_connector.py`, `data_plane/connectors/blackdiamond_connector.py` only after active import/fixture/test scan.
- `data_plane/storage/amplify_positions.csv`, `data_plane/storage/amplify_sleeves.csv`, `data_plane/storage/blackdiamond_performance.csv` only if no test/script/loader depends on them.

### Tier 2 — consolidate/decide
- `ui/templates/firm_knowledge.html`, `ui/templates/knowledge_studio.html`, `ui/templates/knowledge_ingestion.html`, `ui/templates/questionnaire.html`, and `ui/static/components/migration-knowledge.js`: consolidate into Migration Setup only after moving any useful upload/reference behavior.
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

Validated against branch commit `d4a9a08e476f1c7a3bd394862b16b0a26ceccbf1` before this follow-up:
- The current route/template flow renders `login.html`, `persona_selection.html`, `migration_setup.html`, `dashboard.html`, `firm_knowledge.html`, `entity_sample_files.html`, `agent_pipeline.html`, and `reconciliation.html`.
- `entity_card.html` and `entity_menu_card.html` had no references in `ui/app.py` and were not included by any active template. They have now been removed as unreferenced legacy templates.
- `README.md` was rewritten to match the three-entity source-to-canonical P0 workflow and to stop describing legacy cutover/wave/target-loading features as active implementation.
- The prototype trio (`ui/static/prototype.html`, `.css`, `.js`) was removed in the preceding cleanup.

Still **do not delete yet**:
- The remaining `ui/static/components/*.js` files. They have no direct filename references in the currently inspected templates/app, but a safe removal requires tracing inline event handlers and DOM IDs against each file and checking whether any test expects the component assets.
- Legacy cutover, connector, sleeve math, ADF, and wave modules. Several are imported by `ui/app.py` or exercised by tests; remove only after separating the current P0 path and updating the test contract.
- `firm_knowledge.html` and `/firm-knowledge`: both are currently routed and rendered. Consolidation is a navigation/product decision, not a safe orphan deletion.
- Multiple overlapping fixture directories. Compare their consumers before deleting duplicates.

Current branch after the template cleanup has 144 tracked files. CI for the latest cleanup commit is queued/running at the time of this note; do not treat it as passed until the run completes.
