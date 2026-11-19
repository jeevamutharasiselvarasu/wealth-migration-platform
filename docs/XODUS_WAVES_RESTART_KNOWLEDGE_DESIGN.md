# XODUS design: lineage, single-entity restart, wave validation gates, English/SQL rules, knowledge ingestion and vectors

Status: design for review (no code changed). Written 2026-11-19.

## 1. Decisions taken

| # | Decision |
|---|---|
| 1 | Staging mapping = a mapping table plus row lineage columns (section 3). |
| 2 | Promote once at the end, in one transaction (the current behaviour). Waves gate the XODUS canonical and mirror layers only. |
| 3 | Hybrid gate: blocking checks run as SQL inside the Data Factory pipeline; full validation, exceptions and sign-off stay in the app. |
| 4 | The app must show each entity's validation output, read from the results the pipeline's SQL wrote (section 6). |
| 5 | New validation rules are authored in plain English or SQL (section 7). |
| 6 | Firm knowledge accepts PDF, Excel, CSV, TXT, MD, links and Confluence; the content is embedded and retrieved by the platform's Python agents (section 8). |
| 7 | Embeddings run locally on the server (decided 2026-11-19): documents never leave the platform's own host. |

## 2. What the code does today (verified in the repo)

- `PL_XODUS_Run` loads every raw file, builds every entity sequentially in load order (`xodus_load_order`), builds all mirror tables in one procedure (`usp_mirror_<slug>`), then ends. Validation, sign-off and promote are separate, whole-batch steps.
- Raw, canonical and mirror steps `TRUNCATE` their tables. A rerun of one entity is not possible.
- `_load_order` in `ui/app.py` already computes dependency "ready sets" (from firm-config references) and then flattens them. Waves are a small step from there.
- Target validation (`target_validation.py`) runs from the app over pymssql, as single COUNT queries built by `build_plan`. It stores results in the app's own database, not in the XODUS database.
- Setup drops and recreates every raw, canonical and mirror table on each deployment, so a changed package wipes entity state.
- `validation_scenarios.py` offers allow-listed check types only (NOT_BLANK, UNIQUE, ALLOWED_VALUES, ...). There is no free-form SQL and no English authoring.
- Knowledge ingestion exists (`/api/knowledge/file-upload`, `/api/knowledge/confluence`, `platform_knowledge.py`) but no embedding or vector code exists anywhere in the repo.

## 3. Foundation and option 1: registry, entity state, lineage

New tables in `xodus_ops`, written by `build()` or by the pipeline:

- `xodus_entity_registry (migration_slug, entity, wave, depends_on, source_files, canon_table, mirror_tables)` replaces the bare `xodus_load_order`.
- `xodus_entity_state (migration_id, batch_id, entity, stage, status, rows, last_run_id, row_hash_digest, updated_utc)`. Status: PENDING, BUILT, VALIDATED, FAILED, STALE.
- `xodus_ref.column_map (migration_slug, layer, entity, target_table, target_column, source_file, source_column, step, expression, mapping_id)` and a view `xodus_ref.v_column_lineage` chaining raw column to canonical field to live column. `step` is MAPPING, DERIVATION or CODE_MAP. The package version is not a column (it would change the hash it is part of); join `xodus_ops.xodus_schema_version` for it.

Phase 1 is built (2026-11-19): all of section 3 except that `xodus_entity_state` is created empty (nothing writes it until phase 2), and the registry has no `source_files` for derived entities.

Row lineage columns:

- Raw: `xodus_row_id bigint IDENTITY` (a stable id; with parallel copy it is not guaranteed to be the file line number).
- Canonical: `xodus_source_file`, `xodus_source_row` (primary file for multi-file entities), `xodus_row_hash` (SHA2_256 over the mapped fields, after derivations and code translation).
- Mirror: existing `xodus_source_key`, plus `xodus_source_row`.

Validation findings can then name source file and row, not only the key. Everything here is additive.

## 4. Option 2: rerun one entity

