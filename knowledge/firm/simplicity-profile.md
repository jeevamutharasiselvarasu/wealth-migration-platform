---
asset_type: firm_profile
firm_id: FIRM-SIMPLICITY
firm_name: Simplicity Wealth
role: target (acquiring firm)
migration_id: MIG-CORECAP-2026
version: 2.0.0
phase: P0
---

# Firm Profile: Simplicity Wealth (target)

## P0 boundary
Simplicity's model is the target of the canonical model. P0 maps CoreCap source files to the canonical Account, Client and Household entities only. Loading into Simplicity systems, reverse mapping and wave assignment are deferred. The next phase is the Azure integration (ADLS storage and ADF orchestration; the ADF package, simulator and routes exist, and the Azure assets will be added when that phase starts).

## Operational architecture (as recorded in the previous profile; confirm with the migration team)
- **Primary custodian:** Charles Schwab.
- **CRM and client administration:** Salesforce SPARC, holding households, clients and custodial account metadata.
- **Trading and portfolio accounting:** Orion Eclipse, holding model aggregates, sleeves, rebalancing and trade blotters.

## Boundaries that matter for the three entities
- SPARC is the expected owner of Household, Client and Account metadata; it does not handle live trading or portfolio accounting.
- Orion Eclipse owns positions, holdings and performance. These are outside the three-entity P0 and should not be mapped here.
- Whether the target household is a family unit, an advisory relationship or a billing unit, and how joint-account owners are represented, are open canonical-model decisions for the Domain SME. Changes are raised as canonical change requests.

## Open items
- Target mandatory fields, allowed values and key definitions (to be supplied by the target owner).
- Target identifier format and matching keys for accounts, clients and households.

## Target layouts the canonical model must feed (contract 1.1.0)
- **Orion Master Client List** (27 fields) and **SPARC Household and Account** (34 fields). Of these, 24 Orion and 29 SPARC fields come from the three P0 entities; the rest (fees, contributions, models, gain/loss, outside id) are deferred.
- Both are flat, one row per account, with the household and 1st/2nd owner repeated. Household-level fields come from the primary contact (household order 1).
- Custodian, fund and registration type come from fixed target lists, so the canonical carries a code plus a registered value map.
- Registered migration knowledge: `demo_data/knowledge/simplicity_target_layouts.json`.
