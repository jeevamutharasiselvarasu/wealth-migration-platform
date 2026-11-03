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

# --- Dates (OpenDate, ClosedDate, DeletedDate, birth date): the extract mixes these layouts ---
Account open dates are YYYY-MM-DD, MM/DD/YYYY or YYYYMMDD

# --- Amounts (balances, fees): strip currency symbols and thousands separators ---
Strip $ and , from amounts and keep 2 decimals

# --- LEGAL_NAME: first, middle and last name joined; a blank first name is allowed for companies ---
Concatenate FIRST_NAME, MIDDLE_NAME and LAST_NAME with a space
