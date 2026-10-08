# Migration Overview: Core Cap to Simplicity Wealth

## Migration Scope
- **Migration ID:** MIG-CORECAP-2026
- **Acquisitions:** Core Cap -> Simplicity Wealth
- **Target Completion Date:** 2026-11-15

## Governing Constraints
1. **Platform Transfer Only:** No custodian transfer is required. Accounts remain at Charles Schwab.
2. **Rep-as-PM Model Restructuring:** Amplify custom sleeve fractions must be restructured into Orion Model Aggregates via deterministic math.
3. **Control/Data Isolation:** Client production data processes entirely inside the Data Plane; Control Plane manages configuration and rules.