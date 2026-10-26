# Canonical Contract — P0 Context

> Compact working reference for Claude. Treat the repository's `demo_data/knowledge/canonical_contract.md` as the source of truth if it differs from this summary.

## P0 entities
- Account
- Client
- Household

Do not introduce Party or other entities into the P0 UI/workflow without an explicit scope change.

## Mapping direction
Map source fields to the canonical model. Canonical-to-target mapping and target loading are out of scope for this P0.

## Mapping review
A mapping proposal is not approved merely because an agent suggested it. Preserve source-field lineage, the target canonical field, the rationale/evidence, confidence or uncertainty where available, and review status. Require an authorized human approval before treating it as confirmed.

## Data quality and relationships
Use available evidence to validate Account, Client, and Household relationships. Missing definitions, unclear relationships, or uncertain classifications should become review items or information requests rather than silent assumptions.

## Scope guard
Never claim that a target system has been loaded, a production migration/cutover occurred, or a cloud connection is live based solely on local demo state.
