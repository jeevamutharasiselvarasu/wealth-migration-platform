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

## Workflow
1. Persona selection.
2. Migration Setup: target/canonical configuration, source mode, and knowledge registration.
3. Migration Dashboard.
4. Source Discovery & Entity Inference: profile files, propose entity classification, and ask a human to confirm uncertain results.
5. Mapping Studio & Transformation Rules: source field profiling, mapping proposals, lineage, and supported transformation suggestions.
6. Mapping Review & Information Requests: resolve missing definitions and approve/reject proposals.
7. Validation & Exception Management: inspect relationship and data-quality findings and available reconciliation evidence.

## Inputs and knowledge
- Expected file formats include CSV, TSV, XLSX, XLSM, XLS, and JSON.
- Demo data should be separate from platform-provided knowledge.
- Platform knowledge includes the canonical contract and source-mapping guidance.
- Source files and mapping recommendations must retain lineage to their origin.

## Out of scope unless explicitly approved
- Production target loading or cutover.
- Reverse/canonical-to-target mapping.
- Actual wave/cohort assignment.
- Live Azure/ADF integration or claiming cloud connectivity without verification.
- Autonomous approval of consequential mappings.

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
- `demo_data/knowledge/canonical_contract.md`
- `demo_data/knowledge/source_mapping_notes.md`
