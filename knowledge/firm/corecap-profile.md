---
asset_type: firm_profile
firm_id: FIRM-CORECAP
firm_name: Core Cap Wealth Management
role: source (acquired firm)
migration_id: MIG-CORECAP-2026
version: 3.0.0
phase: P0
---

# Firm Profile: Core Cap Wealth Management (source)

## P0 migration boundary
Source-to-canonical mapping for exactly three canonical entities: **Account**, **Client**, **Household**. Canonical-to-target mapping, target loading, trading, sleeve/model processing, performance migration and wave assignment are deferred. Party is not introduced.

## Source context
- **Known source systems:** Amplify and Black Diamond. Confirm which files and fields each system owns before mapping.
- **Business/data owner and mapping approver:** to be supplied by the migration team. Approvals are made by an authenticated Migration Analyst or Platform Admin; canonical-model questions go to the Domain SME.
- **Sample-data policy:** synthetic, masked or representative samples only. Never store real client data or tax identifiers here.

## Entity semantics (to confirm with the Domain SME per source file)
- **Account:** one custodial/legal account with its own source identifier, custodian code, registration type and open date. Registration decides who owns it and who may act (individual, joint, custodial, trust, IRA, discretionary).
- **Client:** a person or legal participant with a source client identifier. In some systems a "client" is only a contact and may own no account; record which meaning CoreCap uses.
- **Household:** a grouping of related clients and/or accounts, not a person or an account. Record whether CoreCap defines it as a family unit, an advisory relationship or a billing unit. It is not the same thing as a billing or reporting group.
- **Relationships:** client-to-account is many-to-many (joint accounts); a client belongs to one household. Relationship files stay separate until the relationship is documented and approved.

## Relationship and lineage rules
- Preserve source identifiers as strings, including leading zeroes; keep the system ID and the custodian account number as separate fields.
- Never infer joins from names, representative codes or similar-looking values alone.
- Missing identifiers, missing mappings, unknown columns and undocumented relationships are review exceptions.
- Proposed mappings need human approval before they become reusable canonical rules; only registered transformations may be approved.
- Clients that have not consented to the acquisition (negative or affirmative consent) must be flagged, not silently migrated. Consent tracking is not yet modelled in P0.

See `platform_knowledge/wealth_management_research.md` for the supporting research.
