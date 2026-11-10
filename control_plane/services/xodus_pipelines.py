"""Builds the XODUS data-plane package: stable, parameterised Data Factory pipelines plus the SQL they run.

Layers (all in the XODUS database unless noted; every table is per migration, named <slug>_<name>):
  xodus_raw     the source files exactly as received (every column text, plus trace columns)       <- PL_XODUS_Load_Raw_File
  xodus_canon   one table per entity in the canonical model, filled by usp_canon_<slug>_<Entity>     <- PL_XODUS_Canonical_Entity
  xodus_mirror  a mirror of each live target table, filled by usp_mirror_<slug>                      <- PL_XODUS_Canonical_To_Mirror
  xodus_ops     version, load order, run log, pipeline runs, table counts, promotion gate and log
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

from control_plane.services import sql_package
from control_plane.services.sql_package import _lit, _q, translate

RAW, CANON, MIRROR, OPS, LANDING = "xodus_raw", "xodus_canon", "xodus_mirror", "xodus_ops", "xodus_landing"
LS_BLOB, LS_XODUS, LS_LIVE = "LS_BLOB", "LS_SQL_Target", "LS_SQL_Simplicity"
STABLE_PIPELINES = ("PL_XODUS_Run", "PL_XODUS_Source_To_Canonical", "PL_XODUS_Load_Raw_File", "PL_XODUS_Canonical_Entity",
                    "PL_XODUS_Canonical_To_Mirror", "PL_XODUS_Promote")
HEADER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .\-]{0,127}$")
TRACE = ("[xodus_migration_id] nvarchar(64) NULL", "[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL",
         "[xodus_source_file] nvarchar(1024) NULL", "[xodus_loaded_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()")


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


def _crosswalk_case(col: str, table: Dict[str, str]) -> str:
    norm = f"UPPER(LTRIM(RTRIM({col})))"
    whens = " ".join(f"WHEN {_lit(k)} THEN {_lit(v)}" for k, v in sorted(table.items()))
    return f"CASE {norm} {whens} ELSE {norm} END" if whens else norm


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
    if fam == "CLIENT_TYPE_LOOKUP":
        return _crosswalk_case(col, A.CLIENT_TYPE_CROSSWALK)
    if fam == "CUSTODIAN_LOOKUP":
        return _crosswalk_case(col, A.COMMON_CUSTODIAN_CROSSWALK)
    if fam == "REG_TYPE_LOOKUP":
        return _crosswalk_case(col, A.REGISTRATION_TYPE_CROSSWALK)
    if fam == "PREFIX_FORMAT":
        return f"CASE WHEN {col} IS NULL THEN NULL WHEN {col} LIKE N'REP-%' THEN {col} ELSE N'REP-' + {col} END"
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
    b = [_schema(s) for s in (RAW, CANON, MIRROR, OPS)]
    tables = {
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
    for name, cols in (("usp_xodus_raw_files", "container, folder, file_name, raw_table"), ("usp_xodus_load_order", "entity"),
                       ("usp_xodus_live_tables", "mirror_table, live_table")):
        table = {"usp_xodus_raw_files": "xodus_raw_files", "usp_xodus_load_order": "xodus_load_order", "usp_xodus_live_tables": "xodus_live_tables"}[name]
        order = " ORDER BY seq" if table != "xodus_live_tables" else " ORDER BY mirror_table"
        b.append(f"CREATE OR ALTER PROCEDURE [{OPS}].[{name}] @MigrationSlug nvarchar(32)\nAS\nBEGIN\n    SET NOCOUNT ON;\n"
                 f"    SELECT {cols} FROM [{OPS}].[{table}] WHERE migration_slug = @MigrationSlug{order};\nEND")
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
        entity_param = ", @Entity sysname" if is_canon else ""
        entity_check = " OR @Entity LIKE N'%[^A-Za-z0-9_]%'" if is_canon else ""
        entity_name = " + N'_' + @Entity" if is_canon else ""
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


def build(migration_id: str, spec: Dict[str, Any], approved_target: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
          entities: Dict[str, Dict[str, Any]], raw_files: List[Dict[str, Any]], load_order: List[str], env: Dict[str, Any],
          generated_by: str, provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """entities[E] = {"files": [file_name, ...], "mappings": {source_field: {"canonical_field", "expression"}}, "key": canonical key or None,
                      "derivations": [{"canonical_field", "input_field", "rule_expression"}], "canonical_fields": [...]}
    raw_files = [{"file_name", "columns": [...]}] (entity files and the lookup files their rules read).
    env = {"container", "rawFolder", "liveSchema"}. Returns {"files": {path: text}, "manifest": {...}, "version": str}."""
    s = slug(migration_id)
    live_schema = sql_package._ident(env.get("liveSchema") or "simplicity")
    approved_target = [m for m in approved_target if m.get("status") == "APPROVED"]
    if not spec or not approved_target:
        raise ValueError("Approve at least one target mapping before building the pipelines.")
    columns_of = {f["file_name"]: list(f["columns"]) for f in raw_files}
    raw_names = {f["file_name"]: raw_table(s, f["file_name"]) for f in raw_files}

    def lookup_ref(parent_file: str) -> str:
        if parent_file not in raw_names:
            raise ValueError(f"A lookup rule reads {parent_file}, which is not among the migration's source files.")
        return f"[{RAW}].[{raw_names[parent_file]}]"

    per_migration: List[str] = []
    objects = {"tables": [], "procedures": []}
    for f in raw_files:
        cols = [f"{_header(c)} nvarchar(4000) NULL" for c in f["columns"]] + list(TRACE)
        per_migration.append(_drop_create(RAW, raw_names[f["file_name"]], cols))
        objects["tables"].append(f"{RAW}.{raw_names[f['file_name']]}")

    # Source -> canonical: one table and one procedure per entity.
    canon_counts: Dict[str, Dict[str, Any]] = {}
    for entity in load_order:
        e = entities[entity]
        sql_package._ident(entity)
        ctable = f"{s}_{entity}"
        cfields = list(dict.fromkeys(e["canonical_fields"]))
        per_migration.append(_drop_create(CANON, ctable, [f"{_q(c)} nvarchar(4000) NULL" for c in cfields]
                                          + ["[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL", "[xodus_loaded_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME()"]))
        objects["tables"].append(f"{CANON}.{ctable}")
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
            per_file.append(exprs)
        mapped = [c for c in cfields if any(c in p for p in per_file)]
        key = e.get("key") if e.get("key") in mapped else None
        selects = []
        for i, (fn, exprs) in enumerate(zip(files, per_file)):
            body = ", ".join(f"{exprs[c]} AS {_q(c)}" for c in mapped if c in exprs)
            part = f"ROW_NUMBER() OVER (PARTITION BY {exprs[key]} ORDER BY (SELECT NULL)) AS [xodus_rn]" if key and key in exprs else "1 AS [xodus_rn]"
            selects.append(f"f{i} AS (SELECT {body}, {part} FROM [{RAW}].[{raw_names[fn]}] AS s WHERE s.[xodus_batch_id] = @BatchId)")
        if len(files) == 1:
            exprs = per_file[0]
            pick = f"WHERE (f0.{_q(key)} IS NULL OR f0.[xodus_rn] = 1)" if key and key in exprs else ""
            select = (f"WITH {selects[0]}\nINSERT INTO [{CANON}].[{ctable}] ({', '.join(_q(c) for c in mapped)}, [xodus_batch_id], [xodus_run_id])\n"
                      f"SELECT {', '.join('f0.' + _q(c) for c in mapped if c in exprs)}, @BatchId, @RunId FROM f0 {pick};")
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
                "SELECT " + ", ".join((f"f{i}.{_q(c)}" if c in per_file[i] else "NULL") for c in mapped) + f", @BatchId, @RunId FROM f{i} WHERE f{i}.{_q(key)} IS NULL"
                for i in keyed)
            select = (f"WITH {', '.join(selects)}, k AS ({keys})\nINSERT INTO [{CANON}].[{ctable}] ({', '.join(_q(c) for c in mapped)}, [xodus_batch_id], [xodus_run_id])\n"
                      f"SELECT {', '.join(cols)}, @BatchId, @RunId FROM k {joins}\nUNION ALL {unkeyed};")
        derive = []
        for d in e.get("derivations", []):
            rule = rules.get(str(d["rule_expression"]).strip().upper())
            if not rule:
                raise ValueError(f"The derivation of {entity}.{d['canonical_field']} pins {d['rule_expression']}, which is not approved.")
            if d["canonical_field"] not in cfields:
                continue
            derive.append(f"UPDATE c SET c.{_q(d['canonical_field'])} = {translate(d['rule_expression'], d['input_field'], rule, 'c', lookup_ref)} "
                          f"FROM [{CANON}].[{ctable}] AS c;")
        log = f"EXEC [{OPS}].[usp_xodus_count] @MigrationId, @RunId, @BatchId, N'canonical', N'{CANON}', N'{ctable}';"
        per_migration.append(_proc(f"[{CANON}].[usp_canon_{s}_{entity}]", "@MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64)",
                                   [f"TRUNCATE TABLE [{CANON}].[{ctable}];", select] + derive + [log]))
        objects["procedures"].append(f"{CANON}.usp_canon_{s}_{entity}")
        canon_counts[entity] = {"table": ctable, "files": [raw_names[f] for f in files], "key": key}

    # Canonical -> mirror: one mirror table per live table, filled by one procedure (all tables in one transaction).
    mirror_body: List[str] = []
    live_tables: List[Dict[str, Any]] = []
    for entity in sorted({m["entity_name"] for m in approved_target}):
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
                                              + ["[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL"]))
            objects["tables"].append(f"{MIRROR}.{mtable}")
            exprs, cols, keys = [], [], []
            for m in trows:
                col = sql_package._ident(fields[m["target_field"]]["column"])
                rule = rules.get(m["transformation_expression"])
                e = translate(m["transformation_expression"], m["canonical_field"], rule, "s", lookup_ref)
                exprs.append(f"{e} AS {_q(col)}")
                cols.append(col)
                if sql_package._is_key_field(m["canonical_field"]):
                    keys.append(e)
            dedupe = (f"ROW_NUMBER() OVER (PARTITION BY {', '.join(keys)} ORDER BY (SELECT NULL)) AS [xodus_rn]" if keys else "1 AS [xodus_rn]")
            mirror_body.append(f"TRUNCATE TABLE [{MIRROR}].[{mtable}];")
            mirror_body.append(
                f"INSERT INTO [{MIRROR}].[{mtable}] ({', '.join(_q(c) for c in cols)}, [xodus_batch_id], [xodus_run_id])\n"
                f"SELECT {'' if keys else 'DISTINCT '}{', '.join('x.' + _q(c) for c in cols)}, @BatchId, @RunId FROM (\n"
                f"    SELECT {', '.join(exprs)}, {dedupe}\n    FROM [{CANON}].[{canon_counts[entity]['table']}] AS s\n) AS x"
                + (" WHERE x.[xodus_rn] = 1;" if keys else ";"))
            mirror_body.append(f"EXEC [{OPS}].[usp_xodus_count] @MigrationId, @RunId, @BatchId, N'mirror', N'{MIRROR}', N'{mtable}';")
            key_cols = [c for c, m in zip(cols, trows) if sql_package._is_key_field(m["canonical_field"])]
            types = {c: t for c, t in all_cols}
            live_tables.append({"entity": entity, "live_table": table, "mirror_table": mtable, "columns": cols, "key_columns": key_cols,
                                "all_columns": [c for c, _ in all_cols], "types": {c: types.get(c, "string") for c in cols}})
    if not live_tables:
        raise ValueError("No approved target mapping has a canonical source, so there is nothing to load.")
    per_migration.append(_proc(f"[{MIRROR}].[usp_mirror_{s}]", "@MigrationId nvarchar(64), @BatchId nvarchar(64), @RunId nvarchar(64)", mirror_body))
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
        cols = [f"{_q(c)} nvarchar(4000) NULL" for c in t["all_columns"]] + ["[xodus_batch_id] nvarchar(64) NULL", "[xodus_run_id] nvarchar(64) NULL"]
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
        "rawFolder": folder, "container": container, "loadOrder": load_order, "canonical": canon_counts, "liveTables": live_tables,
        "objects": objects, "provenance": provenance or {}, "deployed": False,
    }
    out["manifest.json"] = json.dumps(manifest, indent=2)
    out["README.txt"] = (
        f"XODUS data-plane package for {migration_id} (version {version}). Nothing was executed or deployed by XODUS.\n\n"
        "Pipelines (stable; they take the migration and batch as parameters):\n"
        "  PL_XODUS_Run                 raw files -> canonical -> mirror for one batch (started by XODUS through the service principal)\n"
        "  PL_XODUS_Source_To_Canonical loads every raw file, then builds each entity in the load order\n"
        "  PL_XODUS_Load_Raw_File       one source file from Blob storage into its xodus_raw table, as received\n"
        "  PL_XODUS_Canonical_Entity    runs the entity's generated procedure (raw -> canonical)\n"
        "  PL_XODUS_Canonical_To_Mirror canonical -> mirror of the live tables, one transaction\n"
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


def _params(*names: str) -> Dict[str, Any]:
    return {n: {"type": "string"} for n in names}


def _exec(name: str, pipeline: str, params: Dict[str, Any], depends: Optional[List[tuple]] = None) -> Dict[str, Any]:
    return {"name": name, "type": "ExecutePipeline", "dependsOn": [{"activity": a, "dependencyConditions": [c]} for a, c in (depends or [])],
            "typeProperties": {"pipeline": _ref(pipeline, "PipelineReference"), "waitOnCompletion": True, "parameters": params}}


def _lookup(name: str, proc: str, params: Dict[str, Any], depends: Optional[List[tuple]] = None, first_row: bool = False) -> Dict[str, Any]:
    return {"name": name, "type": "Lookup", "dependsOn": [{"activity": a, "dependencyConditions": [c]} for a, c in (depends or [])],
            "policy": {"timeout": "0.00:30:00", "retry": 0},
            "typeProperties": {"source": {"type": "AzureSqlSource", "sqlReaderStoredProcedureName": f"[{OPS}].[{proc}]",
                                          "storedProcedureParameters": {k: {"type": "String", "value": v} for k, v in params.items()}},
                               "dataset": {"referenceName": "DS_XODUS_Sql", "type": "DatasetReference", "parameters": {"schema": OPS, "table": "xodus_run_log"}},
                               "firstRowOnly": first_row}}


def _pipelines(s: str, xodus_batches: List[str], live_batches: List[str]) -> Dict[str, Dict[str, Any]]:
    run_params = {"MigrationId": _p("MigrationId"), "MigrationSlug": _p("MigrationSlug"), "BatchId": _p("BatchId")}
    child = {**run_params, "RunId": _e("@pipeline().RunId")}
    pass_run = {**run_params, "RunId": _p("RunId")}
    p: Dict[str, Dict[str, Any]] = {}

    start = _log("Log_Start", "PL_XODUS_Run", "Pipeline", "STARTED", [], "@pipeline().RunId")
    s2c = _exec("Run_Source_To_Canonical", "PL_XODUS_Source_To_Canonical", child, [("Log_Start", "Succeeded")])
    c2m = _exec("Run_Canonical_To_Mirror", "PL_XODUS_Canonical_To_Mirror", child, [("Run_Source_To_Canonical", "Succeeded")])
    acts = [start, *_guarded(s2c, "PL_XODUS_Run", "Source_To_Canonical", "@pipeline().RunId"),
            *_guarded(c2m, "PL_XODUS_Run", "Canonical_To_Mirror", "@pipeline().RunId"),
            _log("Log_Done", "PL_XODUS_Run", "Pipeline", "SUCCEEDED", [("Log_OK_Run_Canonical_To_Mirror", "Succeeded")], "@pipeline().RunId")]
    for a in acts:  # a failed step also closes the run row as FAILED
        if a["name"].startswith("Log_Fail_"):
            a["typeProperties"]["scripts"][0]["parameters"][4]["value"] = "Pipeline"
    p["PL_XODUS_Run"] = {"description": "One batch: source files -> canonical -> mirror. Started by XODUS; never touches the live tables.",
                         "parameters": _params("MigrationId", "MigrationSlug", "BatchId"), "activities": acts, "folder": {"name": "XODUS"}}

    files = _lookup("Get_Raw_Files", "usp_xodus_raw_files", {"MigrationSlug": _p("MigrationSlug")})
    each_file = {"name": "Load_Each_Raw_File", "type": "ForEach", "dependsOn": [{"activity": "Get_Raw_Files", "dependencyConditions": ["Succeeded"]}],
                 "typeProperties": {"items": _e("@activity('Get_Raw_Files').output.value"), "isSequential": False, "batchCount": 4,
                                    "activities": [_exec("Load_Raw_File", "PL_XODUS_Load_Raw_File", {**pass_run, "Container": _e("@item().container"),
                                                         "Folder": _e("@item().folder"), "File": _e("@item().file_name"), "Table": _e("@item().raw_table")})]}}
    order = _lookup("Get_Load_Order", "usp_xodus_load_order", {"MigrationSlug": _p("MigrationSlug")}, [("Load_Each_Raw_File", "Succeeded")])
    each_entity = {"name": "Build_Each_Entity", "type": "ForEach", "dependsOn": [{"activity": "Get_Load_Order", "dependencyConditions": ["Succeeded"]}],
                   "typeProperties": {"items": _e("@activity('Get_Load_Order').output.value"), "isSequential": True,
                                      "activities": [_exec("Build_Entity", "PL_XODUS_Canonical_Entity", {**pass_run, "Entity": _e("@item().entity")})]}}
    p["PL_XODUS_Source_To_Canonical"] = {"description": "Loads every raw file of the migration, then builds each entity in the load order.",
                                         "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "RunId"),
                                         "activities": [files, each_file, order, each_entity], "folder": {"name": "XODUS"}}

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

    mirror = _script("Build_Mirror", LS_XODUS, f"EXEC [{MIRROR}].[usp_xodus_mirror] @MigrationSlug = @slug, @MigrationId = @mig, @BatchId = @batch, @RunId = @run",
                     [("slug", "String", _p("MigrationSlug")), ("mig", "String", _p("MigrationId")), ("batch", "String", _p("BatchId")), ("run", "String", _p("RunId"))])
    p["PL_XODUS_Canonical_To_Mirror"] = {"description": "Canonical -> mirror of the live tables, all tables in one transaction.",
                                         "parameters": _params("MigrationId", "MigrationSlug", "BatchId", "RunId"),
                                         "activities": _guarded(mirror, "PL_XODUS_Canonical_To_Mirror", "Mirror"), "folder": {"name": "XODUS"}}

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
