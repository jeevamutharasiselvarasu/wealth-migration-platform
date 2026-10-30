# P0 — Foundation Correctness

## Scope

P0 establishes a trustworthy source-to-canonical mapping workflow. It does not execute target-side loads, cutover, post-load reconciliation, or migration-wave assignment.

## Required behavior

- Only explicitly `IN_SCOPE` entities (Account, Client, Household) are processed; an empty scope blocks execution.
- The legacy cohort pipeline is retired: `POST /api/pipeline/run`, `/api/hitl/approve` and `/api/hitl/reject` return a `409` boundary response. Mapping review happens in Source Discovery, Mapping Studio and Mapping Review.
- Mapping run and field-lineage metadata must persist successfully; a persistence error propagates instead of being logged and ignored.
- Wave-condition definitions can be listed (`GET /api/waves/conditions`), but `POST /api/waves/evaluate-record` returns `409 DEFERRED_SCOPE` with `wave_assignment: DISABLED` until approved source telemetry is available.
- ADF mock submissions return `MOCKED`, not `SUCCESS`. Missing ADF configuration returns `NOT_CONFIGURED`. A real submission returns `SUBMITTED` only if Azure returns a run ID; it does not imply completion. Trigger/status lookup errors return `FAILED`, never synthetic success. Azure/ADF is integration scaffolding until the Azure phase.
- State-changing routes are gated by the active persona (`ui/role_policy.py`); a test fails if a POST route is unclassified. Reviewer identity comes from the authenticated session.

## MCL (Simplicity) end-to-end slice

The P0 source-to-canonical example supports three source files as one MCL flow:

| Source concept | Expected source file | Fixed canonical entity | Required canonical fields |
|---|---|---|---|
| Account | `accounts.csv` | Account | `ACCT_NUM`, `CUSTODIAN_CODE`, `REP_CODE`, `REG_TYPE`, `OPEN_DATE` |
| Household | `households.csv` | Household | `HH_ID`, `HH_NAME`, `REP_CODE` |
| Client | `clients.csv` | Client | `CLIENT_ID`, `LEGAL_NAME`, `TAX_ID`, `CLIENT_TYPE` |

Client is one of the three canonical entities in the current P0 pilot. The MCL batch endpoint requires all three uploads and explicit `IN_SCOPE` status for Account, Household, and Client. It profiles each exact uploaded CSV, preserves identifier columns as strings, maps source fields, and persists profile/run/field-lineage metadata. It then checks canonical key presence/uniqueness and validates only relationships evidenced by mapped columns: Account → Household and Account → Client where those fields are present and mapped. Client `HOUSEHOLD_LINK_ID`, when present, is emitted as a proposed Client-to-Household relationship candidate and checked for orphan references; it is not written onto the canonical Client record and remains unapproved until Simplicity source documentation confirms its meaning. Missing relationship columns are reported as `NOT_EVALUATED`, not guessed. Orphan references and duplicate/missing canonical keys are surfaced as remediation findings.

The field aliases currently in the mapper are starter candidates, not verified Simplicity rules. The supplied reference documents do not include Simplicity CSV samples or a field dictionary, so source-specific aliases, date formats, codes, and relationship semantics must be confirmed against those inputs before production use.

The batch result reports entity-level profiles/mappings, relationship checks, a shared MCL execution identifier, and explicit boundaries: target loading `NOT_RUN`, wave assignment `DISABLED`, target adapter `NOT_SELECTED_IN_P0`.

## Validation

Run:

```bash
pytest -q
```

Acceptance tests cover scope gating, mapping-only execution boundaries, disabled wave assignment, honest ADF mock status, MCL source mapping, canonical key uniqueness, relationship orphan detection, role policy, canonical change requests and workflow persistence across restart.

## Known follow-up work

- Confirm Simplicity field names, formats, code maps and relationship semantics from actual sample CSVs and the data dictionary.
- Verify persistence across a full process stop/start and Excel/JSON uploads in a browser.
- Decide the migration reset procedure and the `REP-` prefix rule (see `docs/project/XODUS_CURRENT_STATUS.md`).
- Azure phase: verify ADLS/ADF connectivity before any cloud status is presented as live.
