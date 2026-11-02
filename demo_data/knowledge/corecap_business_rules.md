# CoreCap to Simplicity - migration business rules (demo)

Synthetic demo rules for the CoreCap pilot. Each line is a business rule that can be turned into a
draft transformation rule; none is approved until a Migration Analyst or Platform Admin approves it.

## Custodian codes
- Map SCH to SCHWAB, FID to FIDELITY, PER to PERSHING

## Registration types
- INDV_TAXABLE -> INDIVIDUAL; IRA_ROLL -> IRA_ROLLOVER

## Dates
- Account open dates are MM/DD/YYYY or YYYYMMDD

## Missing values
- Default to "UNKNOWN" if blank
