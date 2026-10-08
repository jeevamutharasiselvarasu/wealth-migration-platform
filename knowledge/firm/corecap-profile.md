---
asset_type: firm_profile
firm_id: FIRM-CORECAP
firm_name: Core Cap Wealth Management
acquisition_id: ACQ-SIMPLICITY-2026
version: 1.0.0
---

# Firm Profile: Core Cap

## Operating Parameters
- **Advisor Population:** 120 Reps
- **Operating Model:** Rep-as-PM (Representative as Portfolio Manager)
- **Primary Custodian:** Charles Schwab (Retained — No asset transfer)

## Platform Architecture
- **Source TAMP:** Amplify (Rep-as-PM sleeve allocations and model aggregates)
- **Source Performance:** Black Diamond (Quarterly Performance Reports - QPR)
- **Target CRM:** Salesforce SPARC
- **Target Accounting/Rebalancer:** Orion Eclipse

## Constraints & Gates
- **Max AUM Drift Tolerance:** 0.5000%
- **Sleeve Allocation Sum Invariant:** Exactly 1.0000 (100.0%)