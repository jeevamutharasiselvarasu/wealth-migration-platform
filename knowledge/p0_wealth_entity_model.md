# P0 Wealth Entity Model: Account, Client, Household

This model describes the source-to-canonical boundary for P0. It is a practical domain interpretation for the migration workflow, not a claim that every wealth-management firm's source systems use identical schemas.

## Account
Represents a legal or custodial account. It normally carries account-level attributes such as account number, custodian, registration type, status, and opening date. It can be associated with one or more clients depending on account ownership and registration (for example, individual, joint, trust, or entity accounts). P0 maps only fields actually present in the source and does not attempt to model positions, transactions, sleeves, or trading.

## Client
Represents a person, trust, or other legal participant known to the firm. A client has its own source identity and identity attributes. A client may be associated with multiple accounts; ownership and roles must come from source evidence rather than assumptions.

## Household
Represents a business grouping used for relationship management or reporting. A household may group multiple clients and accounts, but exact membership rules vary by firm and source platform. A household identifier and label do not by themselves establish membership.

## Relationships are explicit, not guessed
- Account-to-Client: use a documented ownership/registration link, relationship file, or approved source key.
- Client-to-Household: use a documented household membership key or mapping file.
- Account-to-Household: use an explicit source relationship or a reviewed, traceable relationship derived from documented links.
- Representative codes can describe servicing responsibility; they are not automatically unique relationship keys.
- Name similarity and row order are never join logic.
- When relationships are not represented in source data, record an exception and request business confirmation.

## Mapping governance
1. Profile each file separately and retain its source role, filename, columns, and lineage.
2. Map source columns to the fixed canonical fields for the corresponding entity.
3. Mark missing required identifiers, unknown columns, and absent relationship evidence as exceptions.
4. Require a named reviewer to approve each proposed mapping before it is reusable.
5. Keep canonical-to-target mapping and cohort/wave assignment out of P0.

## Source-system variation to expect (from `platform_knowledge/wealth_management_research.md`)
- A "client" may be only a contact (it can own no account) in one system and the legal owner in another; record the meaning each source uses.
- Joint accounts have several owners; joint tenants in common carry ownership percentages. A single `CLIENT_ID` on Account is a simplification.
- A household may be a family unit, an advisory relationship or a billing unit, and it is distinct from billing and reporting groups.
- Keep the system identifier and the custodian account number as separate fields.
