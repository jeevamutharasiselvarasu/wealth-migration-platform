# Source Mapping Notes — P0 Context

> Compact working reference for Claude. Treat the repository's `demo_data/knowledge/source_mapping_notes.md` as the source of truth if it differs from this summary.

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
