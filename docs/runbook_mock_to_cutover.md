# Operational Runbook: Core Cap to Simplicity Wealth Data Migration

## Program Overview
- **Migration ID:** MIG-CORECAP-2026
- **Acquisition Scope:** Core Cap (~120 Advisors) -> Simplicity Wealth
- **Target Systems:** Salesforce SPARC (CRM) & Orion Eclipse (Trading & Portfolio Accounting)
- **Custody Constraint:** Charles Schwab (All accounts remain custodied at Schwab; no custodial asset transfer)

---

## 1. Pre-Cutover Preparation & Discovery
1. Verify Control Plane database connectivity and run seed script `02_seed_canonical_data.sql`.
2. Confirm source file availability in `data_plane/storage/` (`amplify_sleeves.csv`, `amplify_positions.csv`).
3. Validate mapping catalog and Rep-as-PM sleeve math rule `MAP-SLEEVE-001.md`.

---

## 2. Execution Phases

### Phase A: Mock 1 (Pilot Cohort - WAVE-00)
1. Run Mock 1 for Pilot Cohort (5 Advisors) using `scripts/generate_mock_data.py`.
2. Execute automated unit & integration test suites:
   ```bash
   python -m pytest