- `PL_XODUS_Run` gets `Entities` (comma-separated; empty = the whole batch). The loop runs only those entities and loads only the raw files the registry lists for them (including lookup files).
- Built in phase 2 (2026-11-19). Changes from the first draft: there is no `Mode` parameter (empty `Entities` is the whole batch), and the tables keep `TRUNCATE`. A table still holds one batch at a time, and a rerun replaces only the selected entity's own tables; `DELETE WHERE xodus_batch_id` would be wrong because entity resolution and derivations work on the whole canonical table. Consequence: a new batch must be a whole-batch run, and entity reruns only happen inside the latest batch.
- The mirror procedure is split per entity (`usp_mirror_<slug>_<Entity>`); the whole-mirror procedure calls them in order inside one transaction. A rerun reuses the batch id, so Advisor (run 1) and Account (run 2) sit in one batch.
- `usp_xodus_entity_state` is called at the end of every canonical and mirror procedure (BUILT, with the package version and a row-hash digest) and marks dependants STALE. `usp_xodus_check_scope` runs first in a scoped run and refuses: an unknown entity, a batch never built, a batch built by another package version, and a parent that is stale or not built (unless it is in the selection).
- Stage 09 has a "Rerun selected entities" form with an option to include downstream entities.
- Rules: all parents must be VALIDATED in the batch; rerunning a parent marks every descendant STALE (children repoint to the parent's surviving keys); "rerun with downstream" is offered.
- The package version is part of the state. If it changed (setup recreated the tables), every entity in the batch is invalid and the UI says so.
- Sign-off and gate rows hold counts per mirror table, so they can be recorded and checked for an entity set. Promote stays once per batch (decision 2) but only after every entity is VALIDATED and none is STALE.
- The merge never deletes live rows; the UI says so.

## 5. Option 3: wave gates

Built in phase 3 (2026-11-19), with these differences from the first draft:

- `PL_XODUS_Source_To_Canonical` is gone. `PL_XODUS_Run` is Check_Scope, `PL_XODUS_Load_Raw`, then a sequential loop over waves calling `PL_XODUS_Wave` (parallel canonical build, parallel mirror build, `usp_xodus_validate_wave`, gate). Canonical and mirror are built wave by wave, so there is no longer one whole-mirror step in a full run (the whole-mirror procedure still exists).
- The blocking checks are the same plan stage 10 builds (`target_validation.build_plan`), compiled to SQL in `usp_validate_<slug>` at package build time: row count against the raw file, key loaded and unique, required fields, duplicates resolved, code sets (the app now passes the model's code lists in the package inputs), type fit from the target spec, orphans (in the child's wave, only when the parent is in the same or an earlier wave), plus `ENTITY_BUILT` (an unbuilt or stale entity fails). Lengths, live NOT NULL columns and reference lists need the live schema, which only exists after the deployment check, so they stay in stage 10.
- Findings: `xodus_validation_result` (column `observed_count` and `expected_count`, not `failing_count`) and `xodus_validation_row_key` (keys only, at most 50 per finding; the source file and row columns exist but are not filled yet).
- Waivers: `xodus_wave_waiver` exists, the access script grants the platform INSERT on it, and the procedure marks matching failures WAIVED so the gate passes. Nothing in the application writes waivers yet (phase 4).
- Entity state moves to VALIDATED or FAILED after each wave's checks.

- Waves are the dependency levels already implied by `_entity_depends`. Entities in a wave run in parallel (`isSequential: false`, bounded `batchCount`) and are validated together.
- New stable pipeline `PL_XODUS_Wave(Wave)`: build canonical for the wave, build mirror for the wave, `usp_xodus_validate_wave`, then an IfCondition. A FAIL on a BLOCKING check logs and fails the run; later waves do not start.
- Per-entity results mean a failed entity can be rerun alone (section 4) while wave siblings that passed keep their state.
- Orphan checks run only where both sides are in this wave or an earlier one.
- A waiver (an approved exception for a BLOCKING check) is recorded in an INSERT-only table `xodus_ops.xodus_wave_waiver`, the same pattern as `xodus_promotion_gate`, so the app's database permissions stay as narrow as today. The run is then restarted from that wave.
- Hybrid split: BLOCKING (in SQL, in the pipeline): row count against the raw file, key uniqueness, required fields, duplicates resolved, code sets, type and length fit, and approved custom rules marked blocking. Everything else (reference lists from the live target, orphans against live, WARN rules, exceptions, sign-off) stays in the app. Both lists are generated from one check list (`build_plan` already produces plain SQL), so they cannot drift.

## 6. Validation output of each entity, in the app

The pipeline's SQL writes two tables the app reads (the XODUS login already has read access):

- `xodus_ops.xodus_validation_result (result_id, run_id, migration_id, batch_id, wave, entity, check_name, table_name, field_name, status, failing_count, expected, detail, rule_id, source, executed_utc)`; `source` is BUILTIN or RULE.
- `xodus_ops.xodus_validation_row_key (result_id, row_key, source_file, source_row)`: keys only, at most 50 per finding, never other values (the platform's existing principle).

App pages: a wave and entity board (status chips, counts, wave gate state), a drill-down per entity listing each check with its English text and SQL, and failing row keys with a link to the lineage of that row. It reuses the layout of the target validation page. Counts and keys only; no values are shown.

## 7. Validation rules in plain English or SQL

Rule record: id, name, entity, layer (canonical or mirror), English text, SQL, severity (BLOCKING or WARN), status (DRAFT, PROPOSED, APPROVED, RETIRED), author, approver (never the author), version.

Authoring flow:

1. The author writes either English ("every account with status Open must have an advisor") or SQL.
2. English is compiled by the platform itself, with no AI model (decided 2026-11-19: there is no local LLM). The rule editor accepts a controlled English grammar with a fixed set of sentence patterns, for example "every <field> must be filled", "<field> must be one of <list>", "<field> must be unique", "<field> must be between <a> and <b>", "every <entity> with <field> = <value> must have <field2> filled", "every <entity> must have a matching <entity2> on <field>". Each pattern compiles to SQL deterministically, and the author sees both texts side by side. Anything the grammar cannot express is written as SQL directly. Free-form English to SQL by a language model is an optional later add-on if a Platform Admin configures a model endpoint; it would receive field names and types only, never data values, and its output would go through the same static check, dry run and approval.
3. A static check runs on the SQL, then a dry run returns a failing-row count on the current batch, nothing else.
4. A different user approves. Approved rules are retired, not edited; a change is a new version.

SQL contract:

- One read-only `SELECT` returning one integer, the number of failing rows. An optional second query returns at most 50 failing keys.
- References only to the entity's canonical table, the mirror tables and `xodus_ref`, through placeholders (`{canon:Account}`, `{mirror:account}`), with the batch bound as a parameter.
- Rejected by the static check: DML, DDL, `EXEC`, semicolons, comments, cross-database or linked-server names, `OPENROWSET`, `sys.*`, and functions outside an allow-list. Executed by a login with SELECT only and a timeout.
- Decision needed on the parser: `sqlglot` (T-SQL dialect) gives a real syntax tree and is the safer choice, but it is a new dependency. A regex allow-list is not sufficient on its own.

Where the rules live: as rows in `xodus_ref.validation_rule`, executed by the fixed procedure `usp_xodus_validate_wave` through `sp_executesql`. The rules ride the package and pull request, so each is reviewed twice (approval in the app, merge in the repo) and the package version changes. This relaxes the module rule that "nothing free-form is ever emitted" in `xodus_pipelines.py`: approved SQL is now data. A faster rules-only refresh can be added later.

## 8. Knowledge ingestion and vectorisation

### What exists

Uploads: .md, .txt, .csv, .tsv, .json, .xlsx, .xlsm, .xls, .docx, .pptx, .pdf. Confluence Cloud, one page per link, with one server-wide account (`CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN` in the environment). Platform knowledge has a smaller allowed list.

### Gaps

- Excel and CSV keep only the column names; the cell content is not read.
- A generic web link is not supported (only Confluence).
- Confluence: one page at a time, Cloud only, one shared credential, no setup screen, and an unconfigured server just shows an error.
- Text is stored truncated and unchunked; nothing is searchable beyond keyword lines.

### Ingestion changes

- Excel and CSV: read content into text chunks (a sheet's header plus groups of rows; for data dictionaries, one entry per row), with sheet and row ranges as citations.
- Links: https only, host allow-list or admin-approved domains, no redirects to private addresses, size and time limits, HTML reduced to visible text.
- Confluence: a connection page. If nothing is configured, a user sees "Confluence is not connected, ask a Platform Admin"; an admin sees a setup form (base URL, auth type: Cloud API token or Data Center personal access token). The secret is held in the server's secret store or environment and referenced by name; it is never stored in the database, shown, or logged. Import supports one page, a page and its children, or a space, with a page cap and re-sync by page version.
- Everything is stored with: asset id, category, source (file or URL), version, status, entity tags.

### Vectors

- Chunk by heading, sheet or row group (about 400 to 800 tokens, with overlap), embed, and store the vector with citation metadata.
- Embedding model: decided, local on the server (Anthropic offers no embedding model, and client documents are in scope). Implementation: a small open embedding model behind an `Embedder` interface. Prefer an ONNX runtime package (for example `fastembed` with a small BGE model) over `sentence-transformers`, which pulls in PyTorch and makes the container image several GB larger; either fits the interface. The model files are baked into the image at build time so the running container needs no internet access, and the model name and version are stored with every vector so a model change triggers a full re-embed. Masking client data before upload is still advised.
- Store: behind one interface. Start with a DB-agnostic table of vectors searched with numpy (enough for thousands of chunks), because production can run on SQL Server through `mssql_compat` where pgvector does not exist. Swap to pgvector (the Postgres in `docker-compose.yml` would need a pgvector image) or the SQL vector type later, after checking availability.
- Retrieval consumers: the schema mapper, rule suggestions, entity classification and the English-to-SQL step fetch the top-k chunks as cited evidence. Retrieved text is data, never instructions; every output still passes human approval.
- "Trained" (decided 2026-11-19: fine-tuning is wanted). Plan in stages, because fine-tuning needs data that does not exist yet:
  1. Retrieval first (above), plus a feedback store that records every approved or rejected mapping, rule and classification decision with the evidence chunks shown. This is the training dataset.
  2. Fine-tune local models once enough decisions exist: the embedding model (contrastive pairs: field or question to the chunk a reviewer accepted) and small classifiers or rankers for mapping suggestion and entity classification. Both run on CPU or a modest GPU and stay on the server.
  3. Fine-tuning a generative LLM is out of scope. Decided 2026-11-19 from the hardware actually available (one machine, about 31 GB RAM, integrated graphics only, no separate AI models): CPU-only work is viable for a small embedding model (tens of millions of parameters) and scikit-learn style classifiers; LLM fine-tuning is not. Revisit only if a GPU or a hosted model endpoint becomes available.
  Controls: training data comes only from approved decisions and approved knowledge; each fine-tuned model is versioned with its data snapshot and an evaluation on held-out decisions, and is promoted only if it beats the current one; models and training sets are per firm, since they contain that firm's knowledge, and are deleted with the migration; the existing approval gates still apply to every output.
- Retire or replace an asset and its vectors are dropped and rebuilt.

## 9. Phases

1. Registry, entity state, `wave` column, lineage columns, `column_map`. No behaviour change.
2. Entity-scoped runs and non-truncating rebuilds.
3. `PL_XODUS_Wave`, `usp_xodus_validate_wave`, result tables, waiver table.
4. App: entity and wave validation board, per-entity rerun controls.
5. Validation rules: record, static check, English drafting, approval, execution.
6. Knowledge: Excel/CSV content, links, Confluence connection and import, chunk store.
7. Embeddings and retrieval wired into the agents.

Phases 1 to 4 are one stream. Phase 5 depends on 3. Phases 6 and 7 are independent of 1 to 5 and can run in parallel.

## 10. Open items and risks

- Local embedding model: confirm the runtime (ONNX `fastembed` recommended over PyTorch for image size), the model, and that the container CPU and memory suit it. A larger corpus may later need a GPU or a separate embedding service.
- Fine-tuning scope is settled: local embedding model and small classifiers only, on CPU. The open point is how many reviewed decisions exist; training starts only when there are enough to beat the untuned model on held-out decisions.
- `sqlglot` accepted (2026-11-19) as the SQL parser for the rule static check.
- Rules ride the package and pull request (default); a rules-only refresh is a later option.
- Setup recreates the tables on every package change; entity state must be treated as invalid when the version changes.
- Whether Simplicity has foreign keys is not known; assume it might. The end-of-batch MERGE therefore runs in dependency order (wave order, parents first), not alphabetically as today. Verify against the live schema when it is reachable.
- Confluence: the credential and the allowed spaces are set by a Platform Admin only (decided 2026-11-19). Other roles can import only from spaces an admin has allowed, and see "ask a Platform Admin" when none is configured.
