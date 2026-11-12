# CoreCap (Amplify) to Orion MCL - business rules, decisions and facts from the source specs
#
# This file carries everything the four spec workbooks say, so a migration can run from this file alone (no Excel):
#   Data_Mapping_MCLSPARC.xlsx    - "MCL - Amplify Mapping", "SPARC - Amplify Mapping", "Header Tooltips", "Summary"
#   Amplify_Data_Dictionary.xlsx  - every column, null rates, relationships, known data-quality issues
#   Amplify_Data_Definitions.xlsx - "MCL Target Mapping", "RishiMCLMapping", "AmplifyToOrion MCL Mapping", "Field Mapping Amplify", "Entity Mapping"
#   CoreCapDecom_AllFiles.xlsx    - the sample data itself (18 sheets; four feed MCL: Accounts, Households, Household_contacts, Advisors)
#
# How the platform reads this file:
#   1. A plain line that is a transformation sentence becomes a DRAFT governed rule (section "Rules" below).
#   2. A plain line "Target <field> comes from <Entity> <SourceField> [using the rule "<rule sentence>"]" is a mapping statement:
#      Target mapping proposes that target field from the named source field, through the migration's approved source mapping.
#   3. Every "#" line is knowledge for people and agents: facts, open decisions, data-quality warnings. Nothing in a "#" line is applied.
# A person approves every rule and every mapping. Where the specs disagree, both views are written down and the choice is marked.

# ============================ RULES (inferred by the platform) ============================

# Open/Closed: "If ClosedDate is NULL, then Open Else Closed" (MCL sheet). The dictionary agrees: ClosedDate null (73%) = account still open.
If ClosedDate is NULL, then Open Else Closed

# Household status: the dictionary says DeletedDate null (78%) = household still active/live.
If DeletedDate is NULL, then Active Else Deleted

# Registration Type: Amplify spells it 'Rollover IRA', Orion's controlled list spells it 'IRA Rollover' (Rishi sheet: a crosswalk is required).
Map 'Rollover IRA' -> 'IRA Rollover'; anything else is rejected
# The specs give only this pair. The other values CoreCap carries (IRA, Individual, Trust, Roth IRA, Inherited IRA, Inherited Roth IRA, Estate)
# need Orion's AvailableRegistrationTypes list, which no spec contains: a crosswalk file (orion_registration_crosswalk.md) supplies them.
# Null registration types (about 29% of accounts in the dictionary extract) are a data-quality exception, never defaulted.

# Household First Name and Last Name from the combined HouseholdName ('Last, First'; Rishi and Definitions sheets).
Household last name is the part before the comma in the combined name
Household first name is the part after the comma in the combined name
# Multi-person labels such as 'Bob & Susan Smith' do not parse cleanly: those households need manual review after parsing.

# Registration Name (MCL sheet: FirstName + LastName). Orion fills a blank Registration Name with first name, space, last name.
Concatenate FirstName and LastName with a space

# Representative Name: Households.AdvisorId joins to Users_Advisors.Id, take Name (Definitions: RESOLVED; Rishi: Join / Lookup).
Representative Name is the Name column of the Advisors file, joined through AdvisorId

# Household-level fields (Household SSN, DOB, first and last name, name) come from the contact with HouseholdOrder = 1, the household head.
# The condition travels with the rule, in the SQL as well as in memory: other contacts give no value.
Copy the value where HouseholdOrder = 1

# ====================== MAPPING STATEMENTS (read by Target mapping) ======================
# Source: MCL - Amplify Mapping (Mapping Status: Direct Match / Derived), confirmed by MCL Target Mapping and RishiMCLMapping.
Target Account Number comes from Account AccountNumber
Target Custodian comes from Account CustodianName
Target Fund Name comes from Account CustodianName
Target Registration Type comes from Account RegistrationType
Target Open/Closed comes from Account ClosedDate using the rule "If ClosedDate is NULL, then Open Else Closed"
Target Outside ID comes from Account AccountId
Target Address Line 1 comes from Client Address1
Target Address Line 2 comes from Client Address2
Target City comes from Client City
Target State comes from Client State
Target Zip Code comes from Client Zip
Target Email Address comes from Client Email
Target Phone Home comes from Client Phone
Target Registration SSN comes from Client TaxId
Target Registration DOB comes from Client DateOfBirth
Target Registration First Name comes from Client FirstName
Target Registration Last Name comes from Client LastName
Target Registration Name comes from Client FirstName using the rule "Concatenate FirstName and LastName with a space"
Target HH Grouping Code comes from Household Id
Target Household Name comes from Household HouseholdProperName
Target Household First Name comes from Household HouseholdName using the rule "Household first name is the part after the comma in the combined name"
Target Household Last Name comes from Household HouseholdName using the rule "Household last name is the part before the comma in the combined name"
Target Representative Name comes from Household AdvisorId using the rule "Representative Name is the Name column of the Advisors file, joined through AdvisorId"

# ============================ FACTS AND DECISIONS (knowledge) ============================

