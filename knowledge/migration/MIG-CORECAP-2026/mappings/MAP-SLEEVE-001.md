---
rule_id: MAP-SLEEVE-001
title: Rep-as-PM UMA Sleeve Target Weight Normalization
scope: business_logic
entity: Sleeve / Allocation
status: APPROVED
version: 1.0.0
---

# Rule Specification: Sleeve Math Normalization

## Intent
Convert raw percentage allocation weights from Amplify TAMP extracts into normalized decimal weights for Orion Eclipse model compositions.

## Conversion Steps
1. Convert `TARGET_WEIGHT_PCT` (e.g., `70.0`) to decimal representation (`0.7000`).
2. Enforce sum invariant: `SUM(target_weight) = 1.0000` for each account.
3. If rounding drift occurs ($\Delta \le 0.0005$), adjust the primary sleeve by the residual difference ($\Delta$).