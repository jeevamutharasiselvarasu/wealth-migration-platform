# CoreCap to Simplicity: migration knowledge (Account, Client, Household)

Migration knowledge for the CoreCap (Amplify) extract and the Simplicity targets (Orion Master Client List, Salesforce SPARC).
Structure and rules only; no record values. Canonical contract: Simplicity-MCL-v1, config version 1.1.0.
Proposals made from this note stay proposals until a Migration Analyst or Platform Admin approves them.

## Source meaning (CoreCap / Amplify)
- **Account** comes from Accounts. The system id is AccountId (a GUID); the custodian number is AccountNumber. Keep both. The natural key is custodian plus account number.
- **Client** comes from Household_Contacts: one row per person. It is a contact, not necessarily an account owner.
- **Household** comes from Households: a grouping linked to an advisor. HouseholdName is the sort name ("Last, First"); HouseholdProperName is the display name.
- Accounts and contacts link to a household through HouseholdId. **There is no account-to-contact link.** Never infer an account owner from a name; ownership needs an approved rule or a relationship file.

## Derivations the canonical model expects
- ACCT_STATUS: Open when ClosedDate is empty, otherwise Closed.
- HH_STATUS: Active when DeletedDate is empty, otherwise Deleted.
- CUSTODIAN_CODE: derived from CustodianName through a versioned value map (73 free-text names in the full extract).
- LEGAL_NAME: first name, middle name and last name joined; blank first name is allowed for companies.
- Primary contact: the Client with HH_ORDER = 1. Household-level target fields (SSN, name, DOB, address, phone, email) are taken from the primary contact at the target stage, not stored on Household. The 2nd owner is HH_ORDER = 2.
- Representative: Households carry AdvisorId, not a rep code. The Orion representative name is resolved from ADVISOR_ID through a registered advisor list. The advisor list is reference data in this phase, not a fourth entity.

## Missing and invalid values
- Never invent defaults. A blank required value is an exception or an information request.
- REG_TYPE is about 29% null in the full extract. Both targets require it, so every null needs a decision: fix at source, agreed value, or exception.
- REP_CODE is optional (2.6% null).
- AccountNumber: a few rows hold account-name text instead of a number. Reject them as data-quality exceptions; clean values fit 20 characters.
- Keep text identifiers as text (ZIP codes, account numbers, ids) to preserve leading zeros.
- Orphan household ids exist on accounts (about 7) and contacts (about 34). Report them; do not drop silently.

## Open business decisions (Domain SME or business owner)
1. How to identify the account owner(s): parse AccountName, or obtain a relationship file from CoreCap.
2. Whether deleted households and closed accounts migrate.
3. How to treat accounts with no registration type.
4. Whether Orion's Household Name is the sort name or the display name (SPARC uses the display name).
5. Whether the advisor list stays reference data or becomes an Advisor entity in a later phase.
6. How trusts and businesses are identified: the source EntityType holds only "individual", while SPARC derives trusts from name patterns.
7. Whether fee data (Billing_OffB) and the SPARC Total Fee calculation belong in the canonical model or stay a target-side calculation.
8. Whether Fund Name equals the custodian for Orion.
9. Whether one primary address per client is enough.

## Deferred (not in the three P0 entities)
Advisors and permissions, strategies, models, sleeves, holdings, transactions, performance and the Risk Score concept. RiskScore and RiskToleranceScore columns on Accounts and Households are preserved as deferred, not discarded.

## Extract quality (confirm with CoreCap)
- The sample workbook is mostly misaligned: only 89 accounts, 20 households and 20 contacts carry a key. Thousands of further rows hold only a closed date, risk score or deleted date with no id. Counts from it are not reliable until CoreCap confirms the full extract is aligned.
- Billing_OffB uses a different key space (11-character ids) from the Amplify GUIDs. Join on custodian plus account number; this is untested on the sample.
- The dictionary export redacts last name, date of birth, tax id and email.
