# Proposal: a universal canonical model, with logic where it belongs

Status: proposal, reviewed 2026-11-11 (entity list agreed with two changes: no Party layer, Client sits in a Household; the representative is the Advisor). Nothing here is built yet.

## Why change

Today the canonical model (`config/firm_config.yaml`, contract `Simplicity-MCL-v1`) is shaped by the first source and the first target:
field names such as `ACCT_NUM`, `CUSTODIAN_CODE` or `HH_NAME` are close to CoreCap and Orion MCL, values stay in the source's own
coding ("Rollover IRA", "Schwab"), and most mappings are direct copies. The result works for one pair of systems but does not carry over:
a second source or a second target means reworking the middle. A canonical model is only worth having if it is the same for every source
and every target.

## Principles

1. **The canonical model is universal and versioned.** It describes wealth-management data as such, not any system's layout. It
   changes only through canonical releases (change request → SME approval → release → adoption per workspace, as built for D4).
2. **Canonical values are standard.** Typed (ISO dates, decimals, booleans), and every coded field uses a canonical code set
   (registration type, tax status, custodian, account status, relationship role, country, state, ...).
3. **Source logic lives in the source adapter; target logic lives in the target adapter.** Source → canonical absorbs everything that is
   peculiar to a source; canonical → target absorbs everything peculiar to a target. Neither leaks into the canonical model.
4. **Rules are data where they can be.** Crosswalks (source code → canonical code → target code) are reference tables the generated
   procedures read, so a new code value is a reviewed data change, not a new procedure or package.
5. **Agents recommend, deterministic services check, humans approve** (unchanged).

## Proposed canonical model (first cut)

| Entity | Holds | Notes |
|---|---|---|
| Household | the grouping a firm manages together; its advisor | the top of the model: clients and accounts belong to a household |
| Client | a person or an organisation in a household: names, birth date, tax id, type, household role (head, spouse, dependent) and order | one Client per real person across files (entity resolution); no separate Party layer (decided 2026-11-11) |
| Account | number, registration type, tax status, custodian, open/close dates, status, balance as of, household | |
| AccountRole | Client ↔ Account: owner, joint owner, beneficiary, trustee, ... | replaces names copied onto accounts |
| Advisor | the advisor (representative): name, contact, rep codes per custodian | the representative is the advisor (decided 2026-11-11); resolves rep codes to advisors once |
| Address, ContactPoint | typed addresses, phones, emails, with purpose and primary flag | many per Client |
| Identifier | every external id of an entity (source system, type, value) | keeps the lineage back to each source |
| Code sets | canonical codes with descriptions, versioned | reference data, not entities |

Every canonical row keeps its lineage (source system, file, batch, source key), as the raw and mirror layers already do.

## Where each kind of logic goes

| Logic | Layer | How |
|---|---|---|
| Parse, type, trim, normalise | source → canonical | generated per source; rule catalogue (exists) |
| Source code → canonical code | source → canonical | crosswalk table `xodus_ref.source_code_map` (data) |
| Split / join / lookup / derive (e.g. last name from "Last, First", rep name from rep code) | source → canonical | approved rules (exists), generated into the entity procedure |
| Entity resolution and survivorship (same person in two files; which value wins) | source → canonical | new: match rules + survivorship rules per entity, approved like mappings |
| Pivot / unpivot (one contact row → Client + ContactPoints + its household role) | source → canonical | new: one source file may feed several canonical entities |
| Canonical code → target code | canonical → target | crosswalk table `xodus_ref.target_code_map` (data), one per target system |
| Denormalise / flatten (Orion MCL is one flat import) | canonical → target | target adapter, written once per target system |
| Target formatting and required-field rules | canonical → target | target adapter |

The **target adapter** (canonical → target) is built once per target system and reused by every migration into it; it changes only when
the target changes or the canonical model has a new release. The **source adapter** is built per source and is where most of the
analyst's work goes. Both remain generated stored procedures run by the same stable pipelines.

## What changes in the platform

1. Canonical model and code sets move to a versioned platform store (Platform knowledge / Governance), out of `firm_config.yaml`.
2. Stage 03/04 gain code-set mapping (source values → canonical codes, profiled from the samples, suggested, approved) and multi-entity
   files (one file feeding several entities).
3. Stage 06 becomes "target adapter": per target system, reviewed once; a migration reuses the confirmed adapter and only reviews gaps.
4. Validation adds a canonical conformance layer: every coded value is in its code set, types parse, identifiers resolve.
5. `xodus_ref` schema for crosswalk tables, deployed by the setup pipeline and editable through approved changes.

## Phasing

1. Canonical model v2 and code sets (definition, store, release); Household, Client, Account, AccountRole, Advisor first.
2. Crosswalk tables and code-set mapping in stages 03/04; source adapter generation against v2.
3. Orion MCL target adapter against v2, reusable across migrations; stage 06 reworked to adapter review.
4. Entity resolution and survivorship; conformance validation.
5. Second source (Amplify) through the same model to prove the "universal" claim.

## Decisions needed

1. ~~Agree the principles and the entity list~~: agreed 2026-11-11 with the two changes above. Holdings and transactions can follow in a later phase.
2. ~~Which code sets first~~: decided 2026-11-11: registration type, tax status, custodian, account status, household role.
3. ~~Who owns the target adapter~~: decided 2026-11-11: the Platform Admin (platform level, reused by every migration into that target).
4. Whether entity resolution (same person across files) is in the first phase or the second.
