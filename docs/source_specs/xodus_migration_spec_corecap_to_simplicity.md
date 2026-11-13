# XODUS migration spec: CoreCap (Amplify) to Simplicity (orion and sparc)

Nov 13, 2026 · @Jeevamutharasi Selvarasu (LatentView)

## 1. Migration set-up

Create one migration that moves CoreCap (Amplify) data into two schemas of the Simplicity database: `orion` and `sparc`. The target is treated as tables, not flat sheets. Everything below comes from the four Excel files only.

| Setting | Value |
| --- | --- |
| Migration name | CoreCap to Simplicity (Orion MCL and SPARC) |
| Source system | CoreCap (Amplify / BlackDiamond export), workbook CoreCapDecom\_AllFiles |
| Source files | 8 files named by the SPARC mapping, plus Advisors for Orion |
| Target system | Simplicity database |
| Target schemas | `orion` (Orion Master Client List, 27 columns in 3 tables) and `sparc` (SPARC Household & Account, 34 columns in 5 proposed tables) |
| Direction | Source to target only |
| Entities | Household, Client, Account (both schemas); Account model and Gain/Loss (SPARC only) |
| Approval | All mappings stay DRAFT until a Migration Analyst approves them |

**Scope note.** The POC scope is Account, Client and Household. The SPARC tables for model, fee, contributions and gain/loss go beyond that and should be added only if requested.

**Sources of truth**

- Data\_Mapping - MCL\_SPARC.xlsx: column mapping for Orion (27 columns) and SPARC (34 columns). Primary source.
- Amplify\_Data\_Definitions.xlsx: the Rishi MCL mapping, the MCL target mapping and the entity mapping. Used to cross-check.
- Amplify\_Data\_Dictionary.xlsx: source column types and descriptions.
- CoreCapDecom\_AllFiles - Copy.xlsx: the sample data (18 sheets).

## 2. Source files

Eight source files feed the two targets. Advisors is a ninth file, needed only for Orion's Representative Name.

| # | Source file | CoreCap sheet | Columns | Used for |
| --- | --- | --- | --- | --- |
| 1 | Household | Households | 11 | Both |
| 2 | Account | Accounts | 16 | Both |
| 3 | Household\_Contacts | Household\_contacts | 24 | Both |
| 4 | CoreCap\_Billing\_Offboarding | Billing\_OffB | 40 | SPARC (Total Fee) |
| 5 | Account Returns | Performance\_Account (assumed) | 16 | SPARC (Contributions/Distributions) |
| 6 | Models | Models | 11 | SPARC (Current Model) |
| 7 | Model\_Weights | Model\_Weights | 7 | SPARC (Model Symbol) |
| 8 | Realized Gain\_Loss\_Security\_FIRM | not in the workbook | not provided | SPARC (Gain/Loss) |
| 9 | Advisors | Advisors | 8 | Orion (Representative Name) |

**Columns per file**

| File | All columns |
| --- | --- |
| Households | AdvisorId, Id, HouseholdName, HouseholdProperName, CreatedDate, RiskScore, RiskScoreDate, RiskToleranceScore, RiskToleranceScoreDate, DeletedDate, DeletedByUserName |
| Accounts | AccountId, AccountNumber, AccountName, Balance, CustodianName, TaxStatus, RegistrationType, DefaultLiquidationMethod, RepCode, HouseholdId, LastDirectReconDate, ReportedAsOfDate, CustodialStartDate, ClosedDate, RiskScore, RiskScoreDate |
| Household\_contacts | HouseholdId, ContactId, Salutation, FirstName, Nickname, MiddleName, LastName, Suffix, Designations, JobTitle, EmployerName, EntityType, DateOfBirth, TaxId, HouseholdOrder, HouseholdRelationshipType, Address1, Address2, City, State, Zip, Country, Phone, Email |
| Billing\_OffB | Used: Account Number, AccountID, Account Fee Schedule, Market Value. Other 36 columns cover billing configuration, portfolio fee schedule, flat fee and rep fee split. |
| Performance\_Account | AccountNumber, AccountName, PeriodDate, EMV, BMV, PeriodReturn, PeriodReturnNOF, BMReturn, Additions, Withdrawals, Fees, Income, NetGain, Accrual, ExContributions, ExWithdrawals |
| Models | Id, Name, OwnerUserName, CreatedDate, CreatedByUserName, CashTargetDriftUp, CashTargetDriftDown, RiskScore, RiskScoreDate, SharedWithEntireFirm, UserSharing |
| Model\_Weights | ModelId, SymbolId, SymbolType, SymbolName, AllocationPct, TargetDriftUp, TargetDriftDown |
| Advisors | Id, Name, Designations, Title, Email, SMSPhone, IsActive, RepCodes |

