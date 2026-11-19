"""Builds the XODUS data-plane package: stable, parameterised Data Factory pipelines plus the SQL they run.

Layers (all in the XODUS database unless noted; every table is per migration, named <slug>_<name>):
  xodus_raw     the source files exactly as received (every column text, plus trace columns)       <- PL_XODUS_Load_Raw_File
  xodus_canon   one table per entity in the canonical model, filled by usp_canon_<slug>_<Entity>     <- PL_XODUS_Canonical_Entity
  xodus_mirror  a mirror of each live target table, filled by usp_mirror_<slug>                      <- PL_XODUS_Canonical_To_Mirror
  xodus_ops     version, load order, run log, pipeline runs, table counts, promotion gate and log
  xodus_ref     code maps (data the procedures read): source value -> canonical code per migration, canonical code -> target value per target
  Simplicity    xodus_landing.<slug>_<table> and usp_promote_<slug>, which merges into the live tables  <- PL_XODUS_Promote

What changes with the source lives only in the SQL (the generated procedures and the load-order rows), which PL_XODUS_Setup_<slug>
deploys. The pipeline definitions never change: they take the migration and batch as parameters. Nothing here is executed; the
package is published through a pull request and run by Data Factory. Only approved mappings, approved rules and registered
transformation families are turned into SQL; nothing free-form is ever emitted.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import entity_resolution, sql_package
from control_plane.services.sql_package import _lit, _q, translate

RAW, CANON, MIRROR, OPS, LANDING, REF = "xodus_raw", "xodus_canon", "xodus_mirror", "xodus_ops", "xodus_landing", "xodus_ref"
LS_BLOB, LS_XODUS, LS_LIVE = "LS_BLOB", "LS_SQL_Target", "LS_SQL_Simplicity"
STABLE_PIPELINES = ("PL_XODUS_Run", "PL_XODUS_Load_Raw", "PL_XODUS_Wave", "PL_XODUS_Load_Raw_File", "PL_XODUS_Canonical_Entity",
                    "PL_XODUS_Canonical_To_Mirror", "PL_XODUS_Promote")
HEADER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]{0,127}$")
TRACE = ("[xodus_migration_id] nvarchar(64) NULL", "[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL",
         "[xodus_source_file] nvarchar(1024) NULL", "[xodus_loaded_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()")
# Filled by SQL Server, not by the copy: a stable id for each raw row (with a parallel copy it is not the file's line number).
ROW_ID = "[xodus_row_id] bigint IDENTITY(1,1) NOT NULL"
# Where each canonical row came from, and a hash of its values (set at the end of the entity's procedure).
CANON_LINEAGE = ("[xodus_source_file] nvarchar(1024) NULL", "[xodus_source_row] bigint NULL", "[xodus_row_hash] char(64) NULL")


def slug(migration_id: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(migration_id).lower())[:16]
    if not s or not s[0].isalpha():
        s = ("m" + s)[:16]
    return s


def _header(name: str) -> str:
    if not HEADER.match(str(name or "")):
        raise ValueError(f"Source column '{name}' cannot be used as a raw table column (letters, digits, space, '.', '-' and '_' only).")
    return "[" + str(name) + "]"


def _file_stem(file_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", re.sub(r"\.[A-Za-z0-9]+$", "", str(file_name)))[:60]


def raw_table(s: str, file_name: str) -> str:
    return f"{s}_{_file_stem(file_name)}"


def _family(expression: str) -> str:
    """The registered transformation family of an approved source mapping (the same families the mapping agent applies)."""
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent
    return SchemaMapperAgent.transformation_family(expression)


def source_expression(expression: str, column: str, rules: Dict[str, Dict[str, Any]], alias: str, lookup_ref) -> str:
    """T-SQL for one approved source-to-canonical mapping, applying exactly what the mapping agent applies in memory."""
    from agents.discovery.schema_mapper_agent import SchemaMapperAgent as A
    col = f"{alias}.{_header(column)}"
    expr = str(expression or "DIRECT_COPY").strip().upper()
    if expr.startswith("RULE:"):
        rule = rules.get(expr)
        if not rule:
            raise ValueError(f"{expr} is not an approved rule.")
        return translate(expr, column, rule, alias, lookup_ref)
    fam = _family(expr)
    if fam in ("DIRECT_COPY", "ALIAS"):
        return col
    if fam == "TRIM":
        return f"LTRIM(RTRIM({col}))"
    if fam == "UPPERCASE":
        return f"UPPER({col})"
    if fam == "LOWERCASE":
        return f"LOWER({col})"
    if fam == "NORMALIZE_WHITESPACE":
        return f"LTRIM(RTRIM(REPLACE(REPLACE(REPLACE(REPLACE({col}, NCHAR(9), N' '), N'  ', N' '), N'  ', N' '), N'  ', N' ')))"
    if fam == "DATE_FORMAT":
        return f"COALESCE(CONVERT(nvarchar(10), TRY_CONVERT(date, LTRIM(RTRIM({col})), 101), 23), {col})"
    raise ValueError(f"'{expression}' is not a registered transformation and cannot be turned into SQL.")


def _proc(name: str, params: str, body: List[str]) -> str:
    """CREATE OR ALTER PROCEDURE that runs its body in one transaction (all or nothing)."""
    inner = "\n".join("        " + line for chunk in body for line in chunk.rstrip("\n").split("\n"))
    return (f"CREATE OR ALTER PROCEDURE {name}\n    {params}\nAS\nBEGIN\n    SET NOCOUNT ON;\n    SET XACT_ABORT ON;\n    BEGIN TRY\n        BEGIN TRANSACTION;\n"
            f"{inner}\n        COMMIT TRANSACTION;\n    END TRY\n    BEGIN CATCH\n        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;\n        THROW;\n    END CATCH\nEND")


def _schema(name: str) -> str:
    return f"IF SCHEMA_ID(N'{name}') IS NULL EXEC(N'CREATE SCHEMA [{name}]');"


def _drop_create(schema: str, table: str, columns: List[str]) -> str:
    return (f"IF OBJECT_ID(N'{schema}.{table}', N'U') IS NOT NULL DROP TABLE [{schema}].[{table}];\n"
            f"CREATE TABLE [{schema}].[{table}] (\n    " + ",\n    ".join(columns) + "\n);")


def shared_sql() -> List[str]:
    """Objects every migration uses (schemas, ops tables, logging, dispatch, gate check). Safe to run again."""
    b = [_schema(s) for s in (RAW, CANON, MIRROR, OPS, REF)]
    b.append(f"IF OBJECT_ID(N'{REF}.code_map', N'U') IS NULL CREATE TABLE [{REF}].[code_map] ([migration_slug] nvarchar(32) NOT NULL, "
             "[code_set] nvarchar(64) NOT NULL, [source_value] nvarchar(400) NOT NULL, [canonical_code] nvarchar(64) NOT NULL, "
             "CONSTRAINT [PK_code_map] PRIMARY KEY ([migration_slug], [code_set], [source_value]));")
    b.append(f"IF OBJECT_ID(N'{REF}.target_code_map', N'U') IS NULL CREATE TABLE [{REF}].[target_code_map] ([target_system] nvarchar(128) NOT NULL, "
             "[code_set] nvarchar(64) NOT NULL, [canonical_code] nvarchar(64) NOT NULL, [target_value] nvarchar(400) NOT NULL, "
             "CONSTRAINT [PK_target_code_map] PRIMARY KEY ([target_system], [code_set], [canonical_code]));")
    # Column lineage: one row per staging column and the mapping that fills it, written at setup from the approved mappings, rules and code sets.
    b.append(f"IF OBJECT_ID(N'{REF}.column_map', N'U') IS NULL CREATE TABLE [{REF}].[column_map] ([migration_slug] nvarchar(32) NOT NULL, "
             "[layer] nvarchar(16) NOT NULL, [entity] sysname NOT NULL, [target_table] sysname NOT NULL, [target_column] nvarchar(256) NOT NULL, "
             "[source_file] nvarchar(256) NULL, [source_column] nvarchar(256) NULL, [step] nvarchar(16) NOT NULL, [expression] nvarchar(400) NULL, "
             "[mapping_id] nvarchar(64) NULL);")
    b.append(f"""CREATE OR ALTER VIEW [{REF}].[v_column_lineage] AS
SELECT m.[migration_slug], m.[entity], m.[target_table] AS [mirror_table], m.[target_column] AS [live_column], m.[mapping_id], m.[expression] AS [target_expression],
       c.[target_table] AS [canonical_table], c.[target_column] AS [canonical_field], c.[step] AS [canonical_step], c.[source_file], c.[source_column],
       c.[expression] AS [canonical_expression]
