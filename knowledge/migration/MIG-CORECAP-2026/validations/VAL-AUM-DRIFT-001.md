---
rule_id: VAL-AUM-DRIFT-001
title: Post-Load AUM Variance Control Rule
scope: validation
entity: Position / Account
status: APPROVED
version: 1.0.0
---

# Rule Specification: Post-Load AUM Drift Gate

## Intent
Assert that post-load account market values staged in Orion match raw source position snapshots within strict risk tolerances prior to cutover sign-off.

## Gate Criteria
1. Compute Total Source Market Value ($AUM_{\text{source}}$).
2. Compute Total Staged Target Market Value ($AUM_{\text{target}}$).
3. Calculate Variance Percentage:
   $$V_{\text{AUM}} = \left| \frac{AUM_{\text{target}} - AUM_{\text{source}}}{AUM_{\text{source}}} \right| \times 100\%$$
4. Evaluate Gate:
   - $V_{\text{AUM}} \le 0.5000\% \implies \text{PASS}$
   - $V_{\text{AUM}} > 0.5000\% \implies \text{FAIL\_HOLD}$