The Definitions file counts 10 source files instead. It adds Account\_Targets, Account\_Sleeves, Account\_Sleeve\_ActiveHoldings and Strategies, and omits Billing, Returns and Gain/Loss. Those four are not mapped to either target and are left out.

## 3. Target tables

The `orion` split follows the column order of the MCL sheet (8, then 13, then 6 columns). The `sparc` split is a proposal: the flat sheet repeats 1st and 2nd owner columns, so each owner becomes one client row. Table and column names are placeholders until Simplicity confirms them.

| Table | Columns | Count |
| --- | --- | --- |
| `orion.household` | HH Grouping Code, Orion Household Id, Household SSN, Household First Name, Household Last Name, Household Name, Representative Name, Household DOB | 8 |
| `orion.client` | Address Line 1, Address Line 2, City, State, Zip Code, Email Address, Phone Home, Phone Business, Registration SSN, Registration DOB, Registration First Name, Registration Last Name, Registration Name | 13 |
| `orion.account` | Registration Type, Custodian, Account Number, Fund Name, Open/Closed, Outside ID | 6 |
| `sparc.household` | Household Name, Business or Trust Name, Trust Date, Business or Trust EIN | 4 |
| `sparc.client` | Owner order (1 or 2), First Name, Last Name, Mobile Phone, Email, Street Address, Street Address 2, City, State, Zip Code, DOB, SSN, Employment | 12 plus order |
| `sparc.account` | Custodian / Fund Family, Account Number, Registration Type, Account Value, Total Fee, Contributions/Distributions | 6 |
| `sparc.account_model` | Current Model, Model Symbol | 2 |
| `sparc.account_gainloss` | Gain/Loss | 1 |

**Link keys are missing.** The flat sheets had one row per account, so they never needed keys. As tables:

- `orion.client` and `orion.account` have no household column. Only `orion.household` carries HH Grouping Code.
- `sparc.client` needs a household key and the owner order.
- `sparc.account_model` and `sparc.account_gainloss` need an account key.

Simplicity must decide these columns (decision D4).

## 4. Orion mapping: 27 columns

Source is the MCL sheet of Data\_Mapping - MCL\_SPARC.xlsx. "Order 1" means the contact with HouseholdOrder = 1. Notes show where the Rishi or Definitions sheets differ.

| Target table | Target column | Req. | Source | Rule | Note |
| --- | --- | --- | --- | --- | --- |
| orion.household | HH Grouping Code | Yes | Household\_Contacts.HouseholdId | Copy | Rishi: gap (D2) |
| orion.household | Orion Household Id | No | none | Not populated | Orion generates it |
| orion.household | Household SSN | Blank OK | Household\_Contacts.TaxId | Order 1 |  |
| orion.household | Household First Name | No | Household\_Contacts.FirstName | Order 1 | Rishi: split HouseholdName (D6) |
| orion.household | Household Last Name | Yes | Household\_Contacts.LastName | Order 1 | Same |
| orion.household | Household Name | No | FirstName + LastName | Order 1 | Rishi: HouseholdName or HouseholdProperName |
| orion.household | Representative Name | Yes | Households.AdvisorId, then Advisors.Id, then Advisors.Name | Lookup | The MCL sheet says FirstName, which looks like a copy error; Rishi and Definitions agree on the advisor path |
| orion.household | Household DOB | No | Household\_Contacts.DateOfBirth | Order 1 |  |
| orion.client | Address Line 1 | Yes | Household\_Contacts.Address1 | Copy |  |
| orion.client | Address Line 2 | No | Household\_Contacts.Address2 | Copy |  |
| orion.client | City | Yes | Household\_Contacts.City | Copy |  |
| orion.client | State | Yes | Household\_Contacts.State | Copy |  |
| orion.client | Zip Code | Yes | Household\_Contacts.Zip | Copy |  |
| orion.client | Email Address | No | Household\_Contacts.Email | Copy |  |
| orion.client | Phone Home | No | Household\_Contacts.Phone | Copy | Assumes Phone is a home number |
| orion.client | Phone Business | No | none | Not populated | Gap in all sheets |
| orion.client | Registration SSN | No | Household\_Contacts.TaxId | Copy |  |
| orion.client | Registration DOB | No | Household\_Contacts.DateOfBirth | Copy |  |
| orion.client | Registration First Name | No | Household\_Contacts.FirstName | Copy | Rishi: parse Accounts.AccountName (D7) |
| orion.client | Registration Last Name | Yes | Household\_Contacts.LastName | Copy | Same |
| orion.client | Registration Name | No | FirstName + LastName | Concatenate | Rishi: cleaned AccountName |
| orion.account | Registration Type | Yes | Accounts.RegistrationType | Copy, then crosswalk | `Rollover IRA` must become `IRA Rollover` (D3) |
| orion.account | Custodian | Yes | Accounts.CustodianName | Copy, then crosswalk | Check against Orion's custodian list (D3) |
| orion.account | Account Number | Yes | Accounts.AccountNumber | Copy |  |
| orion.account | Fund Name | Yes | Accounts.CustodianName | Copy | The sheet asks if Custodian and Fund can share a field; Rishi: blank (D1) |
| orion.account | Open/Closed | Optional | Accounts.ClosedDate | Blank means Open, otherwise Closed |  |
| orion.account | Outside ID | No | none in this sheet | Not populated | Rishi: Accounts.AccountId (D8) |

