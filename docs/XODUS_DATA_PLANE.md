# XODUS data plane: stable pipelines, mirror, gated promotion

Agreed 2026-11-10. Source changes only change SQL; pipeline definitions never change. Nothing reaches the live tables until a
batch is validated, signed off and promoted.

## Path of the data

| Step | Where | What | Built by |
|---|---|---|---|
| Sample files | Blob `sample-raw/<firm>/` | discovery, mapping and rules work from these | analyst |
| Real data | Blob `raw/<firm>/` | the firm's files, same structure as the samples | firm |
| Raw | XODUS DB `xodus_raw.<mig>_<file>` | every file as received, all columns text, plus trace columns (migration, batch, run, source file, load time) | `PL_XODUS_Load_Raw_File` |
| Canonical | `xodus_canon.<mig>_<Entity>` | one row per entity key, canonical column names | `usp_canon_<mig>_<Entity>` (generated) |
| Mirror | `xodus_mirror.<mig>_<LiveTable>` | the live tables' columns, values as text, exactly what will be promoted | `usp_mirror_<mig>` (generated) |
| Landing | Simplicity `xodus_landing.<mig>_<LiveTable>` | the mirror, copied across databases | `PL_XODUS_Promote` |
| Live | Simplicity `simplicity.*` | MERGE from landing, all tables in one transaction | `usp_promote_<mig>` (generated) |

Azure SQL Database has no cross-database queries, so mirror → live is a Copy activity into Simplicity's landing schema followed
by one transaction inside Simplicity.

## Pipelines (stable; parameters MigrationId, MigrationSlug, BatchId)

- `PL_XODUS_Run`: checks the scope, loads the raw files, then runs one wave after another. Started by XODUS through the service principal. Optional `Entities` reruns only those entities inside the latest batch.
- `PL_XODUS_Load_Raw`: loads the raw files (`xodus_ops.xodus_raw_files`), or only those the selected entities read.
- `PL_XODUS_Wave`: one wave (the entities at one dependency level, `xodus_ops.xodus_entity_registry`; none depends on another). Builds them in parallel (canonical, then mirror), then runs the blocking checks as SQL (`usp_validate_<mig>`, findings in `xodus_ops.xodus_validation_result` with failing row keys in `xodus_validation_row_key`). A failed check, unless waived (`xodus_wave_waiver`), fails the run before the next wave starts.
- `PL_XODUS_Load_Raw_File`, `PL_XODUS_Canonical_Entity`, `PL_XODUS_Canonical_To_Mirror`: one step each, logged.
- `PL_XODUS_Promote`: gate check, copy to landing, merge into live, record the result.
- `PL_XODUS_Setup_<mig>`: deploys this migration's SQL (the only pipeline that changes with the source).

Linked services (made by hand, managed identity): `LS_BLOB`, `LS_SQL_Target` (XODUS DB), `LS_SQL_Simplicity`.

## Flow in the platform

1. Stage 07: generate the package (stable pipelines + generated SQL, version = hash of the SQL).
2. Stage 08: open a pull request (branch `xodus/<mig>-<version>`); approve and merge it in XODUS (option b: the approver is recorded,
   the merge commit names them); publish in Data Factory Studio; run the setup pipeline; run the deployment check.
3. Stage 09: start a batch (refused unless the PR is merged, the deployment check passed and mapping validation passed).
4. Stage 10: target validation on the batch's canonical and mirror tables (row counts against the raw file, keys, required values,
   values fit the live types and lengths, reference lists, relationships); exceptions; sign-off. The sign-off writes the batch's
   mirror counts to `xodus_ops.xodus_promotion_gate` (the only table XODUS writes). Then "Promote to live" starts `PL_XODUS_Promote`,
   which refuses if the mirror no longer matches the signed-off counts.

## Deployment check (read-only)

XODUS DB version and objects match the package; Simplicity landing version matches; every live table and column the batch writes
exists; every NOT NULL live column has an approved mapping. XODUS never creates or alters live tables.

## Telemetry

`xodus_ops.xodus_pipeline_run` (one row per run), `xodus_run_log` (one row per step), `xodus_table_counts` (rows per layer and table),
`xodus_promotion_gate`, `xodus_promotion_log`; Simplicity `xodus_landing.xodus_promotion_result` (inserted/updated per live table).
Data Factory's own run and activity history is read through the service principal and shown next to them.

## Access

`deploy/xodus/00_access.sql` (XODUS DB) and `deploy/simplicity/02_access.sql` (Simplicity): Data Factory's managed identity deploys
and writes; the XODUS login reads, plus INSERT on the gate table only.
