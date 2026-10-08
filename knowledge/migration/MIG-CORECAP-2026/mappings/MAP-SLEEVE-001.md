---
rule_id: MAP-SLEEVE-001
title: Rep-as-PM UMA Sleeve Target Weight Normalization
entity: Sleeve/Allocation
status: APPROVED
version: 1.0.0
---

# Logic
1. Convert percentage target_pct (70.0) to decimal fraction (0.7000).
2. Assert Sum(target_weight) == 1.0000 for each account.
3. If rounding drift exists (e.g. 0.9999), adjust primary sleeve by residual difference.