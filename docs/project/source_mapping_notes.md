# Source Mapping Notes — P0 Context

> Compact working reference. `platform_knowledge/source_mapping_notes.md` is the demo seed registered with the migration; this note records findings and rules agreed while building.

## Source
CoreCap sample files are the source for the P0 workflow. Source layouts can vary and may contain wealth-management terminology that does not exactly match canonical field names.

## Evidence to use
For source profiling and entity inference, consider:
- Column names and aliases.
- Data types and sample values.
- Null/missing-value patterns.
- Uniqueness and candidate keys.
- Relationships and foreign-key-like patterns.
- Registered dictionaries, source definitions, and knowledge references when available.

Optional dictionaries/context should improve inference but should not be mandatory for basic blind classification.

## Classification behavior
- Propose Account, Client, or Household only when evidence supports it.
- Relationship/bridge files should be flagged as relationships or ambiguous inputs instead of being forced into a master entity.
- Return evidence, confidence, competing candidates, and reasons for review where supported.
- Human confirmation is required for uncertain classifications.

## Mapping behavior
- Recommend source-to-canonical mappings with explanation and lineage.
- Support one-to-one and valid one-to-many source-to-canonical relationships where the implementation supports them.
- Ask for missing business definitions through information requests.
- Transformations must come from a registered, explicitly supported set. Never execute arbitrary code.
- Human approval is required before a mapping or transformation becomes approved.

## Demo data separation
Platform knowledge (canonical contract, source mapping notes, reusable rules) must remain distinct from migration-specific sample files and demo configuration.

## Findings and rules added 2026-10-26
- **Required fields are evaluated per entity across all files** of a migration, not per file. A field supplied by any pending/approved proposal of the same entity raises no information request; bulk approval is blocked only by requests for fields no file supplies.
- **Blind test (6 files, opaque headers)**: classification was correct for all four master files and flagged both link files as relationships, but the mapper produced zero proposals and 12 open requests, because it only knows exact canonical names and its alias list. Evidence-based suggestions (type, key, value pattern) are the planned fix; they stay proposals with provenance and a confidence cap, and need human approval.
- **Mapping kinds**: direct, derived, lookup, relationship, excluded, unresolved. Never guess an unresolved concept; a dropped source concept needs an explicit disposition (mapped, derived, relationship, excluded, information loss, unresolved).
- **Coded fields** (registration, rep, custodian codes) need versioned value maps. The `REP_CODE` alias rule `PREFIX_FORMAT` prepends `REP-` to any value lacking it (`R-01` becomes `REP-R-01`); decision pending.
- **Known classification gap**: a file carrying two entity ids (for example `household_accounts.csv`) can score as Account instead of a link file; a file-name or dictionary hint is the preferred fix, not a scoring change.
- Never use a display name alone to join entities; use deterministic keys first and preserve leading zeros (for example `000847`).

## Research-informed notes (see `platform_knowledge/wealth_management_research.md`)
- Keep the system identifier and the custodian account number as separate fields; do not rely on either alone.
- Record which meaning of "client" and "household" each source uses before mapping (contact vs. owner; family vs. advisory relationship vs. billing unit).
- A joint or multi-owner account flattened to one owner is a reviewable warning, not a silent mapping.