# --- The target: Orion Master Client List (MCL), 27 fields, 12 required ---
# Required: HH Grouping Code, Household Last Name, Representative Name, Address Line 1, City, State, Zip Code, Registration Last Name,
# Registration Type, Custodian, Account Number, Fund Name. All other fields are optional (Open/Closed is "Optional").
# HH Grouping Code is conditional: one of HH Grouping Code, Household SSN or Address Line 1 is required, depending on the grouping chosen at import.
# Orion generates its own client ids; the HH Grouping Code number is only used to group accounts into a household.
# Orion Household Id: populate only when loading a new account into an existing Orion household; otherwise leave blank (system-generated).
# Household SSN and Registration SSN: leave blank where the SSN or Tax ID is unavailable; may be formatted with or without dashes; text value.
# First names may be blank for company names. Registration DOB: apply when tracking RMD. Email is required only if the client wants e-statements.
# Phone Home and Phone Business: any format, with or without dashes. Phone Business has no source in the specs: leave blank.
# Household Name: if blank, Orion uses Household First Name, space, Household Last Name. It is the title shown on the client's statement.
# Registration Type: keep generic; do not create custom descriptions per client. Custom registration types and custodians may be added in Orion.
# Custodian is commonly the data feed the account flows from. Fund Name should match the institution that custodies the account, in most cases
# the Custodian name. Open/Closed: if not populated, Orion assumes the account is active and funded.
# Outside ID: an internal account identifier of the source system. The MCL sheet leaves it unmapped; the Rishi sheet recommends the Amplify AccountId
# GUID so every migrated account keeps a traceable link for reconciliation. Decision taken: AccountId.
# Country: if a country is not in Orion's list, leave State blank and fill Address Line 1, Address Line 2 and Zip Code. MCL State is the 2-letter code.
# Orion's lists AvailableRegistrationTypes, AvailableCustodians and AvailableFundNames are not in the specs: values must be checked against them
# before import (Rishi sheet: validate Registration Type and Custodian). Review 'TD Ameritrade' given the Schwab / TD Ameritrade consolidation.

# --- Where the specs disagree (the choice taken is stated) ---
# Fund Name: the MCL sheet maps CustodianName (the tooltip says it should match the custodian); the Rishi sheet says there is no equivalent and it
# stays blank for SMA / multi-holding accounts; the Definitions sheet lists four candidates and asks for a business decision. Taken: CustodianName.
# Household First Name, Last Name, Name, SSN and DOB: the MCL sheet takes them from Household_Contacts where HouseholdOrder = 1 (FirstName,
# LastName, FirstName + LastName, TaxId, DateOfBirth); the Rishi and Definitions sheets parse the combined HouseholdName and say SSN and DOB do not
# exist. Taken: names by parsing HouseholdName; Household Name from HouseholdProperName (the formatted display name, e.g. 'Ms. Suzanne Smith').
# Household SSN and Household DOB need the order-1 contact's TaxId and DateOfBirth, and the canonical Household has no such fields yet.
# Representative Name: the MCL sheet row says Household_Contacts FirstName where HouseholdOrder = 1, which reads as an error; the Definitions
# and Rishi sheets join AdvisorId to the advisor Name. Taken: the advisor join.
# The earlier Definitions analysis (10 source tables) says address, DOB, SSN, email and phone are "Not Available"; the newer MCL sheet maps them from
# Household_Contacts. Taken: the newer sheet. Registration First/Last/Name from AccountName ("strip ' - <type> - <method>' and split on the comma")
# is the older approach and is superseded by Household_Contacts FirstName and LastName.

# --- Source data facts (Amplify_Data_Dictionary) ---
# Households.Id is the key; Accounts.HouseholdId and Household_Contacts.HouseholdId point to it. 7 accounts and 34 contacts have a HouseholdId
# with no matching household. Users_Advisors.Id <- Households.AdvisorId.
# HouseholdName is the internal sortable name, 'Last, First'. HouseholdProperName is the formatted display name and is empty for 0.6% of households.
# Households.DeletedDate null = active; DeletedByUserName only when DeletedDate is set.
# Household_Contacts: HouseholdOrder 1 = primary / head, 2 = spouse or second owner. HouseholdRelationshipType is Head, Spouse, Child (22% null).
# EntityType holds only 'individual' (trusts and businesses are not identified by it). Zip is kept as text to preserve leading zeros. State is the
# 2-letter abbreviation; Country is a code (US, NO, AU). Address, city, state, zip are about 5.8% null. Salutation, Nickname, Suffix, Designations
# and JobTitle have no MCL field.
# Accounts.CustodianName has 73 distinct custodians in the full extract (Schwab, TD Ameritrade, Lincoln Financial Group, ...). RegistrationType has
# 24 distinct values in the dictionary; the sample file carries IRA, Individual, Trust, Roth IRA, Rollover IRA, Inherited IRA, Inherited Roth IRA, Estate.
# TaxStatus values: Taxable, TaxDeferred, NonTaxable. RepCode is the custodian's representative code (not a key to the advisor table).
# Accounts.AccountNumber: a small number of rows hold full account-name text instead of a number (data shifted); this is a data-quality exception at
# validation, not a mapping rule. A clean value fits 20 characters.
# Users_Advisors: Name includes shared or team logins and joint labels ('Howard Jr & Howard Sr', 'House Accounts (No Advisor)'): a business nuance to
# confirm before treating each as a person. A 'Z -' prefix is a sort convention and does not reliably mean inactive; IsActive is the flag. Confirm
# every AdvisorId resolves to an active advisor record.
# Not mapped to MCL and not needed: Accounts DefaultLiquidationMethod (only FIFO), LastDirectReconDate, RiskScore (mixed types, a known data-quality
# issue) and RiskScoreDate; Households RiskScore, RiskScoreDate, RiskToleranceScore(Date); the sleeve, holdings, target, model, strategy,
# performance and transaction sheets. Account_Sleeves, Account_Targets: SleeveClosedDate null = sleeve open.
# Known extract issues outside MCL: Models.CashTargetDriftUp and Account_Sleeve_ActiveHoldings.MarketValue are mis-stored as dates; CUSIP has
# overflow strings; Account_Sleeve_ActiveHoldings has an unlabelled empty column to drop.