## 5. SPARC mapping: 34 columns

Source is the SPARC sheet of Data\_Mapping - MCL\_SPARC.xlsx. Each owner becomes one `sparc.client` row, filtered by HouseholdOrder (1 or 2).

| Target table | SPARC columns | Source | Rule |
| --- | --- | --- | --- |
| sparc.household | Household Name | Households.HouseholdProperName | Copy |
| sparc.household | Business or Trust Name | Household\_Contacts.FirstName | Only where FirstName contains "Trust" |
| sparc.household | Trust Date | Household\_Contacts.DateOfBirth | Same trust condition; a date of birth as a trust date looks wrong |
| sparc.household | Business or Trust EIN | Household\_Contacts.TaxId | Same trust condition |
| sparc.client | 1st Owner / TTEE First Name, Last Name | Household\_Contacts.FirstName, LastName | Order 1 |
| sparc.client | 2nd Owner / TTEE First & Last Name | Household\_Contacts.FirstName | Order 2 |
| sparc.client | 1st Client Mobile Phone, Email Address, Street Address, Street Address2, City, State, Zip Code, DOB, SSN (9 columns) | Household\_Contacts.Phone, Email, Address1, Address2, City, State, Zip, DateOfBirth, TaxId | Order 1, copy |
| sparc.client | 2nd Client Mobile Phone, Email, Street Address, City, State, Zip Code, DOB, SSN (8 columns) | Same contact columns | Order 2, copy |
| sparc.client | Employment | Household\_Contacts.EmployerName | Copy |
| sparc.account | Custodian / Fund Family | Accounts.CustodianName | Copy |
| sparc.account | Account Number (Delivering Firm) | Accounts.AccountNumber | Copy |
| sparc.account | Registration Type | Accounts.RegistrationType | Copy |
| sparc.account | Account Value | Accounts.Balance | Copy |
| sparc.account | Total Fee | Billing\_OffB: Market Value, Account Fee Schedule | Market Value x Account Fee Schedule; confirm what the fee schedule column holds |
| sparc.account | Contributions/Distributions Info | Account Returns: Additions, Withdrawals | Copy |
| sparc.account\_model | Current Model(s) | Models.Name | Copy |
| sparc.account\_model | Model Symbol | Model\_Weights.SymbolName | Copy |
| sparc.account\_gainloss | Gain/Loss | Realized Gain\_Loss: Total Gain Loss | Copy; file not provided |

The sheet covers 34 target columns: 1 + 2 + 1 + 3 + 4 + 9 + 8 + 6 = 34.

## 6. Transformation rules and relationships

Eight transformation types go beyond a plain copy. Each must be a registered rule that an analyst approves.

| Rule | Used by | Logic |
| --- | --- | --- |
| Order filter | Orion household and client; SPARC client | Keep the contact where HouseholdOrder = 1 (or 2) |
| Concatenate | Registration Name, Household Name | FirstName + LastName |
| Split on comma | Household First / Last Name (Rishi alternative) | `Smith, Suzanne` gives last name Smith, first name Suzanne |
| Open/Closed | orion.account | ClosedDate blank means Open, otherwise Closed |
| Lookup | Representative Name | Households.AdvisorId to Advisors.Id, return Name |
| Crosswalk | Registration Type, Custodian | Source value to Orion's list; `Rollover IRA` becomes `IRA Rollover` |
| Trust filter | SPARC trust name, date, EIN | Only where FirstName contains "Trust" |
| Multiply | SPARC Total Fee | Market Value x Account Fee Schedule |

