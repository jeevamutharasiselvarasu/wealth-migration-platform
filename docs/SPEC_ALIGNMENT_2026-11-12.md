# Source spec alignment check (2026-11-12)

Checked: the active migration (Run 2, MIG-867AEF123A) as shown in the platform (source to canonical mappings on Mapping Validation Review, canonical to target mappings and the gap report on Target mapping) against `docs/source_specs/`:

- `Data_Mapping_MCLSPARC.xlsx`, sheet "MCL - Amplify Mapping" (the Orion Master Client List mapping, 27 target fields, 12 required) and its header tooltips.
- `Amplify_Data_Definitions.xlsx`, sheets "MCL Target Mapping" and "AmplifyToOrion MCL Mapping" (an earlier analysis from 10 source tables).
- `CoreCapDecom_AllFiles.xlsx` (18 source sheets; only column headers were read) and `orion_mcl_business_rules.md`.

Only structure and mappings were read; no client rows were used.

## 1. Do we have everything?

| Question | Result |
|---|---|
| Target fields: does the platform target match the spec? | Yes. ORION_MCL_ACCOUNT 6 + CLIENT 13 + HOUSEHOLD 8 = 27, the same 27 as the spec. |
| Source files: are the files that feed MCL loaded? | Yes. Accounts, Households, Household_contacts and Advisors (the other 14 CoreCap sheets, e.g. sleeves, holdings, transactions, performance, models, are not MCL sources). |
| Source columns: is every column that has an MCL home mapped? | Yes. 43 mappings. Unmapped columns are those the spec lists as "no MCL field": DefaultLiquidationMethod, LastDirectReconDate, RiskScore(Date), RiskTolerance*, DeletedByUserName, Salutation, Nickname, Suffix, Designations, JobTitle, SMSPhone. |
| SPARC (34 fields in the same workbook) | Not loaded as a target. The target adapter is Orion MCL only. Confirm whether SPARC is in scope. |

## 2. Is the mapping aligned with the business spec?

Target coverage today: Account 5 of 6, Client 12 of 13, Household 5 of 8 approved. All 12 required fields have a mapping, but one is wrong (item 1).

### Misalignments to fix or decide

| # | Target field | Platform today | Spec says | Action |
|---|---|---|---|---|
| 1 | ACCOUNT.FUND_NAME (required) | ACCT_NAME | CustodianName: "Fund Name should match the institution that custodies the account" | Change to the custodian name source. |
| 2 | CLIENT.PHONE_HOME | unmapped | Household_Contacts.Phone, Direct | Map PHONE to PHONE_HOME. A canonical field for it is needed, or reuse PHONE. |
| 3 | CLIENT.PHONE_BUSINESS | PHONE | no source ("Not Required", blank) | Remove; it is the same source as item 2, so the phone is in the wrong field. |
| 4 | ACCOUNT.OPEN_CLOSED | unresolved, proposed | If ClosedDate is NULL then Open else Closed (rule exists in the business rules file, canonical has CLOSED_DATE) | Add canonical field OPEN_CLOSED derived by that rule, or map CLOSED_DATE through the rule; then approve. |
| 5 | HOUSEHOLD.HOUSEHOLD_FIRST_NAME | HH_DISPLAY_NAME direct | Part after the comma in the combined name (business rule), or FirstName of the order-1 contact (newer sheet) | Use the approved SPLIT_PART rule; the rule exists but is not applied. |
| 6 | HOUSEHOLD.HOUSEHOLD_NAME | HH_NAME direct | "Confirm which one MCL expects": HouseholdName or HouseholdProperName (example value "Bob & Susan Smith" suggests the proper/display name) | Business decision: which source feeds it. |
| 7 | CLIENT.REGISTRATION_NAME | LEGAL_NAME (first, middle and last name joined) | FirstName + LastName | Rule says first and last only; middle name is currently included. Decide. |
| 8 | ACCOUNT.CUSTODIAN (required) | CUSTODIAN_CODE (name to code via business rule; only Schwab confirmed, TDA user-supplied) | CustodianName, direct, "Custodian and Account Number copied as-is" | Confirm Simplicity wants a code and not the name; other custodians still need codes. |

### Gaps the spec says have a source, but the platform leaves open

| Target field | Spec source | State |
|---|---|---|
| HOUSEHOLD.HOUSEHOLD_DOB | Household_Contacts.DateOfBirth where HouseholdOrder = 1 | No canonical Household.DOB; gap report offers a change request. |
| HOUSEHOLD.HOUSEHOLD_SSN (can be blank) | Household_Contacts.TaxId where HouseholdOrder = 1 | Not mapped (the proposed HH_ID at 60% was correctly not approved). |
| HOUSEHOLD.HOUSEHOLD_LAST_NAME | LastName where HouseholdOrder = 1 | Mapped through the HH name split instead; same result except edge cases. |
| ORION_HOUSEHOLD_ID | "Only when loading into an existing household; otherwise blank" | Correctly left blank (target-assigned). |
| CLIENT.PHONE_BUSINESS | none | Correct to leave blank. |

