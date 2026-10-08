---
rule_id: VAL-AUM-DRIFT-001
title: AUM Variance and Reconciliation Control Rule
scope: validation
entity: Position / Account
status: APPROVED
version: 1.0.0
owner: Validation Specialist
effective_from: 2026-10-01
approval_required: true
---

# Rule Title: Post-Load AUM Variance Control

## Intent
Assert that post-load account market values in Orion staging match source Amplify position values within strict risk tolerances prior to cutover.

## Inputs
- amplify_staged_positions.market_value
- orion_staged_positions.market_value
- account_number

## Logic
1. Aggregate total AUM by advisor cohort in Amplify (AUM_source).
2. Aggregate total AUM by advisor cohort in Orion staging (AUM_target).
3. Calculate Variance Percentage:
   Variance_Pct = (|AUM_source - AUM_target| / AUM_source) * 100
4. Evaluate Gate:
   - If Variance_Pct <= 0.5000%, mark as PASS.
   - If Variance_Pct > 0.5000%, trigger VALIDATION_HOLD and auto-rollback.

## Edge Cases
- **Missing Market Price:** Flag security records missing updated price as HIGH severity exception.
- **Unmapped Cash Balances:** Cash accounts must be explicitly reconciled against Orion cash equivalents.

## Approval
- **Approved By:** Chief Risk Officer & Migration Lead
- **Status:** APPROVED
- **Timestamp:** 2026-09-30T10:30:00Z