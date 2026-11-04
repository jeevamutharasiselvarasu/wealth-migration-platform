# CoreCap to Orion MCL - business rules (as stated in Data_Mapping_MCLSPARC.xlsx, MCL - Amplify Mapping)
# Each non-comment line is one rule statement; the platform infers governed rules from them.
If ClosedDate is NULL, then Open Else Closed
Map 'Rollover IRA' -> 'IRA Rollover'; anything else is rejected
Registration Name: remove everything after ' - ' in AccountName
Household first name is the part after the comma in the combined name
Household last name is the part before the comma in the combined name
Concatenate FirstName and LastName with a space
Custodian and Account Number are copied as-is
Outside ID is the source AccountId
Representative Name is the advisor Name joined through AdvisorId