The "where HouseholdOrder = 1" pattern (household fields taken from the head contact) is not expressed anywhere in the platform's mappings yet. It is needed for HOUSEHOLD_DOB, HOUSEHOLD_SSN and, strictly, HOUSEHOLD_FIRST_NAME and LAST_NAME.

### Two specs disagree

`Amplify_Data_Definitions` (10 tables) says address, DOB, SSN, email and phone are "Not Available". `Data_Mapping_MCLSPARC` (with Household_Contacts) maps them. The platform follows the newer one, which is right.

## 3. Business rules file vs the spec

- Rules in `orion_mcl_business_rules.md` that the platform recognises: Open/Closed, Rollover IRA, first/last name split, concatenation, custodian and account number as-is, Outside ID, Representative Name via AdvisorId. All are inferred as governed rules.
- "Registration Name: remove everything after ' - ' in AccountName" belongs to the older spec (account name parsing). The newer spec takes registration names from the contacts, so this rule is not used. Remove it from the file or mark it superseded.
- "Rollover IRA to IRA Rollover; anything else is rejected" conflicts with the registration crosswalk approved in Run 2 (Individual, Trust, Roth IRA, Inherited IRA and others are mapped). The crosswalk is the intended behaviour; update the rule text.

## 4. Proposed next steps

1. Fix items 1, 2, 3 and 5 (clear spec answer, platform change only).
2. Decide items 4, 6, 7 and 8 with the business owner.
3. Raise canonical change requests for Household DOB and SSN (and PHONE_HOME if a separate field is wanted), then approve as Domain SME.
4. Add the order-1 head-contact derivation for household-level fields.
5. Clean up the two stale rule lines above.
6. Confirm whether SPARC is in scope.

## 5. Fixed in Run 2 (2026-11-12, Target mapping, as IMO Lead)

| Item | Change |
|---|---|
| 1 FUND_NAME | Reopened and approved from CUSTODIAN_NAME (DIRECT_COPY). |
| 2 PHONE_HOME | Approved from PHONE (DIRECT_COPY). |
| 3 PHONE_BUSINESS | Reopened and rejected: the spec has no source for it. |
| 5 HOUSEHOLD_FIRST_NAME | Reopened and approved from HH_NAME with rule INF_SPLIT_PART_F1044D@1 (part after the comma). |

Effects: the saved ADF package is stale (the approved set changed) and the pipeline shows stage 06 needing approval again. Regenerate the package, open a pull request, merge, publish, and rerun the batch and stage 10 before relying on these. These four came from target adapter version 2, so the adapter itself still holds the old choices; a Platform Admin should correct it so new migrations start right. Items 4, 6, 7 and 8 and the order-1 head-contact fields are still open.

## 6. Open items answered from the source specs, and applied (Run 2)

| Item | Answer in the specs | Applied |
|---|---|---|
| 4 OPEN_CLOSED | MCL sheet: if ClosedDate is NULL then Open, else Closed; data dictionary: ClosedDate null (73%) means the account is open | CLOSED_DATE with rule INF_IF_BLANK_E9903B@1, approved. No canonical change needed. |
| 6 HOUSEHOLD_NAME | Data dictionary: HouseholdProperName is the formatted display name ("Ms. Suzanne Smith"); HouseholdName is the internal sortable "Last, First" | HH_DISPLAY_NAME, DIRECT_COPY, approved. HouseholdProperName is empty for 0.6% of households; watch for blanks at validation. |
| 7 REGISTRATION_NAME | MCL sheet: FirstName + LastName | FIRST_NAME with rule INF_CONCAT_D9CCCC@1 (first and last joined by a space), approved. Middle name is no longer included. |
| 8 CUSTODIAN | MCL sheet: Direct Match from CustodianName; tooltip: custom custodians may be added; source has 73 custodian names | CUSTODIAN_NAME, DIRECT_COPY, approved. The custodian-code business rule and CUSTODIAN_CODE decisions no longer feed the target. |
| HOUSEHOLD_DOB, HOUSEHOLD_SSN | Household_Contacts.DateOfBirth and TaxId of the contact with HouseholdOrder = 1 (1 = primary/head per the dictionary); both optional | Not applied: the canonical Household has no such fields. Needs canonical change requests, then a head-contact derivation. |

Everything in section 5 and 6 changed the approved set, so the package, pull request, publish, batch and stage 10 must be rerun for Run 2. The target adapter version 2 still carries the old choices and should be corrected by a Platform Admin.