FROM [{REF}].[column_map] AS m
LEFT JOIN [{REF}].[column_map] AS c ON c.[migration_slug] = m.[migration_slug] AND c.[layer] = N'canonical' AND c.[entity] = m.[entity] AND c.[target_column] = m.[source_column]
WHERE m.[layer] = N'mirror'""")
    tables = {
        "xodus_entity_registry": "[migration_slug] nvarchar(32) NOT NULL, [entity] sysname NOT NULL, [wave] int NOT NULL, [depends_on] nvarchar(1000) NULL, "
                                 "[source_files] nvarchar(2000) NULL, [canon_table] sysname NOT NULL, [mirror_tables] nvarchar(2000) NULL, "
                                 "CONSTRAINT [PK_xodus_entity_registry] PRIMARY KEY ([migration_slug], [entity])",
        "xodus_entity_state": "[state_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [migration_id] nvarchar(64) NOT NULL, [batch_id] nvarchar(64) NOT NULL, "
                              "[entity] sysname NOT NULL, [stage] nvarchar(16) NOT NULL, [status] nvarchar(16) NOT NULL, [rows_written] bigint NULL, "
                              "[last_run_id] nvarchar(64) NULL, [row_hash_digest] char(64) NULL, [updated_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME(), "
                              "CONSTRAINT [UQ_xodus_entity_state] UNIQUE ([migration_id], [batch_id], [entity])",
        # Blocking checks run inside the pipeline after each wave. One row per check and entity; the application reads these to show each entity's output.
        "xodus_validation_result": "[result_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [run_id] nvarchar(64) NOT NULL, [migration_id] nvarchar(64) NOT NULL, "
                                   "[batch_id] nvarchar(64) NOT NULL, [wave] int NOT NULL, [entity] sysname NOT NULL, [check_name] nvarchar(64) NOT NULL, "
                                   "[table_name] nvarchar(256) NOT NULL, [field_name] nvarchar(512) NOT NULL, [status] nvarchar(16) NOT NULL, "
                                   "[observed_count] bigint NULL, [expected_count] bigint NULL, [detail] nvarchar(4000) NULL, [rule_id] nvarchar(64) NULL, "
                                   "[source] nvarchar(16) NOT NULL DEFAULT N'BUILTIN', [executed_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()",
        # Keys of the failing rows only (at most 50 per finding), never other values; the lineage columns are filled when the key can be traced to a source row.
        "xodus_validation_row_key": "[key_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [result_id] bigint NOT NULL, [row_key] nvarchar(400) NOT NULL, "
                                    "[source_file] nvarchar(1024) NULL, [source_row] bigint NULL",
        # An approved exception for a blocking check: the platform may INSERT here (like the promotion gate); the validation procedure marks the matching finding WAIVED.
        "xodus_wave_waiver": "[waiver_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [migration_id] nvarchar(64) NOT NULL, [batch_id] nvarchar(64) NOT NULL, "
                             "[entity] sysname NOT NULL, [check_name] nvarchar(64) NOT NULL, [table_name] nvarchar(256) NOT NULL, [field_name] nvarchar(512) NOT NULL, "
                             "[reason] nvarchar(1000) NOT NULL, [approved_by] nvarchar(256) NOT NULL, [written_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()",
        "xodus_schema_version": "[migration_slug] nvarchar(32) NOT NULL, [component] nvarchar(32) NOT NULL, [version] nvarchar(64) NOT NULL, "
                                "[deployed_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME(), CONSTRAINT [PK_xodus_schema_version] PRIMARY KEY ([migration_slug], [component])",
        "xodus_raw_files": "[migration_slug] nvarchar(32) NOT NULL, [seq] int NOT NULL, [container] nvarchar(63) NOT NULL, [folder] nvarchar(512) NOT NULL, "
                           "[file_name] nvarchar(256) NOT NULL, [raw_table] sysname NOT NULL",
        "xodus_load_order": "[migration_slug] nvarchar(32) NOT NULL, [seq] int NOT NULL, [entity] sysname NOT NULL",
        "xodus_live_tables": "[migration_slug] nvarchar(32) NOT NULL, [mirror_table] sysname NOT NULL, [live_table] sysname NOT NULL",
        "xodus_run_log": "[log_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [migration_id] nvarchar(64) NOT NULL, [pipeline_run_id] nvarchar(64) NOT NULL, "
                         "[trigger_file] nvarchar(256) NULL, [pipeline_name] nvarchar(128) NULL, [step_name] nvarchar(128) NOT NULL, [status] nvarchar(16) NOT NULL, "
                         "[rows_read] bigint NULL, [rows_written] bigint NULL, [message] nvarchar(4000) NULL, [logged_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()",
        "xodus_pipeline_run": "[run_id] nvarchar(64) NOT NULL PRIMARY KEY, [migration_id] nvarchar(64) NOT NULL, [batch_id] nvarchar(64) NULL, "
                              "[pipeline_name] nvarchar(128) NOT NULL, [status] nvarchar(16) NOT NULL, [started_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME(), "
                              "[finished_utc] datetime2 NULL, [message] nvarchar(4000) NULL",
        "xodus_table_counts": "[count_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [run_id] nvarchar(64) NOT NULL, [migration_id] nvarchar(64) NOT NULL, "
                              "[batch_id] nvarchar(64) NULL, [layer] nvarchar(16) NOT NULL, [table_name] sysname NOT NULL, [row_count] bigint NOT NULL, "
                              "[logged_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()",
        "xodus_promotion_gate": "[gate_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [migration_id] nvarchar(64) NOT NULL, [batch_id] nvarchar(64) NOT NULL, "
                                "[validation_run_id] nvarchar(64) NOT NULL, [signed_off_by] nvarchar(256) NOT NULL, [signed_off_utc] nvarchar(64) NOT NULL, "
                                "[counts_json] nvarchar(max) NOT NULL, [written_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()",
        "xodus_promotion_log": "[promotion_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [migration_id] nvarchar(64) NOT NULL, [batch_id] nvarchar(64) NOT NULL, "
                               "[run_id] nvarchar(64) NOT NULL, [status] nvarchar(16) NOT NULL, [detail] nvarchar(max) NULL, [logged_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()",
    }
    for name, cols in tables.items():
        b.append(f"IF OBJECT_ID(N'{OPS}.{name}', N'U') IS NULL CREATE TABLE [{OPS}].[{name}] ({cols});")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_log]
    @MigrationId nvarchar(64), @RunId nvarchar(64), @BatchId nvarchar(64), @Pipeline nvarchar(128), @Step nvarchar(128), @Status nvarchar(16),
    @RowsRead bigint = NULL, @RowsWritten bigint = NULL, @Message nvarchar(4000) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO [{OPS}].[xodus_run_log] (migration_id, pipeline_run_id, trigger_file, pipeline_name, step_name, status, rows_read, rows_written, message)
    VALUES (@MigrationId, @RunId, @BatchId, @Pipeline, @Step, @Status, @RowsRead, @RowsWritten, LEFT(@Message, 4000));
    IF @Step = N'Pipeline' AND @Status = N'STARTED'
        INSERT INTO [{OPS}].[xodus_pipeline_run] (run_id, migration_id, batch_id, pipeline_name, status) VALUES (@RunId, @MigrationId, @BatchId, @Pipeline, N'RUNNING');
    ELSE IF @Step = N'Pipeline'
        UPDATE [{OPS}].[xodus_pipeline_run] SET status = @Status, finished_utc = SYSUTCDATETIME(), message = LEFT(@Message, 4000) WHERE run_id = @RunId;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_count]
    @MigrationId nvarchar(64), @RunId nvarchar(64), @BatchId nvarchar(64), @Layer nvarchar(16), @Schema sysname, @Table sysname
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @n bigint, @sql nvarchar(max) = N'SELECT @n = COUNT_BIG(*) FROM ' + QUOTENAME(@Schema) + N'.' + QUOTENAME(@Table);
    EXEC sp_executesql @sql, N'@n bigint OUTPUT', @n = @n OUTPUT;
    INSERT INTO [{OPS}].[xodus_table_counts] (run_id, migration_id, batch_id, layer, table_name, row_count) VALUES (@RunId, @MigrationId, @BatchId, @Layer, @Table, @n);
END""")
    sel = "SELECT LTRIM(RTRIM(value)) FROM STRING_SPLIT(@Entities, N',') WHERE LTRIM(RTRIM(value)) <> N''"  # the entities of a scoped run
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_raw_files] @MigrationSlug nvarchar(32), @Entities nvarchar(2000) = N''
AS
BEGIN
    SET NOCOUNT ON;
    -- Every raw file of the migration, or (a scoped run) only the files the selected entities read, including the lookup files their rules use.
    SELECT f.container, f.folder, f.file_name, f.raw_table FROM [{OPS}].[xodus_raw_files] AS f WHERE f.migration_slug = @MigrationSlug
        AND (@Entities = N'' OR EXISTS (SELECT 1 FROM [{OPS}].[xodus_entity_registry] AS r
             WHERE r.migration_slug = @MigrationSlug AND r.entity IN ({sel})
               AND f.raw_table IN (SELECT LTRIM(RTRIM(value)) FROM STRING_SPLIT(r.source_files, N','))))
    ORDER BY f.seq;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_load_order] @MigrationSlug nvarchar(32), @Entities nvarchar(2000) = N''
AS
BEGIN
    SET NOCOUNT ON;
    SELECT entity FROM [{OPS}].[xodus_load_order] WHERE migration_slug = @MigrationSlug AND (@Entities = N'' OR entity IN ({sel})) ORDER BY seq;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_mirror_scope] @MigrationSlug nvarchar(32), @Entities nvarchar(2000) = N''
AS
BEGIN
    SET NOCOUNT ON;
    -- One row with an empty entity (the whole mirror in one transaction), or one row per selected entity that has mirror tables, in load order.
    IF @Entities = N'' SELECT CAST(N'' AS sysname) AS entity;
    ELSE SELECT o.entity FROM [{OPS}].[xodus_load_order] AS o JOIN [{OPS}].[xodus_entity_registry] AS r ON r.migration_slug = o.migration_slug AND r.entity = o.entity
         WHERE o.migration_slug = @MigrationSlug AND r.mirror_tables <> N'' AND o.entity IN ({sel}) ORDER BY o.seq;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_waves] @MigrationSlug nvarchar(32), @Entities nvarchar(2000) = N''
AS
BEGIN
    SET NOCOUNT ON;
    -- The waves to run, in order: all of them, or the waves the selected entities are in.
    SELECT DISTINCT r.wave FROM [{OPS}].[xodus_entity_registry] AS r WHERE r.migration_slug = @MigrationSlug AND (@Entities = N'' OR r.entity IN ({sel})) ORDER BY r.wave;
END""")
    for name, extra in (("usp_xodus_wave_entities", ""), ("usp_xodus_wave_mirror_entities", " AND r.mirror_tables <> N''")):
        b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[{name}] @MigrationSlug nvarchar(32), @Wave int, @Entities nvarchar(2000) = N''
AS
BEGIN
    SET NOCOUNT ON;
    -- The entities of one wave (siblings: none depends on another) that the run covers, in load order.
    SELECT o.entity FROM [{OPS}].[xodus_load_order] AS o JOIN [{OPS}].[xodus_entity_registry] AS r ON r.migration_slug = o.migration_slug AND r.entity = o.entity
        WHERE o.migration_slug = @MigrationSlug AND r.wave = @Wave{extra} AND (@Entities = N'' OR o.entity IN ({sel})) ORDER BY o.seq;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_validate_wave]
    @MigrationSlug nvarchar(32), @MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64), @Wave int, @Entities nvarchar(2000) = N''
AS
BEGIN
    SET NOCOUNT ON;
    IF @MigrationSlug LIKE N'%[^a-z0-9]%' THROW 50001, N'Unsafe migration name.', 1;
    DECLARE @proc nvarchar(300) = QUOTENAME(N'{OPS}') + N'.' + QUOTENAME(N'usp_validate_' + @MigrationSlug);
    IF OBJECT_ID(@proc, N'P') IS NULL THROW 50002, N'The validation procedure for this migration is not deployed. Run the setup pipeline first.', 1;
    DECLARE @sql nvarchar(400) = N'EXEC ' + @proc + N' @MigrationId = @m, @BatchId = @b, @RunId = @r, @Wave = @w, @Entities = @e';
    EXEC sp_executesql @sql, N'@m nvarchar(64), @b nvarchar(64), @r nvarchar(64), @w int, @e nvarchar(2000)', @m = @MigrationId, @b = @BatchId, @r = @RunId, @w = @Wave, @e = @Entities;
    -- The gate: one row, ok = 1 when no entity of the wave has a failed (and not waived) blocking check.
    DECLARE @bad nvarchar(max) = (SELECT STRING_AGG(CONCAT(r.entity, N': ', r.check_name, CASE WHEN r.field_name <> N'' THEN CONCAT(N' (', r.field_name, N')') ELSE N'' END), N'; ')
        FROM [{OPS}].[xodus_validation_result] AS r WHERE r.migration_id = @MigrationId AND r.batch_id = @BatchId AND r.wave = @Wave AND r.status = N'FAIL'
          AND (@Entities = N'' OR r.entity IN ({sel})));
    SELECT CASE WHEN @bad IS NULL THEN 1 ELSE 0 END AS ok,
           CASE WHEN @bad IS NULL THEN CONCAT(N'Wave ', @Wave, N' passed.') ELSE LEFT(CONCAT(N'Wave ', @Wave, N' failed: ', @bad), 3900) END AS detail;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_live_tables] @MigrationSlug nvarchar(32)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT mirror_table, live_table FROM [{OPS}].[xodus_live_tables] WHERE migration_slug = @MigrationSlug ORDER BY mirror_table;
END""")
    b.append(f"IF COL_LENGTH(N'{OPS}.xodus_entity_state', N'package_version') IS NULL ALTER TABLE [{OPS}].[xodus_entity_state] ADD [package_version] nvarchar(64) NULL;")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_entity_state]
    @MigrationSlug nvarchar(32), @MigrationId nvarchar(64), @BatchId nvarchar(64), @Entity sysname, @Stage nvarchar(16), @RunId nvarchar(64),
    @Rows bigint = NULL, @Digest char(64) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    -- Records that the entity was built in this batch (stage canonical or mirror) with the deployed package version. A rebuilt canonical
    -- entity makes everything that depends on it stale: those entities must be rebuilt before the batch can move on.
    DECLARE @version nvarchar(64) = (SELECT TOP (1) [version] FROM [{OPS}].[xodus_schema_version] WHERE migration_slug = @MigrationSlug AND component = N'xodus');
    MERGE [{OPS}].[xodus_entity_state] AS t
    USING (SELECT @MigrationId AS m, @BatchId AS b, @Entity AS e) AS s ON t.migration_id = s.m AND t.batch_id = s.b AND t.entity = s.e
    WHEN MATCHED THEN UPDATE SET stage = @Stage, status = N'BUILT', rows_written = COALESCE(@Rows, t.rows_written), last_run_id = @RunId,
        row_hash_digest = COALESCE(@Digest, t.row_hash_digest), package_version = @version, updated_utc = SYSUTCDATETIME()
    WHEN NOT MATCHED THEN INSERT (migration_id, batch_id, entity, stage, status, rows_written, last_run_id, row_hash_digest, package_version)
        VALUES (@MigrationId, @BatchId, @Entity, @Stage, N'BUILT', @Rows, @RunId, @Digest, @version);
    IF @Stage = N'canonical'
    BEGIN
        WITH d AS (
            SELECT r.entity FROM [{OPS}].[xodus_entity_registry] AS r CROSS APPLY STRING_SPLIT(r.depends_on, N',') AS p
                WHERE r.migration_slug = @MigrationSlug AND LTRIM(RTRIM(p.value)) = @Entity
            UNION ALL
            SELECT r.entity FROM [{OPS}].[xodus_entity_registry] AS r CROSS APPLY STRING_SPLIT(r.depends_on, N',') AS p JOIN d ON LTRIM(RTRIM(p.value)) = d.entity
                WHERE r.migration_slug = @MigrationSlug)
        UPDATE [{OPS}].[xodus_entity_state] SET status = N'STALE', updated_utc = SYSUTCDATETIME()
            WHERE migration_id = @MigrationId AND batch_id = @BatchId AND entity IN (SELECT entity FROM d);
    END;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_check_scope]
    @MigrationSlug nvarchar(32), @MigrationId nvarchar(64), @BatchId nvarchar(64), @Entities nvarchar(2000) = N''
