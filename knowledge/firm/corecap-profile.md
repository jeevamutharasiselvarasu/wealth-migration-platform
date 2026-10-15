---
asset_type: firm_profile
firm_id: FIRM-CORECAP
firm_name: Core Cap Wealth Management
migration_id: MIG-CORECAP-2026
version: 2.0.0
phase: P0
---

# Firm Profile: Core Cap Wealth Management

## P0 migration boundary
The current phase implements source-to-canonical mapping for exactly three canonical entities: **Account**, **Client**, and **Household**. Canonical-to-target mapping, target loading, portfolio trading, sleeve/model processing, performance migration, and wave assignment are deferred.

## Source context
- **Known source systems:** Amplify and Black Diamond (confirm which files and fields each system owns before mapping).
- **Business/data owner:** To be supplied by the migration team.
- **Mapping approver:** To be supplied by the migration team.
- **Sample-data policy:** Use synthetic, masked, or representative samples only in this P0 workflow.

## Entity semantics
- **Account:** A custodial/legal account record with its own source identifier, custodian code, registration type, and opening date where provided.
- **Client:** A person, trust, or legal participant with a source client identifier and identity attributes.
- **Household:** A business/reporting grouping of related clients and/or accounts; it is not itself a person or account.

## Relationship and lineage rules
- Preserve source identifiers as strings, including leading zeroes.
- Keep separate source files and their lineage distinct until a relationship is explicitly documented and approved.
- Never infer joins from names, representative codes, or matching-looking values alone.
- Missing identifiers, missing mappings, unknown columns, and undocumented relationships must be reported as review exceptions.
- Proposed mappings require human approval before they become reusable canonical rules.
