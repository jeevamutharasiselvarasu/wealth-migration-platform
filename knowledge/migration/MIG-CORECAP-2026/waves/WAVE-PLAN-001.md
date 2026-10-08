---
rule_id: WAVE-PLAN-001
title: Core Cap 120-Advisor Sequential Cohort Wave Strategy
scope: wave_planning
entity: Wave
status: APPROVED
version: 1.0.0
owner: Operations Lead
effective_from: 2026-10-01
approval_required: true
---

# Rule Title: Sequential Wave Execution Plan

## Cohort Sizing & Sequence

| Wave ID | Cohort Name | Advisor Count | Complexity Profile | Target Cutover Date |
|---|---|---|---|---|
| WAVE-00 | Pilot Cohort | 5 Advisors | Standard Rep-as-PM models, high data quality | 2026-10-15 |
| WAVE-01 | Wave 1 | 25 Advisors | Standard Rep-as-PM models | 2026-10-22 |
| WAVE-02 | Wave 2 | 40 Advisors | Complex UMA Sleeve models | 2026-10-29 |
| WAVE-03 | Wave 3 | 50 Advisors | Remaining advisors and complex books | 2026-11-05 |

## Dependency Rules
1. A wave cannot proceed to CUTOVER_READY until the preceding wave successfully exits HYPERCARE or achieves signed acceptance.
2. Any advisor cohort with > 5 unresolved exceptions is automatically deferred to the next wave.