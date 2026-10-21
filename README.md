# XODUS-AMP — Wealth Migration Workbench

XODUS-AMP supports wealth-management business migration after an acquisition: understanding source-firm files, aligning their meaning to a target firm's canonical contract, documenting lineage, and obtaining human review before mappings are treated as approved.

## Current pilot boundary

The current P0 pilot is a **local, sample-file-based source-to-canonical mapping workflow** for exactly three entities:

- **Account**
- **Household**
- **Client**

It supports CSV and other tabular source files, source profiling, entity inference from file evidence, mapping suggestions, lineage evidence, human confirmation/review, knowledge-asset registration, and MCL relationship validation.

P0 is not a production migration runtime. Reverse/canonical-to-target mapping, target-system loading, production cutover, live AI-agent hosting, and actual wave/cohort assignment are outside the current pilot. The dashboard may describe future cohort-segregation conditions, but it must not imply that records have been assigned to waves. Azure/ADF endpoints are integration scaffolding and configuration must not be represented as a live connection unless actually verified.

## Workflow

1. **Persona selection** — choose the role available to the signed-in user.
2. **Migration Setup** — configure target system and canonical contract, source acquisition mode, and register knowledge references.
3. **Migration Dashboard** — review setup, source-file classification status, and outstanding work.
4. **Source Discovery & Entity Inference** — upload or load sample files; inspect evidence and confirm/reject inferred Account, Household, or Client classifications.
5. **Mapping Studio & Transformation Rules** — profile source fields and review source-to-canonical mapping candidates and lineage.
6. **Mapping Review & Information Requests** — resolve missing definitions, gaps, and mapping approvals.
7. **Validation & Exception Management** — inspect relationship/data-quality findings and reconciliation evidence available in the pilot.

The source files and mapping recommendations require human review. Uncertain entity classification should be surfaced for clarification, not silently guessed.

## Local setup

Requires Python 3.11 or later.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\\Scripts\\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
python -m pytest
python -m uvicorn ui.app:app --reload --port 8000
```

Open http://127.0.0.1:8000 in a browser.

## Demo data

On **Migration Setup**, use **Load demo data set** to load the migration configuration from `demo_data/demo_migration.json`, the nine synthetic Account/Household/Client source files under `demo_data/source_files/`, and sample knowledge documents under `demo_data/knowledge/`. The manifest and data files are editable inputs; inferred classifications remain pending human confirmation.

The local demo identity is configured through `demo_data/demo_users.json` and application demo-mode settings. Demo authentication is for local demonstration only. Disable demo mode and configure a proper identity provider before any shared or production deployment. Never place real client data or secrets in demo fixtures.

## Repository map

- `ui/` — FastAPI routes, session/persona handling, templates, and browser components.
- `control_plane/` — canonical kernel, rule/metadata compilation, profiling, inference, validation, and integration scaffolding.
- `agents/` — mapping and validation components; the legacy cohort orchestrator was removed from P0.
- `data_plane/` — local source-file persistence, validation, and telemetry helpers. Legacy custodian connectors and target-system adapters are intentionally absent from P0; target loading remains deferred.
- `knowledge/` and `demo_data/` — editable knowledge assets, inference rules, demo manifest, and synthetic source files.
- `tests/` — contract, UI-boundary, inference, mapping, and service tests.
- `infrastructure/` — future Azure/Bicep and ADF deployment assets; these do not establish that cloud resources are deployed.
- `docs/BRANCH_ARTIFACT_INVENTORY.md` — artifact-by-artifact inventory and cleanup recommendations.

## Safety and scope rules

- Keep the active entity scope limited to Account, Household, and Client unless scope is explicitly changed.
- Keep source-to-canonical mapping as the only mapping direction in P0.
- Do not treat unreviewed suggestions as approved mappings.
- Do not claim that files were loaded into a target, that a production cutover ran, or that cloud infrastructure is connected based only on local demo state.
- Preserve source lineage and explainable evidence for classification and mapping decisions.