**Relationships between source files**

| From | To | Join | Checked in sample |
| --- | --- | --- | --- |
| Accounts.HouseholdId | Households.Id | Key | 0 of 89 match |
| Household\_contacts.HouseholdId | Households.Id | Key | 14 of 20 match |
| Households.AdvisorId | Advisors.Id | Key | 20 of 20 match |
| Billing\_OffB.AccountID | Accounts.AccountId | Key | Not tested |
| Performance\_Account.AccountNumber | Accounts.AccountNumber | Account number only | Not tested |
| Model\_Weights.ModelId | Models.Id | Key | Not tested |
| Account to Model | none among the 8 files | Needs Account\_Targets.ModelId | Gap |
| Realized Gain\_Loss | Accounts | Account number | File not provided |

## 7. Open decisions for Simplicity

Eleven points are open: the mapping sheets contradict each other on some and are silent on others. Resolve each before a real load.

| # | Decision | What the sheets say | Answer |
| --- | --- | --- | --- |
| D1 | Fund Name: blank or custodian name? | MCL sheet: custodian name, with the question "can come from same field?". Rishi: blank, because the accounts are SMA and not single-fund. Required in Orion. |  |
| D2 | HH Grouping Code: household Id, generated, or blank? | MCL sheet: Household\_Contacts.HouseholdId. Rishi: gap, leave blank or auto-generate. Definitions: not available. |  |
| D3 | Orion's exact lists for Registration Type and Custodian | Rishi: Orion uses `IRA Rollover`, Amplify uses `Rollover IRA`; review TD Ameritrade entries after the Schwab merger. |  |
| D4 | Link columns between the tables | Neither sheet defines keys. `orion.client` and `orion.account` have no household column. |  |
| D5 | Are team-login advisors acceptable as Representative Name? | Definitions notes shared logins such as `Howard Jr & Howard Sr`. |  |
| D6 | Household name source | HouseholdName or HouseholdProperName; first and last name from contacts (MCL sheet) or from a split of the name (Rishi). |  |
| D7 | Registration name source | Contacts first and last name (MCL sheet) or parsed Accounts.AccountName (Rishi). |  |
| D8 | Outside ID | MCL sheet: not required. Rishi: Accounts.AccountId. Definitions: unclear. |  |
| D9 | Phone mapping | Phone Home takes Phone by assumption; Phone Business has no source in any sheet. |  |
| D10 | SPARC inputs not in the workbook | Realized Gain\_Loss file; identity of "Account Returns"; Account to Model link needs Account\_Targets. |  |
| D11 | Scope | SPARC model, fee, contributions and gain/loss go beyond Account, Client and Household. |  |

The Rishi and Definitions sheets also list address, phone, email, DOB and SSN as gaps. They predate the Household\_Contacts file, so those gaps are out of date.

## 8. Sample data observations and risks

The sample is small and padded with blank rows, so these are signals to check on the full data, not measurements. Counts are of real rows only.

| Area | Observation |
| --- | --- |
| Volume | 89 accounts, 20 contacts, 20 households, 273 advisors. The Accounts sheet has 38,373 rows, almost all blank. |
| Redaction | LastName, DateOfBirth, TaxId and Email read `[REDACTED]` in all 20 contacts. Last name is required in Orion, so those rows cannot pass as they stand. |
| Account to household | None of the 89 accounts has a HouseholdId found in Households or in contacts. |
| Household names | 5 of 20 have no comma, 6 are joint, 1 has two commas (`Farris, Matthew & McHenry, Dawn`), 1 is a test account, 1 is deleted. A comma split breaks on these. |
| Accounts | 31 of 89 are closed. 19 of 89 have a blank RegistrationType, which is required. 8 distinct types. Custodians: Schwab 81, TD Ameritrade 7, Lincoln Financial Group 1. |
| Advisors | 64 of 273 rows share an email with another row. Team logins such as `Clark & Maginn` share `brad.clark@corecapinv.com` with `Brad Clark`. Matching advisors by email would merge them. |
| Advisor links | All 20 households resolve to an advisor. |

**Before loading**

1. Treat `[REDACTED]` as blank and block rows that lack a required value.
2. Skip blank padding rows instead of loading them with empty keys.
3. Exclude or flag the test household and deleted households.
4. Decide D1 to D11 with Simplicity.