AS
BEGIN
    SET NOCOUNT ON;
    IF @Entities = N'' RETURN;  -- a whole-batch run has nothing to check
    DECLARE @sel TABLE (entity sysname NOT NULL PRIMARY KEY);
    INSERT INTO @sel SELECT DISTINCT LTRIM(RTRIM(value)) FROM STRING_SPLIT(@Entities, N',') WHERE LTRIM(RTRIM(value)) <> N'';
    DECLARE @msg nvarchar(max) = N'';
    SELECT @msg = @msg + e.entity + N' is not an entity of this migration. ' FROM @sel AS e
        WHERE NOT EXISTS (SELECT 1 FROM [{OPS}].[xodus_entity_registry] AS r WHERE r.migration_slug = @MigrationSlug AND r.entity = e.entity);
    IF @msg <> N'' BEGIN SET @msg = LEFT(@msg, 2000); THROW 50004, @msg, 1; END;
    IF NOT EXISTS (SELECT 1 FROM [{OPS}].[xodus_entity_state] WHERE migration_id = @MigrationId AND batch_id = @BatchId)
        THROW 50005, N'This batch has not been built yet. Run the whole batch first; single entities can only be rerun inside a built batch.', 1;
    DECLARE @version nvarchar(64) = (SELECT TOP (1) [version] FROM [{OPS}].[xodus_schema_version] WHERE migration_slug = @MigrationSlug AND component = N'xodus');
    SELECT @msg = @msg + s.entity + N' was built by another package version. ' FROM [{OPS}].[xodus_entity_state] AS s
        WHERE s.migration_id = @MigrationId AND s.batch_id = @BatchId AND ISNULL(s.package_version, N'') <> ISNULL(@version, N'');
    IF @msg <> N'' BEGIN SET @msg = LEFT(@msg + N'The package changed since this batch was built; run the whole batch again.', 2000); THROW 50006, @msg, 1; END;
    SELECT @msg = @msg + e.entity + N' needs ' + p.parent + N' built in this batch first (it is stale, failed validation or not built). ' FROM @sel AS e
        JOIN [{OPS}].[xodus_entity_registry] AS r ON r.migration_slug = @MigrationSlug AND r.entity = e.entity
        CROSS APPLY (SELECT LTRIM(RTRIM(value)) AS parent FROM STRING_SPLIT(r.depends_on, N',') WHERE LTRIM(RTRIM(value)) <> N'') AS p
        WHERE p.parent NOT IN (SELECT entity FROM @sel)
          AND NOT EXISTS (SELECT 1 FROM [{OPS}].[xodus_entity_state] AS s WHERE s.migration_id = @MigrationId AND s.batch_id = @BatchId
                          AND s.entity = p.parent AND s.status IN (N'BUILT', N'VALIDATED'));
    IF @msg <> N'' BEGIN SET @msg = LEFT(@msg, 2000); THROW 50007, @msg, 1; END;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_stamp_raw]
    @MigrationSlug nvarchar(32), @Table sysname, @MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64), @SourceFile nvarchar(1024)
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT EXISTS (SELECT 1 FROM [{OPS}].[xodus_raw_files] WHERE migration_slug = @MigrationSlug AND raw_table = @Table)
        THROW 50003, N'That raw table is not registered for this migration.', 1;
    DECLARE @sql nvarchar(max) = N'UPDATE ' + QUOTENAME(N'{RAW}') + N'.' + QUOTENAME(@Table)
        + N' SET xodus_migration_id = @m, xodus_batch_id = @b, xodus_run_id = @r, xodus_source_file = @f WHERE xodus_batch_id IS NULL';
    EXEC sp_executesql @sql, N'@m nvarchar(64), @b nvarchar(64), @r nvarchar(64), @f nvarchar(1024)', @m = @MigrationId, @b = @BatchId, @r = @RunId, @f = @SourceFile;
    EXEC [{OPS}].[usp_xodus_count] @MigrationId, @RunId, @BatchId, N'raw', N'{RAW}', @Table;
END""")
    for schema, prefix in ((CANON, "usp_canon_"), (MIRROR, "usp_mirror_")):
        is_canon = schema == CANON
        entity_param = ", @Entity sysname" if is_canon else ", @Entity sysname = N''"
        entity_check = " OR @Entity LIKE N'%[^A-Za-z0-9_]%'"
        entity_name = " + N'_' + @Entity" if is_canon else " + CASE WHEN @Entity = N'' THEN N'' ELSE N'_' + @Entity END"
        b.append(f"""CREATE OR ALTER PROCEDURE [{schema}].[usp_xodus_{'canon' if is_canon else 'mirror'}]
    @MigrationSlug nvarchar(32), @MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64){entity_param}
AS
BEGIN
    SET NOCOUNT ON;
    IF @MigrationSlug LIKE N'%[^a-z0-9]%'{entity_check} THROW 50001, N'Unsafe migration or entity name.', 1;
    DECLARE @proc nvarchar(300) = QUOTENAME(N'{schema}') + N'.' + QUOTENAME(N'{prefix}' + @MigrationSlug{entity_name});
    IF OBJECT_ID(@proc, N'P') IS NULL THROW 50002, N'The procedure for this migration is not deployed. Run the setup pipeline first.', 1;
    DECLARE @sql nvarchar(400) = N'EXEC ' + @proc + N' @MigrationId = @m, @BatchId = @b, @RunId = @r';
    EXEC sp_executesql @sql, N'@m nvarchar(64), @b nvarchar(64), @r nvarchar(64)', @m = @MigrationId, @b = @BatchId, @r = @RunId;
END""")
    b.append(f"""CREATE OR ALTER PROCEDURE [{OPS}].[usp_xodus_gate_check]
    @MigrationSlug nvarchar(32), @MigrationId nvarchar(64), @BatchId nvarchar(64)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @counts nvarchar(max) = (SELECT TOP (1) counts_json FROM [{OPS}].[xodus_promotion_gate] WHERE migration_id = @MigrationId AND batch_id = @BatchId ORDER BY gate_id DESC);
    IF @counts IS NULL BEGIN SELECT CAST(0 AS int) AS ok, N'No sign-off is recorded for this batch.' AS detail; RETURN; END;
    DECLARE @t sysname, @expected bigint, @actual bigint, @sql nvarchar(max), @bad nvarchar(max) = N'';
    DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT [key], CAST([value] AS bigint) FROM OPENJSON(@counts);
    OPEN c; FETCH NEXT FROM c INTO @t, @expected;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        IF @t NOT LIKE @MigrationSlug + N'[_]%' SET @bad = @bad + @t + N' is not a table of this migration. ';
        ELSE IF OBJECT_ID(QUOTENAME(N'{MIRROR}') + N'.' + QUOTENAME(@t), N'U') IS NULL SET @bad = @bad + @t + N' is missing. ';
        ELSE
        BEGIN
            SET @sql = N'SELECT @n = COUNT_BIG(*) FROM ' + QUOTENAME(N'{MIRROR}') + N'.' + QUOTENAME(@t) + N' WHERE xodus_batch_id = @b';
            EXEC sp_executesql @sql, N'@n bigint OUTPUT, @b nvarchar(64)', @n = @actual OUTPUT, @b = @BatchId;
            IF @actual <> @expected SET @bad = @bad + @t + N' has ' + CAST(@actual AS nvarchar(20)) + N' rows; ' + CAST(@expected AS nvarchar(20)) + N' were signed off. ';
        END;
        FETCH NEXT FROM c INTO @t, @expected;
    END;
    CLOSE c; DEALLOCATE c;
    SELECT CASE WHEN @bad = N'' THEN 1 ELSE 0 END AS ok, CASE WHEN @bad = N'' THEN N'The mirror matches the signed-off counts.' ELSE @bad END AS detail;
END""")
    return b


# Wording of each blocking check's finding: (pass text, text after the count when it fails). Counts only, never values.
_ZERO_TEXT = {
    "KEYS_LOADED": ("Every canonical key is in the mirror table.", "canonical key(s) are missing from the mirror table."),
    "TARGET_KEY_UNIQUE": ("No duplicate keys in the mirror table.", "duplicate key value(s) in the mirror table."),
    "REQUIRED_NOT_BLANK": ("Populated in every mirror row.", "mirror row(s) have a blank value."),
    "VALUES_FIT_TYPE": ("Every populated value converts to the live column's type.",
                        "row(s) hold a value that does not convert to the live column's type; the promotion would fail on them."),
    "VALUES_FIT_LENGTH": ("Every value fits the live column.", "row(s) are longer than the live column allows."),
    "VALUES_IN_REFERENCE_LIST": ("Every populated value is in the target's reference list.", "row(s) hold a value that is not in the target's reference list."),
    "DUPLICATES_RESOLVED": ("No two canonical records share this match value.", "match value(s) are still shared by more than one canonical record."),
    "VALUES_IN_CODE_SET": ("Every populated value is a canonical code.", "canonical row(s) hold a value with no approved canonical code."),
    "NO_ORPHANS": ("Every row points at an existing parent record.", "row(s) point at a parent record that is not in the mirror."),
}
_COUNT_TEXT = {
    "STAGING_ROW_COUNT": "N'The canonical table holds '", "ROWS_LOADED": "N'The mirror table holds '",
}


def _check_sql(p: Dict[str, Any]) -> str:
    """One planned check as T-SQL that stores its finding (and, for a failure, the keys of up to 50 failing rows)."""
    kind, name = p["kind"], p["check"]
    lines = [f"SET @n = ({p['sql']});"]
    if p.get("expected_sql"):
        lines.append(f"SET @x = ({p['expected_sql']});")
    elif kind in ("equals", "at_least") and isinstance(p.get("expected"), int):
        lines.append(f"SET @x = {p['expected']};")
    else:
        lines.append("SET @x = NULL;")
    if kind == "equals":
        status = "CASE WHEN @x IS NULL THEN N'WARN' WHEN @n = @x THEN N'PASS' ELSE N'FAIL' END"
    elif kind == "at_least":
        status = "CASE WHEN @x IS NULL THEN N'WARN' WHEN @n >= @x THEN N'PASS' ELSE N'FAIL' END"
    else:
        status = "CASE WHEN @n = 0 THEN N'PASS' ELSE N'FAIL' END"
    if kind in ("equals", "at_least"):
        what = "the source delivered " if name == "STAGING_ROW_COUNT" else "the canonical table holds "
        detail = (f"CONCAT({_COUNT_TEXT[name]}, @n, N' row(s); {what}', ISNULL(CAST(@x AS nvarchar(30)), N'an unknown number of'), N'.')")
    else:
        ok, bad = _ZERO_TEXT[name]
        detail = f"CASE WHEN @n = 0 THEN {_lit(ok)} ELSE CONCAT(@n, N' ', {_lit(bad)}) END"
    lines.append(f"SET @st = {status};")
    lines.append(f"INSERT INTO [{OPS}].[xodus_validation_result] (run_id, migration_id, batch_id, wave, entity, check_name, table_name, field_name, status, "
                 f"observed_count, expected_count, detail, source) VALUES (@RunId, @MigrationId, @BatchId, @Wave, {_lit(p['entity'])}, {_lit(name)}, "
                 f"{_lit(p.get('table') or '')}, {_lit(p.get('field') or '')}, @st, @n, @x, {detail}, N'BUILTIN');")
    lines.append("SET @rid = SCOPE_IDENTITY();")
    if p.get("keys_sql"):
        lines.append(f"IF @st = N'FAIL' BEGIN INSERT INTO @k ([k]) {p['keys_sql']}; "
                     f"INSERT INTO [{OPS}].[xodus_validation_row_key] (result_id, row_key) SELECT @rid, [k] FROM @k; DELETE FROM @k; END;")
    return "\n".join(lines)


def _validate_procedure(slug_: str, plan: List[Dict[str, Any]], waves: Dict[str, int]) -> str:
    """usp_validate_<slug>: the blocking checks of the entities of one wave (and, when given, only the selected ones), written to
    xodus_validation_result. A failed check is a finding, never an error: the gate reads the findings. A check that compares two entities runs
    in the child's wave, and only when the parent is in the same or an earlier wave."""
    by_entity: Dict[str, List[Dict[str, Any]]] = {}
    for p in plan:
        if p["kind"] not in ("equals", "at_least", "zero") or not p.get("sql"):
            continue
        if p["check"] == "NO_ORPHANS":  # 'column -> Parent.key': the parent is named in the field
            parent = str(p["field"]).split("→", 1)[-1].strip().split(".")[0]
            if waves.get(parent, 99) > waves.get(p["entity"], -1):
                continue
        by_entity.setdefault(p["entity"], []).append(p)
    sel = "SELECT LTRIM(RTRIM(value)) FROM STRING_SPLIT(@Entities, N',') WHERE LTRIM(RTRIM(value)) <> N''"
    body = [
        "DECLARE @scope TABLE ([entity] sysname NOT NULL PRIMARY KEY);",
        f"INSERT INTO @scope SELECT r.[entity] FROM [{OPS}].[xodus_entity_registry] AS r WHERE r.migration_slug = {_lit(slug_)} AND r.[wave] = @Wave "
        f"AND (@Entities = N'' OR r.[entity] IN ({sel}));",
        # Findings of the entities being validated are replaced (a rerun or a second validation never leaves old findings behind).
        f"DELETE k FROM [{OPS}].[xodus_validation_row_key] AS k JOIN [{OPS}].[xodus_validation_result] AS r ON r.result_id = k.result_id "
        "WHERE r.migration_id = @MigrationId AND r.batch_id = @BatchId AND r.[entity] IN (SELECT [entity] FROM @scope);",
        f"DELETE FROM [{OPS}].[xodus_validation_result] WHERE migration_id = @MigrationId AND batch_id = @BatchId AND [entity] IN (SELECT [entity] FROM @scope);",
        "DECLARE @n bigint, @x bigint, @st nvarchar(16), @rid bigint;",
        "DECLARE @k TABLE ([k] nvarchar(400));",
        f"INSERT INTO [{OPS}].[xodus_validation_result] (run_id, migration_id, batch_id, wave, entity, check_name, table_name, field_name, status, detail, source)\n"
        "SELECT @RunId, @MigrationId, @BatchId, @Wave, sc.[entity], N'ENTITY_BUILT', N'', N'', "
        "CASE WHEN st.[status] IS NULL OR st.[status] = N'STALE' THEN N'FAIL' ELSE N'PASS' END, "
        "CASE WHEN st.[status] IS NULL THEN N'The entity was not built in this batch.' "
        "WHEN st.[status] = N'STALE' THEN N'A parent was rebuilt after this entity; rebuild it.' ELSE N'Built in this batch.' END, N'BUILTIN'\n"
        f"FROM @scope AS sc LEFT JOIN [{OPS}].[xodus_entity_state] AS st ON st.migration_id = @MigrationId AND st.batch_id = @BatchId AND st.[entity] = sc.[entity];",
    ]
    for entity in sorted(by_entity):
        checks = "\n".join(_check_sql(p) for p in by_entity[entity])
        body.append(f"IF EXISTS (SELECT 1 FROM @scope WHERE [entity] = {_lit(entity)})\nBEGIN\n" + "\n".join("    " + ln for ln in checks.split("\n")) + "\nEND;")
    body += [
        # An approved exception turns the matching failed finding into WAIVED, which the gate does not count.
        f"UPDATE r SET r.[status] = N'WAIVED' FROM [{OPS}].[xodus_validation_result] AS r JOIN [{OPS}].[xodus_wave_waiver] AS w ON w.migration_id = r.migration_id "
        "AND w.batch_id = r.batch_id AND w.[entity] = r.[entity] AND w.check_name = r.check_name AND w.table_name = r.table_name AND w.field_name = r.field_name "
        "WHERE r.migration_id = @MigrationId AND r.batch_id = @BatchId AND r.[entity] IN (SELECT [entity] FROM @scope) AND r.[status] = N'FAIL';",
        f"UPDATE st SET st.[status] = CASE WHEN EXISTS (SELECT 1 FROM [{OPS}].[xodus_validation_result] AS r WHERE r.migration_id = @MigrationId AND r.batch_id = @BatchId "
        "AND r.[entity] = st.[entity] AND r.[status] = N'FAIL') THEN N'FAILED' ELSE N'VALIDATED' END, st.updated_utc = SYSUTCDATETIME() "
        f"FROM [{OPS}].[xodus_entity_state] AS st WHERE st.migration_id = @MigrationId AND st.batch_id = @BatchId AND st.[entity] IN (SELECT [entity] FROM @scope) AND st.[status] <> N'STALE';",
    ]
    return _proc(f"[{OPS}].[usp_validate_{slug_}]", "@MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64), @Wave int, @Entities nvarchar(2000) = N''", body)