# --- Left blank on purpose (the specs name no source, and nothing is invented) ---
# Orion Household Id (system generated) and Phone Business. The Rishi sheet's gaps for address, phone, email, SSN and DOB are superseded by the
# MCL sheet, which maps them from Household_Contacts.

# --- SPARC target (Data_Mapping_MCLSPARC, "SPARC - Amplify Mapping"; not loaded as a target in this platform) ---
# 34 fields, 22 required. Direct matches: Household Name from Households.HouseholdProperName; 1st owner first and last name, mobile phone, email,
# street address 1 and 2, city, zip, DOB, SSN from Household_Contacts; Custodian / Fund Family from Accounts.CustodianName; Account Number
# (Delivering Firm); Registration Type; Account Value from Accounts.Balance; Employment from Household_Contacts.EmployerName; Contributions and
# Distributions from Account Returns Additions and Withdrawals; Current Model from Models.Name; Model Symbol from Model_Weights.SymbolName; Gain or
# Loss from Realized Gain_Loss_Security_FIRM.Total Gain Loss (key: account number).
# Derived: 2nd owner name, phone, email, address, city, state, zip, DOB and SSN from Household_Contacts where HouseholdOrder = 2; Business or Trust Name,
# Trust Date (from DateOfBirth) and Trust or Business EIN (from TaxId) where FirstName contains 'Trust'; Total Fee = Market Value x Account Fee
# Schedule (CoreCap_Billing_Offboarding). SPARC wants State as the full name (no abbreviations), unlike MCL.

# --- Simplicity file layout (Amplify_Data_Definitions, "Field Mapping Amplify"; a different target from MCL) ---
# Users_Advisors to RepresentativeUpdate_Simplicity: Id to Representative ID, Name to Full Name, Title to Job Title, Email to Email, SMSPhone to
# Mobile, IsActive to Active (Designations: none). Households to HouseholdUpdate: Id to Household ID, AdvisorId to Representative ID, HouseholdName
# to Full Name, HouseholdProperName to Report Name, CreatedDate to Start Date; RiskToleranceScore to RegistrationSuitabilityUpdate_Simplicity Risk
# Tolerance. Accounts to AccountUpdate_Simplicity: AccountId to Account ID, AccountNumber to Account Number, AccountName to Nickname, CustodianName to
# Custodian, TaxStatus to Qualified Plan, ClosedDate to Account Status; Balance to StoredActivity_Simplicity Ending Market Value; HouseholdId to
# ReportingOnlyAccounts_Simplicity Household ID; RepCode to HouseholdUpdate Representative ID; RiskScore to RegistrationSuitabilityUpdate_Simplicity
# Risk Exposure. Strategies, Models, Model_Weights, Account_Sleeves, Account_Targets, Account_Sleeve_ActiveHoldings and Account_Sleeve_History map to
# SeparatelyManaged, ModelAggregatePermissionsImport, ProductSub-Holdings, AssetUpdate, StoredPerformance and Account Returns files. Fields with
# no Simplicity field: Designations, RegistrationType, DefaultLiquidationMethod, LastDirectReconDate, ReportedAsOfDate, CustodialStartDate,
# RiskScoreDate, DeletedDate, DeletedByUserName and the sleeve date fields.
# Entity mapping: Users_Advisors to Users (Advisors); Accounts to Account (with Household, advisor, risk score); Households to Household (with
# advisor, risk score); Strategies, Models, Model_Weights, Account_Targets, Account_Sleeves, Account_Sleeve_ActiveHoldings and
# Account_Sleeve_History to the strategy, model, weight, position and performance entities.

# --- Coverage of the MCL sheet's own summary: 27 of 27 target fields addressed (12 of 12 required, 15 of 15 optional); 25 matched to source. ---
