# Canonical Contract — P0 Context

> Compact working reference. The runtime contract is `config/firm_config.yaml`; `platform_knowledge/canonical_contract.md` is the demo seed registered with the migration. If either differs from this summary, they win.

## P0 entities
- Account
- Client
- Household

Do not introduce Party or other entities into the P0 UI/workflow without an explicit scope change.

## Mapping direction
Map source fields to the canonical model. Canonical-to-target mapping and target loading are out of scope for this P0.

## Mapping review
A mapping proposal is not approved merely because an agent suggested it. Preserve source-field lineage, the target canonical field, the rationale/evidence, confidence or uncertainty where available, and review status. Require an authorized human approval before treating it as confirmed. The Migration Analyst (or Platform Admin) approves or rejects mappings; the reviewer is taken from the authenticated session.

## Data quality and relationships
Use available evidence to validate Account, Client, and Household relationships. Missing definitions, unclear relationships, or uncertain classifications should become review items or information requests rather than silent assumptions.

## Scope guard
Never claim that a target system has been loaded, a production migration/cutover occurred, or a cloud connection is live based solely on local demo state.

## Firm configuration (implemented 2026-10-26)
The canonical contract (fields, types, nullability, keys) lives in `config/firm_config.yaml` and is versioned (`version_id` = config version + content hash). Migration setup is blocked without it, and required fields for the mapper, RFIs and dashboard all come from it. Keys, types and required fields are seed values pending business-owner confirmation; `REG_TYPE` and `TAX_ID` were confirmed as required.

## Canonical model ownership and change governance (decided 2026-10-26)
- The canonical model belongs to the platform. The **Domain SME / Data Steward** owns it day to day; the **Platform Admin** maintains it.
- A migration team does not edit the canonical model. When the migration needs something it does not provide (a missing field, a changed definition, a new reference/lookup value), a **Migration Analyst raises a canonical change request** on `/canonical-changes`: entity, canonical field, change type (add field, change definition, add reference value, other), proposed change, reason.
- The **Domain SME decides**: approve, reject or defer. Reject and defer require a note. A decided request is final; a deferred one can be decided later.
- Requests persist in `canonical_change_requests` (per migration) with requester, decider, timestamps and note; both actions are audited. Identity comes from the session.
- Information requests about missing canonical definitions can be answered by the Analyst or SME; a need to change the model itself becomes a change request rather than a silent workaround.
- P0 limit: approved requests are the versioned backlog for the platform team. They do not alter `firm_config.yaml`, the mapper or the kernel automatically.

## Decisions against the reference documents (2026-10-26)
Reference set: wealth-management entity model, data-migration platform blueprint, data-migration-platform skill. They describe the full platform; XODUS P0 is a subset.
- **Client stays an entity** for P0, with **no Party**. The reference model treats Client as usually a role (Party/Household/Account relationship) and allows a distinct Client entity only with SME approval; P0 scope retains it. Revisit if the Simplicity contract says otherwise.
- **Registration and Custodian Account are folded into Account** for P0 (`REG_TYPE`, `CUSTODIAN_CODE` are Account fields). Deliberate simplification, not a modelling claim.
- **Relationship files** (account-to-client, client-to-household links) are relationship artifacts, not master entities. No relationship entity exists in P0, so each must carry an explicit disposition (relationship, deferred; excluded; information loss; unresolved) instead of sitting unmapped.
- **Mapping kinds**: every proposal is one of direct, derived, lookup, relationship, excluded, unresolved. A source concept without a destination still needs a disposition. Unresolved concepts are never guessed.
- Out of scope for P0 (full-platform items): waves, target adapters, execution runtimes, other entity domains.

## Research-informed open questions (see `platform_knowledge/wealth_management_research.md`)
- Whether canonical Client may be a non-person (trust, company) via a type attribute, given Party is out of scope.
- Whether the canonical Household means family unit, advisory relationship or billing unit.
- How joint-account owners and ownership percentages are represented against a single `CLIENT_ID` on Account.
- Whether a consent status (negative/affirmative consent to the acquisition) belongs on Client or Account.
