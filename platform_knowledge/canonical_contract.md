# Demo canonical contract — version demo-canonical-v1 (aligned to config contract 1.1.0)

This is illustrative demo input, not an approved production schema. The runtime contract is config/firm_config.yaml.

## Account
Candidate fields: ACCT_NUM, CUSTODIAN_CODE, REG_TYPE, OPEN_DATE, REP_CODE (optional), ACCOUNT_ID, ACCT_NAME, CUSTODIAN_NAME, HH_ID, BALANCE, BALANCE_AS_OF, TAX_STATUS, ACCT_STATUS, CLOSED_DATE, CLIENT_ID, ACCOUNT_STATUS.

## Household
Candidate fields: HH_ID, HH_NAME, HH_DISPLAY_NAME, ADVISOR_ID, CREATED_DATE, HH_STATUS, DELETED_DATE, REP_CODE (optional), PRIMARY_CLIENT_ID, RELATIONSHIP_STATUS.

## Client
Candidate fields: CLIENT_ID, LEGAL_NAME, CLIENT_TYPE, TAX_ID, FIRST_NAME, MIDDLE_NAME, LAST_NAME, DATE_OF_BIRTH, HH_ID, HH_ORDER, HH_RELATIONSHIP, ADDRESS_LINE_1, ADDRESS_LINE_2, CITY, STATE, POSTAL_CODE, COUNTRY, PHONE, EMAIL, EMPLOYER_NAME, RELATIONSHIP_STATUS.

## Sensitive fields
Names, tax id, date of birth, address, phone and email are personal data. Never show their values in lineage, previews or logs.

## Review notes
Confirm mandatory fields, key definitions, allowed values and relationship cardinality with the target owner before approval. Do not infer tax identifiers or other sensitive fields from the sample.
