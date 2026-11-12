# CoreCap to Orion MCL - business rules for the demo (spec rules only)
#
# These are the rules the source specs state, copied from docs/source_specs/orion_mcl_business_rules.md, which is the master.
# Nothing here is invented: the custodian crosswalk, date, amount and name-join rules from earlier demo notes were removed
# because the specs do not state them. The Representative Name lookup is only in the master file (it needs a confirmed
# relationship between the households and advisors files).
#
# How inference reads this file: every line that does not start with "#" is a rule statement matched against a fixed set
# of sentence forms and becomes a DRAFT governed rule that a person approves.

If ClosedDate is NULL, then Open Else Closed
Map 'Rollover IRA' -> 'IRA Rollover'; anything else is rejected
Household last name is the part before the comma in the combined name
Household first name is the part after the comma in the combined name
Concatenate FirstName and LastName with a space
