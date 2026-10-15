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

## MCL (Simplicity) end-to-end slice

The P0 source-to-canonical example supports three source files as one MCL flow:

| Source concept | Expected source file | Fixed canonical entity | Required canonical fields |
|---|---|---|---|
| Account | `accounts.csv` | Account | `ACCT_NUM`, `CUSTODIAN_CODE`, `REP_CODE`, `REG_TYPE`, `OPEN_DATE` |
| Household | `households.csv` | Household | `HH_ID`, `HH_NAME`, `REP_CODE` |
| Client | `clients.csv` | Party | `PARTY_ID`, `LEGAL_NAME`, `TAX_ID`, `PARTY_TYPE` |

Client is a source-system concept and maps to the existing canonical Party entity; it does not add a new canonical entity. The MCL batch endpoint requires all three uploads and explicit `IN_SCOPE` status (Client is gated by Party's scope). It profiles each exact uploaded CSV, preserves identifier columns as strings, maps source fields, and persists profile/run/field-lineage metadata. It then checks canonical key presence/uniqueness and validates only relationships evidenced by mapped columns: Account → Household, Account → Client/Party, and Household primary client → Party. Missing relationship columns are reported as `NOT_EVALUATED`, not guessed. Orphan references and duplicate/missing canonical keys are surfaced as remediation findings.

The field aliases currently in the mapper are starter candidates, not verified Simplicity rules. The supplied reference documents do not include Simplicity CSV samples or a field dictionary, so source-specific aliases, date formats, codes, and relationship semantics must be confirmed against those inputs before production use.

The batch result reports entity-level profiles/mappings, relationship checks, a shared MCL execution identifier, and explicit boundaries: target loading `NOT_RUN`, wave assignment `DISABLED`, target adapter `NOT_SELECTED_IN_P0`.

## Validation

Run:

```bash
pytest -q
```

P0 acceptance tests cover explicit scope gating, no synthetic source records, mapping-only execution boundaries, disabled wave assignment, honest ADF mock status, MCL source mapping, canonical key uniqueness, and relationship orphan detection.

## Known follow-up work

- Exercise the uploaded-file → profile → mapping path with a clean environment and verify lineage in the UI.
- Expand UI progress states for `BLOCKED`, `NO_SOURCE_DATA`, `NEEDS_REMEDIATION`, `MAPPED`, `MOCKED`, and `FAILED`.
- Confirm Simplicity field names, formats, code maps, and relationship semantics from actual sample CSVs and the data dictionary.\n- Extend the shared mapping registry and scope model to all configured entities.
- Add CI execution and test against a clean database/storage directory before merge.
