# P0 — Foundation Correctness

## Scope

P0 establishes a trustworthy source-to-canonical mapping workflow. It does not execute target-side loads, cutover, reconciliation, or migration-wave assignment.

## Required behavior

- Only explicitly `IN_SCOPE` entities are sent to the mapping orchestrator.
- An empty scope blocks execution.
- A missing source extract produces `NO_SOURCE_DATA`; no placeholder account, household, or position records are fabricated.
- Every run has a unique run ID.
- Mapping run and field-lineage metadata must persist successfully. A persistence error rolls back and propagates instead of being logged and ignored.
- Wave-condition definitions can be listed, but evaluation/assignment is disabled until approved source telemetry is available.
- ADF mock submissions return `MOCKED`, not `SUCCESS`.
- Missing ADF configuration returns `NOT_CONFIGURED`.
- A real ADF submission returns `SUBMITTED` only if Azure returns a run ID; it does not imply execution completion.
- ADF trigger/status lookup errors return `FAILED`, never synthetic success.
- Target-side ADF execution is blocked by the P0 API boundary.

## Validation

Run:

```bash
pytest -q
```

P0 acceptance tests cover explicit scope gating, no synthetic source records, mapping-only execution boundaries, disabled wave assignment, and honest ADF mock status.

## Known follow-up work

- Exercise the uploaded-file → profile → mapping path with a clean environment and verify lineage in the UI.
- Expand UI progress states for `BLOCKED`, `NO_SOURCE_DATA`, `NEEDS_REMEDIATION`, `MAPPED`, `MOCKED`, and `FAILED`.
- Extend the shared mapping registry and scope model to all configured entities.
- Add CI execution and test against a clean database/storage directory before merge.
