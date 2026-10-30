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
Simplicity's model is the target of the canonical model. P0 maps CoreCap source files to the canonical Account, Client and Household entities only. Loading into Simplicity systems, reverse mapping and wave assignment are deferred. The next phase is the Azure integration (ADLS storage and ADF orchestration; scaffolding exists under `infrastructure/` and the ADF routes).

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