def build(migration_id: str, spec: Dict[str, Any], approved_target: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
          entities: Dict[str, Dict[str, Any]], raw_files: List[Dict[str, Any]], load_order: List[str], env: Dict[str, Any],
          generated_by: str, provenance: Optional[Dict[str, Any]] = None, codes: Optional[Dict[str, Any]] = None,
          depends: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """entities[E] = {"files": [file_name, ...], "mappings": {source_field: {"canonical_field", "expression"}}, "key": canonical key or None,
                      "derivations": [{"canonical_field", "input_field", "rule_expression"}], "canonical_fields": [...]}
    raw_files = [{"file_name", "columns": [...]}] (entity files and the lookup files their rules read).
    env = {"container", "rawFolder", "liveSchema"}.
    codes (canonical model v2) = {"coded": {entity: {canonical_field: code_set}}, "source_map": [{"code_set", "source_value", "canonical_code"}],
                                  "target_map": [{"code_set", "canonical_code", "target_value"}], "target_system": str, "model_version": str}:
    coded canonical fields are translated through xodus_ref.code_map after the entity is built, and through xodus_ref.target_code_map on
    the way to the mirror (a code with no target value passes through).
    depends = {entity: [entities whose records must load first]} (optional): gives each entity its wave in xodus_ops.xodus_entity_registry
    (wave 0 has no parent in the migration). Returns {"files": {path: text}, "manifest": {...}, "version": str}."""
    s = slug(migration_id)
    coded = (codes or {}).get("coded") or {}
    target_system = str((codes or {}).get("target_system") or (spec or {}).get("target_system") or "")
    live_schema = sql_package._ident(env.get("liveSchema") or "simplicity")
    approved_target = [m for m in approved_target if m.get("status") == "APPROVED"]
    if not spec or not approved_target:
        raise ValueError("Approve at least one target mapping before building the pipelines.")
    columns_of = {f["file_name"]: list(f["columns"]) for f in raw_files}
    raw_names = {f["file_name"]: raw_table(s, f["file_name"]) for f in raw_files}

    reading = {"entity": None}  # the entity whose SQL is being generated
    lookups: Dict[str, set] = {}  # entity -> raw tables its canonical and mirror SQL read through lookup rules

    def lookup_ref(parent_file: str) -> str:
        if parent_file not in raw_names:
            raise ValueError(f"A lookup rule reads {parent_file}, which is not among the migration's source files.")
        if reading["entity"]:
            lookups.setdefault(reading["entity"], set()).add(raw_names[parent_file])
        return f"[{RAW}].[{raw_names[parent_file]}]"

    per_migration: List[str] = []
    objects = {"tables": [], "procedures": []}
    for f in raw_files:
        cols = [f"{_header(c)} nvarchar(4000) NULL" for c in f["columns"]] + list(TRACE) + [ROW_ID]
        per_migration.append(_drop_create(RAW, raw_names[f["file_name"]], cols))
        objects["tables"].append(f"{RAW}.{raw_names[f['file_name']]}")

    # Source -> canonical: one table and one procedure per entity.
    canon_counts: Dict[str, Dict[str, Any]] = {}
    applied_codes: Dict[str, Dict[str, str]] = {}
    resolved: Dict[str, str] = {}  # entity -> its crosswalk table (source key -> surviving key)  # the coded fields the canonical procedures translate (stage 10 checks them)
    # Column lineage rows for xodus_ref.column_map: (layer, entity, target_table, target_column, source_file, source_column, step, expression, mapping_id).
    lineage: List[tuple] = []
    for entity in load_order:
        reading["entity"] = entity
        e = entities[entity]
        sql_package._ident(entity)
        ctable = f"{s}_{entity}"
        cfields = list(dict.fromkeys(e["canonical_fields"]))
        per_migration.append(_drop_create(CANON, ctable, [f"{_q(c)} nvarchar(4000) NULL" for c in cfields]
                                          + ["[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL", "[xodus_loaded_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()"]
                                          + list(CANON_LINEAGE)))
        objects["tables"].append(f"{CANON}.{ctable}")
        derived = e.get("derived")
        recipe = entity_resolution.recipe_for(entity)
        if derived and recipe and derived == recipe["method"]:
            # No source file for this entity: the approved derivation, read from the canonical tables of the entities it builds on
            # (earlier in the load order, the holder already resolved).
            needs = [recipe["parent"]["entity"], recipe["holder"]["entity"]]
            if any(n not in entities for n in needs) or load_order.index(entity) < max(load_order.index(n) for n in needs):
                raise ValueError(f"Deriving {entity} needs the {' and '.join(needs)} entities, loaded before {entity}.")
            files, mapped, key = [], list(recipe["columns"]), None
            select = entity_resolution.derive_sql(entity, CANON, s, ctable)
            lineage += [("canonical", entity, ctable, c, None, None, "DERIVATION", f"DERIVE:{derived}", None) for c in mapped]
        elif derived:
            raise ValueError(f"Unknown derivation {derived} for {entity}.")
        else:
            files = [fn for fn in e["files"] if fn in columns_of]
            if not files:
                raise ValueError(f"{entity} has no approved source file to read.")
            # Which canonical field each file supplies (a field can come from several files: the first non-blank wins, as in memory).
            per_file: List[Dict[str, str]] = []
            for i, fn in enumerate(files):
                alias = f"f{i}"
                exprs = {}
                for src, m in sorted(e["mappings"].items()):
                    if src in columns_of[fn] and m["canonical_field"] in cfields:
                        exprs[m["canonical_field"]] = source_expression(m["expression"], src, rules, "s", lookup_ref)
                        lineage.append(("canonical", entity, ctable, m["canonical_field"], fn, src, "MAPPING", str(m["expression"]), None))
                per_file.append(exprs)
            mapped = [c for c in cfields if any(c in p for p in per_file)]
            key = e.get("key") if e.get("key") in mapped else None
            selects = []
            for i, (fn, exprs) in enumerate(zip(files, per_file)):
                body = ", ".join(f"{exprs[c]} AS {_q(c)}" for c in mapped if c in exprs)
                part = f"ROW_NUMBER() OVER (PARTITION BY {exprs[key]} ORDER BY (SELECT NULL)) AS [xodus_rn]" if key and key in exprs else "1 AS [xodus_rn]"
                # Each file's rows keep the file name and raw row id they were read from (the canonical row's lineage).
                selects.append(f"f{i} AS (SELECT {body}, {part}, s.[xodus_source_file] AS [xodus_src_file], s.[xodus_row_id] AS [xodus_src_row] "
                               f"FROM [{RAW}].[{raw_names[fn]}] AS s WHERE s.[xodus_batch_id] = @BatchId)")
            if len(files) == 1:
                exprs = per_file[0]
                pick = f"WHERE (f0.{_q(key)} IS NULL OR f0.[xodus_rn] = 1)" if key and key in exprs else ""
                select = (f"WITH {selects[0]}\nINSERT INTO [{CANON}].[{ctable}] ({', '.join(_q(c) for c in mapped)}, [xodus_source_file], [xodus_source_row], [xodus_batch_id], [xodus_run_id])\n"
                          f"SELECT {', '.join('f0.' + _q(c) for c in mapped if c in exprs)}, f0.[xodus_src_file], f0.[xodus_src_row], @BatchId, @RunId FROM f0 {pick};")
            else:
                if not key:
                    raise ValueError(f"{entity} reads {len(files)} files but no key field is mapped, so their rows cannot be joined.")
                keyed = [i for i, p in enumerate(per_file) if key in p]
                keys = " UNION ".join(f"SELECT {_q(key)} AS k FROM f{i} WHERE {_q(key)} IS NOT NULL" for i in keyed)
                cols = []
                for c in mapped:
                    sources = [f"f{i}.{_q(c)}" for i, p in enumerate(per_file) if c in p and i in keyed]
                    cols.append(f"COALESCE({', '.join(sources)})" if len(sources) > 1 else (sources[0] if sources else "NULL"))
                joins = " ".join(f"LEFT JOIN f{i} ON f{i}.{_q(key)} = k.k AND f{i}.[xodus_rn] = 1" for i in keyed)
                unkeyed = " UNION ALL ".join(
                    "SELECT " + ", ".join((f"f{i}.{_q(c)}" if c in per_file[i] else "NULL") for c in mapped)
                    + f", f{i}.[xodus_src_file], f{i}.[xodus_src_row], @BatchId, @RunId FROM f{i} WHERE f{i}.{_q(key)} IS NULL"
                    for i in keyed)
                # The first file (in the entity's order) that holds the key supplies the row's source file and row id.
                src_file = "COALESCE(" + ", ".join(f"f{i}.[xodus_src_file]" for i in keyed) + ")" if len(keyed) > 1 else f"f{keyed[0]}.[xodus_src_file]"
                src_row = "COALESCE(" + ", ".join(f"f{i}.[xodus_src_row]" for i in keyed) + ")" if len(keyed) > 1 else f"f{keyed[0]}.[xodus_src_row]"
                select = (f"WITH {', '.join(selects)}, k AS ({keys})\nINSERT INTO [{CANON}].[{ctable}] ({', '.join(_q(c) for c in mapped)}, [xodus_source_file], [xodus_source_row], [xodus_batch_id], [xodus_run_id])\n"
                          f"SELECT {', '.join(cols)}, {src_file}, {src_row}, @BatchId, @RunId FROM k {joins}\nUNION ALL {unkeyed};")
        derive = []
        for d in e.get("derivations", []):
            rule = rules.get(str(d["rule_expression"]).strip().upper())
            if not rule:
                raise ValueError(f"The derivation of {entity}.{d['canonical_field']} pins {d['rule_expression']}, which is not approved.")
            if d["canonical_field"] not in cfields:
                continue
            derive.append(f"UPDATE c SET c.{_q(d['canonical_field'])} = {translate(d['rule_expression'], d['input_field'], rule, 'c', lookup_ref, cfields)} "
                          f"FROM [{CANON}].[{ctable}] AS c;")
            lineage.append(("canonical", entity, ctable, d["canonical_field"], None, d["input_field"], "DERIVATION", str(d["rule_expression"]), None))
        # Coded fields: the approved source value -> canonical code map (data in xodus_ref.code_map). A value with no approved code is left
        # as received and fails the stage 10 conformance check (VALUES_IN_CODE_SET).
        filled = set(mapped) | {d["canonical_field"] for d in e.get("derivations", [])}
        for field, code_set in sorted((coded.get(entity) or {}).items()):
            if field in filled and field in cfields:
                applied_codes.setdefault(entity, {})[field] = code_set
                lineage.append(("canonical", entity, ctable, field, None, field, "CODE_MAP", f"CODE_SET:{code_set}", None))
                derive.append(f"UPDATE c SET c.{_q(field)} = m.[canonical_code] FROM [{CANON}].[{ctable}] AS c JOIN [{REF}].[code_map] AS m "
                              f"ON m.[migration_slug] = {_lit(s)} AND m.[code_set] = {_lit(code_set)} AND m.[source_value] = UPPER(LTRIM(RTRIM(c.{_q(field)})));")
        # References to an entity resolved earlier in the load order point at its surviving record (the crosswalk below).
        for field, (ref_entity, _) in sorted((entity_resolution.references().get(entity) or {}).items()):
            if field in filled | set(mapped) and ref_entity in resolved:
                derive.append(f"UPDATE c SET c.{_q(field)} = x.[survivor_key] FROM [{CANON}].[{ctable}] AS c JOIN [{CANON}].[{resolved[ref_entity]}] AS x "
                              f"ON x.[source_key] = c.{_q(field)} WHERE x.[survivor_key] <> x.[source_key];")
        # Entity resolution: rows the approved match rules link become one record (most complete survives); the crosswalk keeps every source key.
        if e.get("resolution"):
            if not key:
                raise ValueError(f"{entity} has match rules but its key field is not mapped, so its rows cannot be resolved.")
            xtable = f"{s}_{entity}_xref"
            per_migration.append(_drop_create(CANON, xtable, ["[source_key] nvarchar(400) NOT NULL", "[survivor_key] nvarchar(400) NOT NULL",
                                                              "[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL"]))
            objects["tables"].append(f"{CANON}.{xtable}")
            derive += entity_resolution.sql_resolve(CANON, ctable, xtable, key, [c for c in mapped if c in cfields] +
                                                    [c for c in filled if c in cfields and c not in mapped], e["resolution"])
            resolved[entity] = xtable
        # The hash covers every value the procedure filled (after derivations, code translation and resolution): a later run can tell
        # whether an entity's row changed. NULL is marked with a character no source value holds, so an empty and a missing value differ.
        hashed = [c for c in cfields if c in set(mapped) | filled]
        if hashed:
            parts = ", ".join(f"COALESCE(c.{_q(c)}, NCHAR(1))" for c in hashed)
            derive.append(f"UPDATE c SET c.[xodus_row_hash] = CONVERT(char(64), HASHBYTES('SHA2_256', CONCAT_WS(N'|', CAST(N'' AS nvarchar(max)), {parts})), 2) "
                          f"FROM [{CANON}].[{ctable}] AS c;")
        log = f"EXEC [{OPS}].[usp_xodus_count] @MigrationId, @RunId, @BatchId, N'canonical', N'{CANON}', N'{ctable}';"
        # Entity state: built in this batch, with a digest of its row hashes (a rerun can tell whether anything changed); dependants become stale.
        derive.append(f"DECLARE @n bigint, @d char(64);\n"
                      f"SELECT @n = COUNT_BIG(*), @d = CONVERT(char(64), HASHBYTES('SHA2_256', CONCAT(COUNT_BIG(*), N'|', ISNULL(CAST(CHECKSUM_AGG(CHECKSUM([xodus_row_hash])) AS nvarchar(40)), N''))), 2) "
                      f"FROM [{CANON}].[{ctable}];\n"
                      f"EXEC [{OPS}].[usp_xodus_entity_state] @MigrationSlug = {_lit(s)}, @MigrationId = @MigrationId, @BatchId = @BatchId, @Entity = {_lit(entity)}, "
                      "@Stage = N'canonical', @RunId = @RunId, @Rows = @n, @Digest = @d;")
        per_migration.append(_proc(f"[{CANON}].[usp_canon_{s}_{entity}]", "@MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64)",
                                   [f"TRUNCATE TABLE [{CANON}].[{ctable}];", select] + derive + [log]))
        objects["procedures"].append(f"{CANON}.usp_canon_{s}_{entity}")
        canon_counts[entity] = {"table": ctable, "files": [raw_names[f] for f in files], "key": key, "xref": resolved.get(entity),
                                "resolution": e.get("resolution") or [], "derived": derived}

    # Canonical -> mirror: one mirror table per live table, filled by one procedure (all tables in one transaction).
    mirror_by_entity: Dict[str, List[str]] = {}
    live_tables: List[Dict[str, Any]] = []
    checked: Dict[str, Dict[str, Any]] = {}  # entity -> what the blocking checks of its wave read (stage 10 builds the same shape from the live schema)
    code_lists = (codes or {}).get("code_lists") or {}
    for entity in sorted({m["entity_name"] for m in approved_target}):
        reading["entity"] = entity
        mirror_body: List[str] = mirror_by_entity.setdefault(entity, [])
        body = spec["entities"].get(entity)
        if not body or entity not in canon_counts:
            raise ValueError(f"Approved target mappings reference {entity}, which has no confirmed target model or no canonical table.")
        fields = {f["name"]: f for f in body["fields"]}
        rows = [m for m in approved_target if m["entity_name"] == entity and m["target_field"] in fields and m.get("canonical_field")]
        for table in sorted({fields[m["target_field"]]["table"] for m in rows}):
            sql_package._ident(table)
            trows = sorted([m for m in rows if fields[m["target_field"]]["table"] == table], key=lambda r: r["target_field"])
            # The mirror holds the values exactly as they will be promoted, as text: a value that does not fit the live column's type is a
            # stage 10 finding (VALUES_FIT_TYPE), and the live tables enforce their own types when the batch is merged.
            all_cols = sorted({(f["column"], f.get("type") or "string") for f in body["fields"] if f["table"] == table})
            mtable = f"{s}_{table}"
            per_migration.append(_drop_create(MIRROR, mtable, [f"{_q(c)} nvarchar(4000) NULL" for c, _ in all_cols]
                                              + ["[xodus_source_key] nvarchar(400) NULL", "[xodus_source_row] bigint NULL", "[xodus_batch_id] nvarchar(64) NULL",
                                                 "[xodus_run_id] nvarchar(64) NULL"]))
            objects["tables"].append(f"{MIRROR}.{mtable}")
            exprs, cols, keys = [], [], []
            for m in trows:
                col = sql_package._ident(fields[m["target_field"]]["column"])
                rule = rules.get(m["transformation_expression"])
                e = translate(m["transformation_expression"], m["canonical_field"], rule, "s", lookup_ref)
                code_set = (coded.get(entity) or {}).get(m["canonical_field"])
                if code_set:  # canonical code -> the target system's value (xodus_ref.target_code_map); a code with no target value passes through
                    e = (f"COALESCE((SELECT TOP (1) t.[target_value] FROM [{REF}].[target_code_map] AS t WHERE t.[target_system] = {_lit(target_system)} "
                         f"AND t.[code_set] = {_lit(code_set)} AND t.[canonical_code] = {e}), {e})")
                exprs.append(f"{e} AS {_q(col)}")
                cols.append(col)
                lineage.append(("mirror", entity, mtable, col, None, m["canonical_field"], "MAPPING", str(m["transformation_expression"]), m.get("target_mapping_id")))
                if sql_package._is_key_field(m["canonical_field"], entity):
                    keys.append(e)
            # Each mirror row keeps the canonical key of the source row it came from (xodus_source_key): stage 10 names failing rows by it.
            ckey = canon_counts[entity]["key"]
            source_key = f"s.{_q(ckey)}" if ckey else "CAST(NULL AS nvarchar(400))"
            # One row per target key; a table without a mapped key keeps one row per distinct set of values (the first source key).
            part = ", ".join(keys) if keys else ", ".join(e.rsplit(" AS ", 1)[0] for e in exprs)
            # An entity with no key has no source key to order by, and SQL Server rejects a constant in a window's ORDER BY.
            order_by = source_key if ckey else "(SELECT NULL)"
            dedupe = f"ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order_by}) AS [xodus_rn]"
            mirror_body.append(f"TRUNCATE TABLE [{MIRROR}].[{mtable}];")
            mirror_body.append(
                f"INSERT INTO [{MIRROR}].[{mtable}] ({', '.join(_q(c) for c in cols)}, [xodus_source_key], [xodus_source_row], [xodus_batch_id], [xodus_run_id])\n"
                f"SELECT {', '.join('x.' + _q(c) for c in cols)}, x.[xodus_source_key], x.[xodus_source_row], @BatchId, @RunId FROM (\n"
                f"    SELECT {', '.join(exprs)}, {source_key} AS [xodus_source_key], s.[xodus_source_row] AS [xodus_source_row], {dedupe}\n    FROM [{CANON}].[{canon_counts[entity]['table']}] AS s\n) AS x"
                " WHERE x.[xodus_rn] = 1;")
            mirror_body.append(f"EXEC [{OPS}].[usp_xodus_count] @MigrationId, @RunId, @BatchId, N'mirror', N'{MIRROR}', N'{mtable}';")
            key_cols = [c for c, m in zip(cols, trows) if sql_package._is_key_field(m["canonical_field"], entity)]
            types = {c: t for c, t in all_cols}
            cn = canon_counts[entity]
            ce = checked.setdefault(entity, {"staging_table": cn["table"], "expected_rows": None, "key": cn["key"], "tables": {}, "unmapped_required": []})
            if len(cn["files"]) == 1:
                ce["expected_sql"] = f"SELECT COUNT(*) FROM [{RAW}].[{cn['files'][0]}]"
            if cn.get("xref"):
                ce["expected_sql"] = f"SELECT COUNT(DISTINCT [survivor_key]) FROM [{CANON}].[{cn['xref']}]"
                ce["resolution"] = cn.get("resolution") or []
            coded_here = {f: cs for f, cs in (applied_codes.get(entity) or {}).items() if code_lists.get(cs)}
            if coded_here:
                ce["code_sets"] = {f: list(code_lists[cs]) for f, cs in coded_here.items()}
            fmap = {f["name"]: f for f in body["fields"]}
            ce["tables"][mtable] = {
                "columns": {m["canonical_field"]: sql_package._ident(fmap[m["target_field"]]["column"]) for m in trows},
                "required": [sql_package._ident(fmap[m["target_field"]]["column"]) for m in trows if fmap[m["target_field"]].get("required")],
                "code_lists": {}, "max_lengths": {}, "row_key": "xodus_source_key",
                "types": {sql_package._ident(fmap[m["target_field"]]["column"]): fmap[m["target_field"]]["type"] for m in trows
                          if fmap[m["target_field"]].get("type") and str(fmap[m["target_field"]]["type"]).lower() not in
                          ("string", "nvarchar", "varchar", "nchar", "char", "text", "ntext", "boolean", "bit")}}
            live_tables.append({"entity": entity, "live_table": table, "mirror_table": mtable, "columns": cols, "key_columns": key_cols,
                                "all_columns": [c for c, _ in all_cols], "types": {c: types.get(c, "string") for c in cols}})
    if not live_tables:
        raise ValueError("No approved target mapping has a canonical source, so there is nothing to load.")
    mirror_params = "@MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64)"
    for entity, body_sql in mirror_by_entity.items():
        if not body_sql:
            continue
        state = (f"EXEC [{OPS}].[usp_xodus_entity_state] @MigrationSlug = {_lit(s)}, @MigrationId = @MigrationId, @BatchId = @BatchId, @Entity = {_lit(entity)}, "
                 "@Stage = N'mirror', @RunId = @RunId;")
        per_migration.append(_proc(f"[{MIRROR}].[usp_mirror_{s}_{entity}]", mirror_params, body_sql + [state]))
        objects["procedures"].append(f"{MIRROR}.usp_mirror_{s}_{entity}")
    # The whole mirror (a full batch) is the entity procedures in order, in one transaction (their own transactions nest inside it).
    per_migration.append(_proc(f"[{MIRROR}].[usp_mirror_{s}]", mirror_params,
                               [f"EXEC [{MIRROR}].[usp_mirror_{s}_{e}] @MigrationId = @MigrationId, @BatchId = @BatchId, @RunId = @RunId;"
                                for e, b in mirror_by_entity.items() if b]))
    objects["procedures"].append(f"{MIRROR}.usp_mirror_{s}")

    # Registry rows the stable pipelines read.
    folder = str(env.get("rawFolder") or "").strip("/")
    container = str(env.get("container") or "")
    rows = [f"DELETE FROM [{OPS}].[{t}] WHERE migration_slug = {_lit(s)};" for t in ("xodus_raw_files", "xodus_load_order", "xodus_live_tables")]
    rows += [f"INSERT INTO [{OPS}].[xodus_raw_files] (migration_slug, seq, container, folder, file_name, raw_table) VALUES "
             + ", ".join(f"({_lit(s)}, {i + 1}, {_lit(container)}, {_lit(folder)}, {_lit(f['file_name'])}, {_lit(raw_names[f['file_name']])})" for i, f in enumerate(raw_files)) + ";"]
    rows += [f"INSERT INTO [{OPS}].[xodus_load_order] (migration_slug, seq, entity) VALUES "
             + ", ".join(f"({_lit(s)}, {i + 1}, {_lit(e)})" for i, e in enumerate(load_order)) + ";"]
    rows += [f"INSERT INTO [{OPS}].[xodus_live_tables] (migration_slug, mirror_table, live_table) VALUES "
             + ", ".join(f"({_lit(s)}, {_lit(t['mirror_table'])}, {_lit(t['live_table'])})" for t in live_tables) + ";"]
    # Entity registry: each entity's wave (0 = no parent in this migration; otherwise one more than its deepest parent), parents, files and tables.
    deps: Dict[str, List[str]] = {}
    for entity in load_order:
        wanted = set((depends or {}).get(entity) or ())
        recipe = entity_resolution.recipe_for(entity) if canon_counts[entity]["derived"] else None
        if recipe:
            wanted |= {recipe["parent"]["entity"], recipe["holder"]["entity"]}
        deps[entity] = sorted(d for d in wanted if d in canon_counts and d != entity and load_order.index(d) < load_order.index(entity))
    waves: Dict[str, int] = {}
    for entity in load_order:  # the load order already puts parents first; a parent that comes later (a cycle) is ignored
        waves[entity] = 1 + max([waves[d] for d in deps[entity] if d in waves], default=-1)
    from control_plane.services import target_validation  # the checks of stage 10, planned over the XODUS canonical and mirror tables
    plan = target_validation.build_plan(schema=MIRROR, staging_schema=CANON, entities=checked)
    per_migration.append(_validate_procedure(s, plan, waves))
    objects["procedures"].append(f"{OPS}.usp_validate_{s}")
    mirrors: Dict[str, List[str]] = {}
    for t in live_tables:
        mirrors.setdefault(t["entity"], []).append(t["mirror_table"])
    rows.append(f"DELETE FROM [{OPS}].[xodus_entity_registry] WHERE migration_slug = {_lit(s)};")
    rows.append(f"INSERT INTO [{OPS}].[xodus_entity_registry] (migration_slug, entity, wave, depends_on, source_files, canon_table, mirror_tables) VALUES "
                + ", ".join(f"({_lit(s)}, {_lit(e)}, {waves[e]}, {_lit(','.join(deps[e]))}, {_lit(','.join(sorted(set(canon_counts[e]['files']) | lookups.get(e, set()))))}, "
                            f"{_lit(canon_counts[e]['table'])}, {_lit(','.join(sorted(mirrors.get(e, []))))})" for e in load_order) + ";")
    # Column lineage (xodus_ref.column_map): rewritten on every setup, so it always matches the deployed procedures.
    lineage = sorted(set(lineage), key=lambda r: tuple("" if v is None else str(v) for v in r))
    rows.append(f"DELETE FROM [{REF}].[column_map] WHERE migration_slug = {_lit(s)};")
    for i in range(0, len(lineage), 500):  # SQL Server allows at most 1000 rows in one VALUES list
        rows.append(f"INSERT INTO [{REF}].[column_map] (migration_slug, layer, entity, target_table, target_column, source_file, source_column, step, expression, mapping_id) VALUES "
                    + ", ".join("(" + ", ".join([_lit(s)] + ["NULL" if v is None else _lit(v) for v in r]) + ")" for r in lineage[i:i + 500]) + ";")
    if codes is not None:  # the code maps are data: rewritten on every setup, read by the procedures above
        source_map = sorted({(r["code_set"], r["source_value"], r["canonical_code"]) for r in codes.get("source_map") or []})
        target_map = sorted({(r["code_set"], r["canonical_code"], r["target_value"]) for r in codes.get("target_map") or []})
        rows.append(f"DELETE FROM [{REF}].[code_map] WHERE migration_slug = {_lit(s)};")
        if source_map:
            rows.append(f"INSERT INTO [{REF}].[code_map] (migration_slug, code_set, source_value, canonical_code) VALUES "
                        + ", ".join(f"({_lit(s)}, {_lit(cs)}, {_lit(v)}, {_lit(c)})" for cs, v, c in source_map) + ";")
        rows.append(f"DELETE FROM [{REF}].[target_code_map] WHERE target_system = {_lit(target_system)};")
        if target_map:
            rows.append(f"INSERT INTO [{REF}].[target_code_map] (target_system, code_set, canonical_code, target_value) VALUES "
                        + ", ".join(f"({_lit(target_system)}, {_lit(cs)}, {_lit(c)}, {_lit(v)})" for cs, c, v in target_map) + ";")

    # Simplicity: landing tables and the promotion procedure (MERGE into the live tables, all tables in one transaction).
    live: List[str] = [_schema(LANDING),
                       f"IF OBJECT_ID(N'{LANDING}.xodus_schema_version', N'U') IS NULL CREATE TABLE [{LANDING}].[xodus_schema_version] ([migration_slug] nvarchar(32) NOT NULL, "
                       "[component] nvarchar(32) NOT NULL, [version] nvarchar(64) NOT NULL, [deployed_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME(), "
                       "CONSTRAINT [PK_landing_schema_version] PRIMARY KEY ([migration_slug], [component]));",
                       f"IF OBJECT_ID(N'{LANDING}.xodus_promotion_result', N'U') IS NULL CREATE TABLE [{LANDING}].[xodus_promotion_result] ([result_id] bigint IDENTITY(1,1) "
                       "NOT NULL PRIMARY KEY, [migration_slug] nvarchar(32) NOT NULL, [batch_id] nvarchar(64) NOT NULL, [live_table] sysname NOT NULL, [inserted] bigint NOT NULL, "
                       "[updated] bigint NOT NULL, [promoted_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME());"]
    promote: List[str] = ["DECLARE @a TABLE ([act] nvarchar(10));"]
    for t in live_tables:
        cols = [f"{_q(c)} nvarchar(4000) NULL" for c in t["all_columns"]] + ["[xodus_source_key] nvarchar(400) NULL", "[xodus_source_row] bigint NULL",
                                                                              "[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL"]
        live.append(_drop_create(LANDING, t["mirror_table"], cols))
        src_cols = ", ".join(_q(c) for c in t["columns"])
        insert_only = not t["key_columns"]
        on_cols = t["columns"] if insert_only else t["key_columns"]
        on = " AND ".join((f"(tgt.{_q(c)} = src.{_q(c)} OR (tgt.{_q(c)} IS NULL AND src.{_q(c)} IS NULL))" if insert_only else f"tgt.{_q(c)} = src.{_q(c)}") for c in on_cols)
        others = [c for c in t["columns"] if c not in on_cols]
        update = (f"\nWHEN MATCHED THEN UPDATE SET " + ", ".join(f"tgt.{_q(c)} = src.{_q(c)}" for c in others)) if others and not insert_only else ""
        promote += ["DELETE FROM @a;",
                    f"MERGE [{live_schema}].[{t['live_table']}] AS tgt\nUSING (SELECT {src_cols} FROM [{LANDING}].[{t['mirror_table']}] WHERE [xodus_batch_id] = @BatchId) AS src\n"
                    f"ON {on}{update}\nWHEN NOT MATCHED BY TARGET THEN INSERT ({src_cols}) VALUES ({', '.join('src.' + _q(c) for c in t['columns'])})\nOUTPUT $action INTO @a;",
                    f"INSERT INTO [{LANDING}].[xodus_promotion_result] (migration_slug, batch_id, live_table, inserted, updated) "
                    f"SELECT {_lit(s)}, @BatchId, {_lit(t['live_table'])}, COUNT(CASE WHEN act = N'INSERT' THEN 1 END), COUNT(CASE WHEN act = N'UPDATE' THEN 1 END) FROM @a;"]
    live.append(_proc(f"[{LANDING}].[usp_promote_{s}]", "@BatchId nvarchar(64)", promote))
    live.append(f"CREATE OR ALTER PROCEDURE [{LANDING}].[usp_xodus_promote] @MigrationSlug nvarchar(32), @BatchId nvarchar(64)\nAS\nBEGIN\n    SET NOCOUNT ON;\n"
                f"    IF @MigrationSlug LIKE N'%[^a-z0-9]%' THROW 50001, N'Unsafe migration name.', 1;\n"
                f"    DECLARE @proc nvarchar(300) = QUOTENAME(N'{LANDING}') + N'.' + QUOTENAME(N'usp_promote_' + @MigrationSlug);\n"
                f"    IF OBJECT_ID(@proc, N'P') IS NULL THROW 50002, N'The promotion procedure for this migration is not deployed. Run the setup pipeline first.', 1;\n"
                f"    DECLARE @sql nvarchar(400) = N'EXEC ' + @proc + N' @BatchId = @b';\n"
                f"    EXEC sp_executesql @sql, N'@b nvarchar(64)', @b = @BatchId;\n"
                f"    SELECT live_table, inserted, updated FROM [{LANDING}].[xodus_promotion_result] WHERE migration_slug = @MigrationSlug AND batch_id = @BatchId ORDER BY result_id;\nEND")

    xodus_batches = shared_sql() + per_migration + rows
    version = hashlib.sha256(("\n".join(xodus_batches) + "\n".join(live)).encode("utf-8")).hexdigest()[:16]
    xodus_batches.append(f"DELETE FROM [{OPS}].[xodus_schema_version] WHERE migration_slug = {_lit(s)} AND component = N'xodus';\n"
                         f"INSERT INTO [{OPS}].[xodus_schema_version] (migration_slug, component, version) VALUES ({_lit(s)}, N'xodus', {_lit(version)});")
    live.append(f"DELETE FROM [{LANDING}].[xodus_schema_version] WHERE migration_slug = {_lit(s)} AND component = N'landing';\n"
                f"INSERT INTO [{LANDING}].[xodus_schema_version] (migration_slug, component, version) VALUES ({_lit(s)}, N'landing', {_lit(version)});")

    files: Dict[str, Any] = {}
    for name, props in _pipelines(s, xodus_batches, live).items():
        files[f"pipeline/{name}.json"] = {"name": name, "properties": props}
    for name, props in _datasets().items():
        files[f"dataset/{name}.json"] = {"name": name, "properties": props}
    out = {p: json.dumps(c, indent=2) for p, c in files.items()}
    out[f"sql/xodus_{s}.sql"] = "\n\nGO\n\n".join(xodus_batches) + "\n\nGO\n"
    out[f"sql/simplicity_{s}.sql"] = "\n\nGO\n\n".join(live) + "\n\nGO\n"
    manifest = {
        "carrier": "XODUS-SQL-ADF", "migrationId": migration_id, "migrationSlug": s, "version": version, "generatedBy": generated_by,
        "generatedUtc": datetime.now(timezone.utc).isoformat(), "targetSystem": spec.get("target_system"), "targetModelVersion": spec.get("spec_version"),
        "liveSchema": live_schema, "pipelines": list(STABLE_PIPELINES) + [f"PL_XODUS_Setup_{s}"], "setupPipeline": f"PL_XODUS_Setup_{s}",
        "linkedServices": [LS_BLOB, LS_XODUS, LS_LIVE], "rawFiles": [{"file": f["file_name"], "table": raw_names[f["file_name"]]} for f in raw_files],
        "rawFolder": folder, "container": container, "loadOrder": load_order, "waves": waves, "dependsOn": deps, "columnMapRows": len(lineage),
        "waveChecks": {e: sorted({p["check"] for p in plan if p["entity"] == e and p["kind"] in ("equals", "at_least", "zero")}) for e in checked},
        "canonical": canon_counts, "liveTables": live_tables,
        "objects": objects, "provenance": provenance or {}, "deployed": False,
        "codeSets": ({"modelVersion": codes.get("model_version"), "coded": applied_codes, "sourceValues": len(codes.get("source_map") or []),
                      "sourceMap": sorted([r["code_set"], r["source_value"], r["canonical_code"]] for r in codes.get("source_map") or []),
                      "targetValues": len(codes.get("target_map") or []),
                      "targetMap": sorted([r["code_set"], r["canonical_code"], r["target_value"]] for r in codes.get("target_map") or []), "targetSystem": target_system} if codes is not None else None),
    }
    out["manifest.json"] = json.dumps(manifest, indent=2)
    out["README.txt"] = (
        f"XODUS data-plane package for {migration_id} (version {version}). Nothing was executed or deployed by XODUS.\n\n"
        "Pipelines (stable; they take the migration and batch as parameters):\n"
        "  PL_XODUS_Run                 one batch: raw files, then wave by wave (started by XODUS through the service principal); Entities limits it to some entities\n"
        "  PL_XODUS_Load_Raw            loads the raw files (or only those the selected entities read)\n"
        "  PL_XODUS_Wave                one wave: its entities are built in parallel (canonical, mirror), then the blocking checks run as SQL; a failure stops the run\n"
        "  PL_XODUS_Load_Raw_File       one source file from Blob storage into its xodus_raw table, as received\n"
        "  PL_XODUS_Canonical_Entity    runs the entity's generated procedure (raw -> canonical)\n"
        "  PL_XODUS_Canonical_To_Mirror canonical -> mirror of the live tables, for one entity (or all, in one transaction)\n"
        "  PL_XODUS_Promote             after sign-off: checks the promotion gate, copies the mirror to Simplicity and merges into the live tables\n"
        f"  PL_XODUS_Setup_{s}   deploys this migration's SQL (run it after merging, before the first run and after every change)\n\n"
        f"Linked services needed in the factory (created by hand, managed identity): {LS_BLOB}, {LS_XODUS} (XODUS database), {LS_LIVE} (Simplicity database).\n"
        f"SQL: sql/xodus_{s}.sql runs in the XODUS database, sql/simplicity_{s}.sql in Simplicity (the setup pipeline runs both).\n")
    return {"files": out, "manifest": manifest, "version": version}


def _e(value: str) -> Dict[str, str]:
    return {"value": value, "type": "Expression"}


def _p(name: str) -> Dict[str, str]:
    return _e(f"@pipeline().parameters.{name}")


def _ref(name: str, kind: str) -> Dict[str, str]:
    return {"referenceName": name, "type": kind}


def _script(name: str, ls: str, text: str, params: List[tuple], depends: Optional[List[tuple]] = None) -> Dict[str, Any]:
    """A Script activity whose values are passed as script parameters (never spliced into the SQL text)."""
    return {"name": name, "type": "Script", "linkedServiceName": _ref(ls, "LinkedServiceReference"),
            "dependsOn": [{"activity": a, "dependencyConditions": [c]} for a, c in (depends or [])],
            "policy": {"timeout": "0.02:00:00", "retry": 0},
            "typeProperties": {"scripts": [{"type": "NonQuery", "text": text,
                                            "parameters": [{"name": n, "type": t, "direction": "Input", "value": v} for n, t, v in params]}]}}


def _log(name: str, pipeline: str, step: Any, status: str, depends: List[tuple], run: str = "@pipeline().parameters.RunId",
         rows_read: Optional[str] = None, rows_written: Optional[str] = None, message: Optional[str] = None) -> Dict[str, Any]:
    return _script(name, LS_XODUS,
                   f"EXEC [{OPS}].[usp_xodus_log] @MigrationId = @mig, @RunId = @run, @BatchId = @batch, @Pipeline = @pl, @Step = @step, @Status = @status, "
                   "@RowsRead = @rr, @RowsWritten = @rw, @Message = @msg",
                   [("mig", "String", _p("MigrationId")), ("run", "String", _e(run)), ("batch", "String", _p("BatchId")), ("pl", "String", pipeline),
                    ("step", "String", step), ("status", "String", status), ("rr", "Int64", _e(rows_read or "@null")),
                    ("rw", "Int64", _e(rows_written or "@null")), ("msg", "String", _e(message or "@null"))], depends)


def _err(activity: str) -> str:
    e = f"string(activity('{activity}').error.message)"
    return f"@if(greater(length({e}), 3900), substring({e}, 0, 3900), {e})"


def _guarded(main: Dict[str, Any], pipeline: str, step: Any, run: str = "@pipeline().parameters.RunId", ok_rows: tuple = (None, None)) -> List[Dict[str, Any]]:
    """main, then a log row for success or failure; a failure ends the pipeline as failed."""
    n = main["name"]
    return [main,
            _log(f"Log_OK_{n}", pipeline, step, "SUCCEEDED", [(n, "Succeeded")], run, *ok_rows),
            _log(f"Log_Fail_{n}", pipeline, step, "FAILED", [(n, "Failed")], run, message=_err(n)),
            {"name": f"Fail_{n}", "type": "Fail", "dependsOn": [{"activity": f"Log_Fail_{n}", "dependencyConditions": ["Succeeded"]}],
             "typeProperties": {"message": _e(_err(n)), "errorCode": "XODUS_STEP_FAILED"}}]


def _params(*names: str, **defaults: str) -> Dict[str, Any]:
    return {n: ({"type": "string", "defaultValue": defaults[n]} if n in defaults else {"type": "string"}) for n in names}


def _exec(name: str, pipeline: str, params: Dict[str, Any], depends: Optional[List[tuple]] = None) -> Dict[str, Any]:
    return {"name": name, "type": "ExecutePipeline", "dependsOn": [{"activity": a, "dependencyConditions": [c]} for a, c in (depends or [])],
            "typeProperties": {"pipeline": _ref(pipeline, "PipelineReference"), "waitOnCompletion": True, "parameters": params}}


def _lookup(name: str, proc: str, params: Dict[str, Any], depends: Optional[List[tuple]] = None, first_row: bool = False,
            types: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {"name": name, "type": "Lookup", "dependsOn": [{"activity": a, "dependencyConditions": [c]} for a, c in (depends or [])],
            "policy": {"timeout": "0.00:30:00", "retry": 0},
            "typeProperties": {"source": {"type": "AzureSqlSource", "sqlReaderStoredProcedureName": f"[{OPS}].[{proc}]",
                                          "storedProcedureParameters": {k: {"type": (types or {}).get(k, "String"), "value": v} for k, v in params.items()}},
                               "dataset": {"referenceName": "DS_XODUS_Sql", "type": "DatasetReference", "parameters": {"schema": OPS, "table": "xodus_run_log"}},
                               "firstRowOnly": first_row}}


def _pipelines(s: str, xodus_batches: List[str], live_batches: List[str]) -> Dict[str, Dict[str, Any]]:
    run_params = {"MigrationId": _p("MigrationId"), "MigrationSlug": _p("MigrationSlug"), "BatchId": _p("BatchId")}
    child = {**run_params, "RunId": _e("@pipeline().RunId")}
    pass_run = {**run_params, "RunId": _p("RunId")}
    p: Dict[str, Dict[str, Any]] = {}

    start = _log("Log_Start", "PL_XODUS_Run", "Pipeline", "STARTED", [], "@pipeline().RunId")
    # Entities is a comma-separated list: empty runs the whole batch; otherwise only those entities are rebuilt inside the existing batch
    # (the scope check refuses an unknown entity, a batch that was never built, another package version, or a parent that is stale or not built).
    check = _script("Check_Scope", LS_XODUS, f"EXEC [{OPS}].[usp_xodus_check_scope] @MigrationSlug = @slug, @MigrationId = @mig, @BatchId = @batch, @Entities = @entities",
                    [("slug", "String", _p("MigrationSlug")), ("mig", "String", _p("MigrationId")), ("batch", "String", _p("BatchId")),
                     ("entities", "String", _p("Entities"))], [("Log_Start", "Succeeded")])
    load = _exec("Run_Load_Raw", "PL_XODUS_Load_Raw", {**child, "Entities": _p("Entities")}, [("Log_OK_Check_Scope", "Succeeded")])
    waves = _lookup("Get_Waves", "usp_xodus_waves", {"MigrationSlug": _p("MigrationSlug"), "Entities": _p("Entities")}, [("Run_Load_Raw", "Succeeded")])
    # One wave after another (a wave's entities are siblings and run in parallel inside PL_XODUS_Wave); a failed gate stops the run before the next wave.
    each_wave = {"name": "Run_Waves", "type": "ForEach", "dependsOn": [{"activity": "Get_Waves", "dependencyConditions": ["Succeeded"]}],
                 "typeProperties": {"items": _e("@activity('Get_Waves').output.value"), "isSequential": True,
                                    "activities": [_exec("Run_Wave", "PL_XODUS_Wave", {**child, "Wave": _e("@string(item().wave)"), "Entities": _p("Entities")})]}}
    acts = [start, *_guarded(check, "PL_XODUS_Run", "Check_Scope", "@pipeline().RunId"),
            *_guarded(load, "PL_XODUS_Run", "Load_Raw", "@pipeline().RunId"), waves,
            *_guarded(each_wave, "PL_XODUS_Run", "Waves", "@pipeline().RunId"),
            _log("Log_Done", "PL_XODUS_Run", "Pipeline", "SUCCEEDED", [("Log_OK_Run_Waves", "Succeeded")], "@pipeline().RunId")]
    for a in acts:  # a failed step also closes the run row as FAILED
        if a["name"].startswith("Log_Fail_"):
            a["typeProperties"]["scripts"][0]["parameters"][4]["value"] = "Pipeline"
    p["PL_XODUS_Run"] = {"description": "One batch: raw files, then wave by wave (canonical, mirror, blocking validation), or (Entities set) only those entities "
                                        "rebuilt inside an existing batch. Started by XODUS; never touches the live tables.",
                         "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "Entities", Entities=""), "activities": acts, "folder": {"name": "XODUS"}}

    scoped = {"MigrationSlug": _p("MigrationSlug"), "Entities": _p("Entities")}
    files = _lookup("Get_Raw_Files", "usp_xodus_raw_files", scoped)
    each_file = {"name": "Load_Each_Raw_File", "type": "ForEach", "dependsOn": [{"activity": "Get_Raw_Files", "dependencyConditions": ["Succeeded"]}],
                 "typeProperties": {"items": _e("@activity('Get_Raw_Files').output.value"), "isSequential": False, "batchCount": 4,
                                    "activities": [_exec("Load_Raw_File", "PL_XODUS_Load_Raw_File", {**pass_run, "Container": _e("@item().container"),
                                                         "Folder": _e("@item().folder"), "File": _e("@item().file_name"), "Table": _e("@item().raw_table")})]}}
    p["PL_XODUS_Load_Raw"] = {"description": "Loads the raw files of the migration (or only those the selected entities read) from Blob storage, as received.",
                              "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "RunId", "Entities", Entities=""),
                              "activities": [files, each_file], "folder": {"name": "XODUS"}}

    # One wave: its entities are siblings (none depends on another), so they are built in parallel; the blocking checks run as SQL on the
    # tables just built, and a failed check stops the run here, before any entity that depends on this wave is built.
    wave_scope = {"MigrationSlug": _p("MigrationSlug"), "Wave": _p("Wave"), "Entities": _p("Entities")}
    wave_types = {"Wave": "Int32"}
    get_entities = _lookup("Get_Wave_Entities", "usp_xodus_wave_entities", wave_scope, types=wave_types)
    build_each = {"name": "Build_Each_Entity", "type": "ForEach", "dependsOn": [{"activity": "Get_Wave_Entities", "dependencyConditions": ["Succeeded"]}],
                  "typeProperties": {"items": _e("@activity('Get_Wave_Entities').output.value"), "isSequential": False, "batchCount": 4,
                                     "activities": [_exec("Build_Entity", "PL_XODUS_Canonical_Entity", {**pass_run, "Entity": _e("@item().entity")})]}}
    get_mirrors = _lookup("Get_Wave_Mirror_Entities", "usp_xodus_wave_mirror_entities", wave_scope, [("Build_Each_Entity", "Succeeded")], types=wave_types)
    mirror_each = {"name": "Build_Each_Mirror", "type": "ForEach", "dependsOn": [{"activity": "Get_Wave_Mirror_Entities", "dependencyConditions": ["Succeeded"]}],
                   "typeProperties": {"items": _e("@activity('Get_Wave_Mirror_Entities').output.value"), "isSequential": False, "batchCount": 4,
                                      "activities": [_exec("Build_Mirror", "PL_XODUS_Canonical_To_Mirror", {**pass_run, "Entity": _e("@item().entity")})]}}
    validate = _lookup("Validate_Wave", "usp_xodus_validate_wave",
                       {"MigrationSlug": _p("MigrationSlug"), "MigrationId": _p("MigrationId"), "BatchId": _p("BatchId"), "RunId": _p("RunId"),
                        "Wave": _p("Wave"), "Entities": _p("Entities")}, [("Build_Each_Mirror", "Succeeded")], first_row=True, types=wave_types)
    wave_step = _e("@concat('Wave ', pipeline().parameters.Wave)")
    gate_wave = {"name": "Gate_Wave", "type": "IfCondition", "dependsOn": [{"activity": "Validate_Wave", "dependencyConditions": ["Succeeded"]}],
                 "typeProperties": {"expression": _e("@not(equals(activity('Validate_Wave').output.firstRow.ok, 1))"),
                                    "ifTrueActivities": [_log("Log_Wave_Failed", "PL_XODUS_Wave", wave_step, "FAILED", [],
                                                              message="@activity('Validate_Wave').output.firstRow.detail"),
                                                         {"name": "Fail_Wave", "type": "Fail", "dependsOn": [{"activity": "Log_Wave_Failed", "dependencyConditions": ["Succeeded"]}],
                                                          "typeProperties": {"message": _e("@activity('Validate_Wave').output.firstRow.detail"), "errorCode": "XODUS_WAVE"}}]}}
    p["PL_XODUS_Wave"] = {"description": "One wave: build its entities (canonical, then mirror) in parallel, run the blocking checks as SQL, and stop the run if any fails.",
                          "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "RunId", "Wave", "Entities", Entities=""),
                          "activities": [get_entities, build_each, get_mirrors, mirror_each, validate, gate_wave,
                                         _log("Log_Wave_OK", "PL_XODUS_Wave", wave_step, "SUCCEEDED", [("Gate_Wave", "Succeeded")])],
                          "folder": {"name": "XODUS"}}

    copy = {"name": "Copy_Raw", "type": "Copy", "dependsOn": [], "policy": {"timeout": "0.02:00:00", "retry": 0},
            "inputs": [{"referenceName": "DS_XODUS_RawFile", "type": "DatasetReference",
                        "parameters": {"container": _p("Container"), "folder": _p("Folder"), "file": _p("File")}}],
            "outputs": [{"referenceName": "DS_XODUS_Sql", "type": "DatasetReference", "parameters": {"schema": RAW, "table": _p("Table")}}],
            "typeProperties": {"source": {"type": "DelimitedTextSource", "storeSettings": {"type": "AzureBlobStorageReadSettings"}},
                               "sink": {"type": "AzureSqlSink", "preCopyScript": _e(f"@concat('TRUNCATE TABLE [{RAW}].[', pipeline().parameters.Table, ']')"),
                                        "writeBehavior": "insert"}}}
    # Data Factory does not allow additional columns on these sink datasets, so the trace columns are stamped by a procedure on the rows
    # the copy just loaded (the table was truncated before the copy, so those are exactly the rows still unstamped).
    stamp = _script("Stamp_Trace", LS_XODUS, f"EXEC [{OPS}].[usp_xodus_stamp_raw] @MigrationSlug = @slug, @Table = @table, @MigrationId = @mig, @BatchId = @batch, "
                                             "@RunId = @run, @SourceFile = @file",
                    [("slug", "String", _p("MigrationSlug")), ("table", "String", _p("Table")), ("mig", "String", _p("MigrationId")),
                     ("batch", "String", _p("BatchId")), ("run", "String", _p("RunId")),
                     ("file", "String", _e("@concat(pipeline().parameters.Container, '/', pipeline().parameters.Folder, '/', pipeline().parameters.File)"))],
                    [("Copy_Raw", "Succeeded")])
    step = _e("@concat('Load ', pipeline().parameters.File)")
    copy_steps = _guarded(copy, "PL_XODUS_Load_Raw_File", step)
    stamp_steps = _guarded(stamp, "PL_XODUS_Load_Raw_File", step, ok_rows=("@activity('Copy_Raw').output.rowsRead", "@activity('Copy_Raw').output.rowsCopied"))
    p["PL_XODUS_Load_Raw_File"] = {"description": "One source file from Blob storage into its raw table, as received (every column text), then the trace columns.",
                                   "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "RunId", "Container", "Folder", "File", "Table"),
                                   "activities": [a for a in copy_steps if a["name"] != "Log_OK_Copy_Raw"] + stamp_steps,
                                   "folder": {"name": "XODUS"}}

    canon = _script("Build_Canonical", LS_XODUS, f"EXEC [{CANON}].[usp_xodus_canon] @MigrationSlug = @slug, @MigrationId = @mig, @BatchId = @batch, @RunId = @run, @Entity = @entity",
                    [("slug", "String", _p("MigrationSlug")), ("mig", "String", _p("MigrationId")), ("batch", "String", _p("BatchId")),
                     ("run", "String", _p("RunId")), ("entity", "String", _p("Entity"))])
    p["PL_XODUS_Canonical_Entity"] = {"description": "Runs the entity's generated procedure: raw -> canonical, in one transaction.",
                                      "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "RunId", "Entity"),
                                      "activities": _guarded(canon, "PL_XODUS_Canonical_Entity", _e("@concat('Canonical ', pipeline().parameters.Entity)")),
                                      "folder": {"name": "XODUS"}}

    mirror = _script("Build_Mirror", LS_XODUS, f"EXEC [{MIRROR}].[usp_xodus_mirror] @MigrationSlug = @slug, @MigrationId = @mig, @BatchId = @batch, @RunId = @run, @Entity = @entity",
                     [("slug", "String", _p("MigrationSlug")), ("mig", "String", _p("MigrationId")), ("batch", "String", _p("BatchId")), ("run", "String", _p("RunId")),
                      ("entity", "String", _p("Entity"))])
    mirror_step = _e("@if(equals(pipeline().parameters.Entity, ''), 'Mirror', concat('Mirror ', pipeline().parameters.Entity))")
    p["PL_XODUS_Canonical_To_Mirror"] = {"description": "Canonical -> mirror of the live tables: all tables in one transaction (Entity empty) or one entity's tables.",
                                         "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "RunId", "Entity", Entity=""),
                                         "activities": _guarded(mirror, "PL_XODUS_Canonical_To_Mirror", mirror_step), "folder": {"name": "XODUS"}}

    prom_run = "@pipeline().RunId"
    gate = _lookup("Gate_Check", "usp_xodus_gate_check", {"MigrationSlug": _p("MigrationSlug"), "MigrationId": _p("MigrationId"), "BatchId": _p("BatchId")},
                   [("Log_Start", "Succeeded")], first_row=True)
    refuse = {"name": "Stop_If_Gate_Fails", "type": "IfCondition", "dependsOn": [{"activity": "Gate_Check", "dependencyConditions": ["Succeeded"]}],
              "typeProperties": {"expression": _e("@not(equals(activity('Gate_Check').output.firstRow.ok, 1))"),
                                 "ifTrueActivities": [_log("Log_Gate_Refused", "PL_XODUS_Promote", "Gate", "FAILED", [], prom_run,
                                                           message="@activity('Gate_Check').output.firstRow.detail"),
                                                      {"name": "Fail_Gate", "type": "Fail", "dependsOn": [{"activity": "Log_Gate_Refused", "dependencyConditions": ["Succeeded"]}],
                                                       "typeProperties": {"message": _e("@activity('Gate_Check').output.firstRow.detail"), "errorCode": "XODUS_GATE"}}]}}
    tables = _lookup("Get_Live_Tables", "usp_xodus_live_tables", {"MigrationSlug": _p("MigrationSlug")}, [("Stop_If_Gate_Fails", "Succeeded")])
    land = {"name": "Copy_To_Landing", "type": "Copy", "policy": {"timeout": "0.02:00:00", "retry": 0},
            "inputs": [{"referenceName": "DS_XODUS_Sql", "type": "DatasetReference", "parameters": {"schema": MIRROR, "table": _e("@item().mirror_table")}}],
            "outputs": [{"referenceName": "DS_XODUS_SqlSimplicity", "type": "DatasetReference", "parameters": {"schema": LANDING, "table": _e("@item().mirror_table")}}],
            "typeProperties": {"source": {"type": "AzureSqlSource"},
                               "sink": {"type": "AzureSqlSink", "preCopyScript": _e(f"@concat('TRUNCATE TABLE [{LANDING}].[', item().mirror_table, ']')"), "writeBehavior": "insert"}}}
    each_live = {"name": "Copy_Each_Table", "type": "ForEach", "dependsOn": [{"activity": "Get_Live_Tables", "dependencyConditions": ["Succeeded"]}],
                 "typeProperties": {"items": _e("@activity('Get_Live_Tables').output.value"), "isSequential": True, "activities": [land]}}
    promote = {"name": "Merge_Into_Live", "type": "Script", "linkedServiceName": _ref(LS_LIVE, "LinkedServiceReference"),
               "dependsOn": [{"activity": "Copy_Each_Table", "dependencyConditions": ["Succeeded"]}], "policy": {"timeout": "0.02:00:00", "retry": 0},
               "typeProperties": {"scripts": [{"type": "Query", "text": f"EXEC [{LANDING}].[usp_xodus_promote] @MigrationSlug = @slug, @BatchId = @batch",
                                               "parameters": [{"name": "slug", "type": "String", "direction": "Input", "value": _p("MigrationSlug")},
                                                              {"name": "batch", "type": "String", "direction": "Input", "value": _p("BatchId")}]}]}}
    record = _script("Record_Promotion", LS_XODUS,
                     f"INSERT INTO [{OPS}].[xodus_promotion_log] (migration_id, batch_id, run_id, status, detail) VALUES (@mig, @batch, @run, N'SUCCEEDED', @detail)",
                     [("mig", "String", _p("MigrationId")), ("batch", "String", _p("BatchId")), ("run", "String", _e(prom_run)),
                      ("detail", "String", _e("@string(activity('Merge_Into_Live').output.resultSets[0].rows)"))], [("Merge_Into_Live", "Succeeded")])
    p["PL_XODUS_Promote"] = {"description": "After sign-off: checks the promotion gate, copies the mirror to Simplicity landing and merges into the live tables in one transaction.",
                             "parameters": _params("MigrationId", "MigrationSlug", "BatchId"),
                             "activities": [_log("Log_Start", "PL_XODUS_Promote", "Pipeline", "STARTED", [], prom_run), gate, refuse, tables, each_live, promote,
                                            _log("Log_Fail_Copy", "PL_XODUS_Promote", "Pipeline", "FAILED", [("Copy_Each_Table", "Failed")], prom_run, message=_err("Copy_Each_Table")),
                                            _log("Log_Fail_Merge", "PL_XODUS_Promote", "Pipeline", "FAILED", [("Merge_Into_Live", "Failed")], prom_run, message=_err("Merge_Into_Live")),
                                            {"name": "Fail_Promotion", "type": "Fail", "dependsOn": [{"activity": "Log_Fail_Merge", "dependencyConditions": ["Succeeded"]}],
                                             "typeProperties": {"message": _e(_err("Merge_Into_Live")), "errorCode": "XODUS_PROMOTE"}},
                                            {"name": "Fail_Copy", "type": "Fail", "dependsOn": [{"activity": "Log_Fail_Copy", "dependencyConditions": ["Succeeded"]}],
                                             "typeProperties": {"message": _e(_err("Copy_Each_Table")), "errorCode": "XODUS_PROMOTE"}},
                                            record, _log("Log_Done", "PL_XODUS_Promote", "Pipeline", "SUCCEEDED", [("Record_Promotion", "Succeeded")], prom_run)],
                             "folder": {"name": "XODUS"}}

    setup_x = {"name": "Deploy_XODUS_Objects", "type": "Script", "linkedServiceName": _ref(LS_XODUS, "LinkedServiceReference"), "dependsOn": [],
               "policy": {"timeout": "0.01:00:00", "retry": 0}, "typeProperties": {"scripts": [{"type": "NonQuery", "text": b} for b in xodus_batches]}}
    setup_l = {"name": "Deploy_Simplicity_Objects", "type": "Script", "linkedServiceName": _ref(LS_LIVE, "LinkedServiceReference"),
               "dependsOn": [{"activity": "Deploy_XODUS_Objects", "dependencyConditions": ["Succeeded"]}],
               "policy": {"timeout": "0.01:00:00", "retry": 0}, "typeProperties": {"scripts": [{"type": "NonQuery", "text": b} for b in live_batches]}}
    p[f"PL_XODUS_Setup_{s}"] = {"description": f"Deploys the SQL objects for migration {s}: raw, canonical and mirror tables, the generated procedures, the load order "
                                               "and the Simplicity landing tables and promotion procedure. Live tables are never created or changed.",
                                "parameters": {}, "activities": [setup_x, setup_l], "folder": {"name": "XODUS/Setup"}}
    return p


def _datasets() -> Dict[str, Dict[str, Any]]:
    def sql(ls: str) -> Dict[str, Any]:
        return {"linkedServiceName": _ref(ls, "LinkedServiceReference"), "parameters": {"schema": {"type": "string"}, "table": {"type": "string"}},
                "type": "AzureSqlTable", "typeProperties": {"schema": _e("@dataset().schema"), "table": _e("@dataset().table")}, "folder": {"name": "XODUS"}}
    return {
        "DS_XODUS_RawFile": {"linkedServiceName": _ref(LS_BLOB, "LinkedServiceReference"),
                             "parameters": {"container": {"type": "string"}, "folder": {"type": "string"}, "file": {"type": "string"}},
                             "type": "DelimitedText", "folder": {"name": "XODUS"},
                             "typeProperties": {"location": {"type": "AzureBlobStorageLocation", "container": _e("@dataset().container"),
                                                             "folderPath": _e("@dataset().folder"), "fileName": _e("@dataset().file")},
                                                "columnDelimiter": ",", "quoteChar": "\"", "escapeChar": "\"", "firstRowAsHeader": True, "encodingName": "UTF-8"}},
        "DS_XODUS_Sql": sql(LS_XODUS),
        "DS_XODUS_SqlSimplicity": sql(LS_LIVE),
    }
