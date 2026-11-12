# CoreCap to Simplicity - migration business rules (demo)
#
# How inference reads this file: every line that does not start with "#" is a rule statement. Each one is matched
# against a fixed set of sentence forms and becomes a DRAFT governed rule; nothing is used until a Migration Analyst
# or Platform Admin approves it. Prose belongs in "#" comment lines like this one, never as a plain line.
#
# Sources: corecap_migration_decisions.md (derivations, missing values), corecap_source_dictionary.json (column meaning),
# simplicity_target_layouts.json (target formats), canonical_contract.md (Simplicity-MCL-v1).
# Rules for unknowns are deliberately absent: the decisions note says never invent defaults, so a blank required
# value stays an exception or information request instead of a "default to" rule.

# --- Custodian crosswalk (CUSTODIAN_CODE is derived from CustodianName through a versioned value map) ---
Map SCH to SCHWAB, FID to FIDELITY, PER to PERSHING

# --- Registration type crosswalk (REG_TYPE; about 29% null in the full extract, decide each null separately) ---
INDV_TAXABLE -> INDIVIDUAL; IRA_ROLL -> IRA_ROLLOVER
# The registration types CoreCap actually carries (docs/source_specs/CoreCapDecom_AllFiles.xlsx, Accounts; Amplify data dictionary) mapped to
# the canonical registration_type codes. The platform never sees the real rows, so every value that can occur is declared here.
Map IRA to IRA_TRADITIONAL; Individual to INDIVIDUAL; Roth IRA to IRA_ROTH; Rollover IRA to IRA_ROLLOVER; Inherited IRA to IRA_INHERITED; Inherited Roth IRA to IRA_INHERITED_ROTH; Trust to TRUST; Estate to ESTATE

# --- Dates (OpenDate, ClosedDate, DeletedDate, birth date): the extract mixes these layouts ---
Account open dates are YYYY-MM-DD, MM/DD/YYYY or YYYYMMDD

# --- Amounts (balances, fees): strip currency symbols and thousands separators ---
Strip $ and , from amounts and keep 2 decimals

# --- LEGAL_NAME: first, middle and last name joined; a blank first name is allowed for companies ---
Concatenate FIRST_NAME, MIDDLE_NAME and LAST_NAME with a space

# --- Orion target rules stated in docs/source_specs/orion_mcl_business_rules.md (they feed target fields that have no canonical field yet) ---
If ClosedDate is NULL, then Open Else Closed
Household last name is the part before the comma in the combined name
Household first name is the part after the comma in the combined name

# --- Household status (corecap_migration_decisions.md: HH_STATUS is Active when DeletedDate is empty, otherwise Deleted) ---
If DeletedDate is NULL, then Active Else Deleted

# --- Deliberately NOT written as rules: the decisions note lists these as open business decisions, and nothing is invented ---
# Owner of each account (no account-to-contact link; household head owns the accounts is a working assumption approved on Entity resolution),
# whether deleted households and closed accounts migrate, accounts with no registration type (about 29% null: exception, never defaulted),
# how trusts and businesses are identified (EntityType holds only individual), the SPARC Total Fee calculation (Market Value x Fee Schedule),
# SPARC full state names, AccountNumber holding name text (a data-quality exception at validation, not a mapping rule).
