# CoreCap to Orion MCL - business rules (as stated in the source specs)
#
# Sources: Data_Mapping_MCLSPARC.xlsx ("MCL - Amplify Mapping" and "Header Tooltips"), Amplify_Data_Dictionary.xlsx,
# Amplify_Data_Definitions.xlsx ("MCL Target Mapping"). Every rule the specs state is here.
#
# How inference reads this file: each line that does not start with "#" is one rule statement. The platform matches it
# against a fixed set of sentence forms and proposes a DRAFT governed rule; a Migration Analyst or Platform Admin approves it.
# Prose and copy-only facts stay in "#" lines, never as plain lines.

# --- Account: Open/Closed (MCL "Open/Closed"; dictionary: a null ClosedDate means the account is still open) ---
If ClosedDate is NULL, then Open Else Closed

# --- Account: Registration Type (MCL "Registration Type", Direct Match from Accounts.RegistrationType) ---
Map 'Rollover IRA' -> 'IRA Rollover'; anything else is rejected
# The spec states only this pair. The other registration types CoreCap carries are mapped in the registration crosswalk
# (orion_registration_crosswalk.md / corecap_business_rules.md), which supersedes "anything else is rejected" for them.

# --- Household names (the combined HouseholdName is 'Last, First'; MCL Household First Name and Last Name derive from it) ---
Household last name is the part before the comma in the combined name
Household first name is the part after the comma in the combined name

# --- Registration Name and Household Name (MCL: FirstName + LastName) ---
Concatenate FirstName and LastName with a space

# --- Representative Name (MCL: Households.AdvisorId joins to Users_Advisors.Id, take Name) ---
Representative Name is the Name column of the Advisors file, joined through AdvisorId

# --- Copied as-is, no transformation (these are mapping choices the platform shows on Target mapping; no rule is needed) ---
# Custodian is CustodianName from Accounts, copied as-is (MCL Direct Match). Custom custodians may be added in Orion.
# Fund Name is the same CustodianName ("Fund Name should match the institution that custodies the account").
# Account Number is AccountNumber from Accounts, copied as-is.
# Outside ID is the source AccountId.
# Registration Type, Registration SSN (TaxId), Registration DOB (DateOfBirth), Registration First Name and Last Name
# (FirstName, LastName) and the address, city, state, zip, email and Phone Home (Phone) fields are Direct Match from
# Household_Contacts.
# Household Name is the formatted display name, Households.HouseholdProperName (the dictionary: HouseholdName is the
# internal sortable 'Last, First').
# HH Grouping Code is Households.Id.
# Household SSN and Household DOB are the TaxId and DateOfBirth of the contact with HouseholdOrder = 1 (the head); both optional.

# --- Left blank on purpose (the specs name no source, and nothing is invented) ---
# Orion Household Id (only set when adding an account to an existing Orion household), Phone Business.
