"""Builds the transformation SQL from APPROVED canonical-to-target mappings.

Deterministic and declarative: every approved mapping becomes one T-SQL select expression. Only DIRECT_COPY and
approved, registered transformation rules are translated; anything else is refused and no free-form SQL is ever
emitted. The data plane reads the source files from Azure Blob storage into ``staging`` tables (a Copy activity), runs
these statements, and fills the target SQL database. Nothing is executed or deployed here.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from control_plane.services import transformation_rules as tr

IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
STAGING_SCHEMA = "staging"
# Rule input-format tokens to SQL Server CONVERT styles. DD-MON-YYYY is read as style 106 after replacing '-' by ' '.
DATE_STYLE = {"MM/DD/YYYY": 101, "DD/MM/YYYY": 103, "YYYY-MM-DD": 23, "MM-DD-YYYY": 110, "DD-MON-YYYY": 106, "YYYYMMDD": 112, "MM/DD/YY": 1}


def _lit(text: Any) -> str:
    """T-SQL unicode string literal."""
    return "N'" + str(text).replace("'", "''").replace("\n", " ").replace("\r", " ").replace("\x00", "") + "'"


def _ident(name: Any) -> str:
    if not IDENT.match(str(name or "")):
        raise ValueError(f"'{name}' is not a safe column name.")
    return str(name)


def _q(name: str) -> str:
    return f"[{_ident(name)}]"


def _stem(file_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", re.sub(r"\.[A-Za-z0-9]+$", "", str(file_name)))


def lookup_table(parent_file: str) -> str:
    return "lkp_" + _stem(parent_file)


def columns_used(expression: str, column: str, rule: Optional[Dict[str, Any]]) -> Set[str]:
    """Staging columns an approved mapping reads."""
    cols = {_ident(column)} if column else set()
    if rule and rule.get("rule_type") == "CONCAT":
        cols |= {_ident(s) for s in tr.validate_params("CONCAT", rule["params"])["sources"]}
    return cols


def translate(expression: str, column: str, rule: Optional[Dict[str, Any]], alias: str = "s") -> str:
    """T-SQL expression for one approved mapping. Raises ValueError for anything unregistered."""
    a = _ident(alias)
    col = f"{a}.{_q(column)}"
    if expression == "DIRECT_COPY":
        return col
    if not rule or rule.get("status") != "APPROVED":
        raise ValueError(f"'{expression}' is not an approved, registered transformation.")
    p = tr.validate_params(rule["rule_type"], rule["params"])
    kind = rule["rule_type"]
    t = f"LTRIM(RTRIM({col}))"
    blank = f"NULLIF({t}, N'') IS NULL"
    if kind == "LOOKUP":
        ret = re.sub(r"[^A-Za-z0-9_]", "_", p["return_column"])
        key = re.sub(r"[^A-Za-z0-9_]", "_", p["parent_key"])
        return (f"CASE WHEN {blank} THEN {col} ELSE (SELECT TOP (1) CAST(l.{_q(ret)} AS nvarchar(4000)) "
                f"FROM {_q(STAGING_SCHEMA)}.{_q(lookup_table(p['parent_file']))} AS l WHERE LTRIM(RTRIM(l.{_q(key)})) = {t}) END")
    if kind == "DEFAULT_IF_BLANK":
        return f"CASE WHEN {blank} THEN {_lit(p['default'])} ELSE {col} END"
    if kind == "IF_BLANK":
        return f"CASE WHEN {blank} THEN {_lit(p['when_blank'])} ELSE {_lit(p['when_filled'])} END"
    if kind == "CONCAT":
        return f"CONCAT_WS({_lit(p['separator'])}, " + ", ".join(f"LTRIM(RTRIM({a}.{_q(s)}))" for s in p["sources"]) + ")"
    if kind == "VALUE_MAP":
        key = f"UPPER({t})" if p["case_insensitive"] else t
        whens = " ".join(f"WHEN {_lit(str(k).upper() if p['case_insensitive'] else k)} THEN {_lit(v)}" for k, v in p["map"].items())
        body = f"CASE {key} {whens} ELSE {'NULL' if p.get('strict') else col} END"
    elif kind == "DATE_FORMAT":
        parsed = []
        for f in p["input_formats"]:
            src = f"REPLACE({t}, N'-', N' ')" if f == "DD-MON-YYYY" else t
            parsed.append(f"TRY_CONVERT(date, {src}, {DATE_STYLE[f]})")
        body = f"CONVERT(char(10), COALESCE({', '.join(parsed)}), 23)"
    elif kind == "NUMBER_CLEAN":
        def stripped(e: str) -> str:
            for ch in p["strip_chars"]:
                e = f"REPLACE({e}, {_lit(ch)}, N'')"
            return e
        dec = f"decimal(38, {int(p['decimals'])})"
        plain = f"TRY_CONVERT({dec}, {stripped(t)})"
        if p["parentheses_negative"]:
            inner = f"SUBSTRING({t}, 2, LEN({t}) - 2)"
            body = f"CONVERT(varchar(64), CASE WHEN {t} LIKE N'(%)' THEN -TRY_CONVERT({dec}, {stripped(inner)}) ELSE {plain} END)"
        else:
            body = f"CONVERT(varchar(64), {plain})"
    elif kind == "SPLIT_PART":
        sep = "NCHAR(31)"
        body = (f"LTRIM(RTRIM((SELECT x.[value] FROM STRING_SPLIT(REPLACE({col}, {_lit(p['delimiter'])}, {sep}), {sep}, 1) AS x "
                f"WHERE x.[ordinal] = {int(p['index']) + 1})))")
    elif kind == "IF_EQUALS":
        other = col if p["else"] is None else _lit(p["else"])
        body = f"CASE WHEN {t} = {_lit(p['when_equals'])} THEN {_lit(p['then'])} ELSE {other} END"
    else:
        raise ValueError(f"Unsupported rule type '{kind}'.")
    return f"CASE WHEN {blank} THEN {col} ELSE {body} END"


KEY_FIELDS = {"ACCT_NUM", "ACCOUNT_ID", "HH_ID", "CLIENT_ID", "CONTACT_ID"}  # identifiers of the thing a row represents (not TAX_ID, ADVISOR_ID, ...)


def _is_key_field(canonical: str) -> bool:
    return (canonical or "").upper() in KEY_FIELDS


def _merge(schema: str, entity: str, table: str, fields: Dict[str, Dict[str, Any]], mappings: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """MERGE (update matching rows, insert new ones) keyed on the table's approved ID-like mapped columns. One source row per key is used."""
    built = _insert(schema, entity, table, fields, mappings, rules)
    rows = sorted(mappings, key=lambda r: r["target_field"])
    cols = [_ident(fields[m["target_field"]]["column"]) for m in rows]
    keys = [c for c, m in zip(cols, rows) if _is_key_field(m["canonical_field"] or "")]
    insert_only = not keys  # no identifier is mapped: add rows that are not already there, never update
    if insert_only:
        keys = list(cols)
    exprs = [e.rsplit(" AS ", 1)[0] for e in built["exprs"]]
    aliased = built["exprs"]
    sel = ",\n        ".join(aliased)
    others = [c for c in cols if c not in keys]
    mode = "MERGE on " + ", ".join(keys) if not insert_only else "insert-if-missing on all mapped columns (no identifier is mapped; nothing is updated)"
    sql = (f"-- {entity} -> {schema}.{table}: {mode} ({len(mappings)} approved mapping(s))\n"
           f"MERGE {_q(schema)}.{_q(table)} AS t\nUSING (\n    SELECT {', '.join('x.' + _q(c) for c in cols)} FROM (\n"
           f"        SELECT\n        {sel},\n        ROW_NUMBER() OVER (PARTITION BY {', '.join('(' + e + ')' for e, c in zip(exprs, cols) if c in keys)} ORDER BY (SELECT NULL)) AS rn\n"
           f"        FROM {_q(STAGING_SCHEMA)}.{_q(entity)} AS s\n    ) AS x WHERE x.rn = 1\n) AS src\n"
           f"ON " + " AND ".join((f"(t.{_q(k)} = src.{_q(k)} OR (t.{_q(k)} IS NULL AND src.{_q(k)} IS NULL))" if insert_only else f"t.{_q(k)} = src.{_q(k)}") for k in keys) + "\n"
           + (f"WHEN MATCHED THEN UPDATE SET " + ", ".join(f"t.{_q(c)} = src.{_q(c)}" for c in others) + "\n" if others and not insert_only else "")
           + f"WHEN NOT MATCHED BY TARGET THEN INSERT ({', '.join(_q(c) for c in cols)})\n    VALUES ({', '.join('src.' + _q(c) for c in cols)});\n")
    return {**built, "sql": sql}


def _insert(schema: str, entity: str, table: str, fields: Dict[str, Dict[str, Any]], mappings: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    targets: List[str] = []
    exprs: List[str] = []
    cols: Set[str] = set()
    lookups: Dict[str, Set[str]] = {}
    for m in sorted(mappings, key=lambda r: r["target_field"]):
        tgt, src = _ident(fields[m["target_field"]]["column"]), _ident(m["canonical_field"] or "")
        rule = rules.get(m["transformation_expression"])
        exprs.append(f"{translate(m['transformation_expression'], src, rule)} AS {_q(tgt)}")
        targets.append(_q(tgt))
        cols |= columns_used(m["transformation_expression"], src, rule)
        if rule and rule.get("rule_type") == "LOOKUP":
            p = tr.validate_params("LOOKUP", rule["params"])
            lookups.setdefault(lookup_table(p["parent_file"]), set()).update(
                {re.sub(r"[^A-Za-z0-9_]", "_", p["parent_key"]), re.sub(r"[^A-Za-z0-9_]", "_", p["return_column"])})
    sql = (f"-- {entity} -> {schema}.{table}: {len(mappings)} approved mapping(s)\n"
           f"INSERT INTO {_q(schema)}.{_q(table)} ({', '.join(targets)})\nSELECT\n    " + ",\n    ".join(exprs) +
           f"\nFROM {_q(STAGING_SCHEMA)}.{_q(entity)} AS s;\n")
    return {"sql": sql, "exprs": exprs, "columns": cols, "lookups": lookups, "target_columns": sorted(t.strip("[]") for t in targets)}


_SQL_TYPES = {"date": "date", "datetime": "datetime2", "timestamp": "datetime2", "int": "int", "integer": "int", "bigint": "bigint",
              "decimal": "decimal(28,8)", "number": "decimal(28,8)", "numeric": "decimal(28,8)", "float": "float", "boolean": "bit", "bool": "bit"}


def sql_type(model_type: Any) -> str:
    """T-SQL column type for a target field type; anything not recognised is stored as text rather than guessed."""
    return _SQL_TYPES.get(str(model_type or "").strip().lower(), "nvarchar(4000)")


def build(migration_id: str, spec: Dict[str, Any], approved: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
          env: Dict[str, Any], generated_by: str, provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {'files': {path: text}, 'manifest': {...}}. Raises ValueError when there is nothing safe to generate."""
    approved = [m for m in approved if m.get("status") == "APPROVED"]
    if not spec or not approved:
        raise ValueError("Approve at least one target mapping before generating the transformation SQL.")
    schema = _ident(env.get("sqlSchema") or "dbo")
    files: Dict[str, str] = {}
    entities: Dict[str, Any] = {}
    warnings: List[str] = []
    staging: Dict[str, Set[str]] = {}
    lookup_cols: Dict[str, Set[str]] = {}
    statements: List[str] = []
    for entity in sorted({m["entity_name"] for m in approved}):
        body = spec["entities"].get(entity)
        if not body:
            raise ValueError(f"Approved mappings reference {entity}, which is not in the confirmed target model.")
        _ident(entity)
        rows = [m for m in approved if m["entity_name"] == entity]
        fields = {f["name"]: f for f in body["fields"]}
        unknown = [m["target_field"] for m in rows if m["target_field"] not in fields]
        if unknown:
            raise ValueError(f"Approved mapping(s) for {entity} are not in the confirmed target model: {', '.join(unknown)}")
        mapped = {m["target_field"] for m in rows}
        req_missing = [n for n, f in fields.items() if f.get("required") and n not in mapped]
        if req_missing:
            warnings.append(f"{entity}: required target field(s) without an approved mapping: {', '.join(req_missing)}")
        tables: Dict[str, Any] = {}
        for table in sorted({fields[m["target_field"]]["table"] for m in rows}):
            _ident(table)
            trows = [m for m in rows if fields[m["target_field"]]["table"] == table]
            built = (_merge if env.get("loadMode") == "merge" else _insert)(schema, entity, table, fields, trows, rules)
            files[f"sql/{entity}_{table}.sql"] = built["sql"]
            statements.append(built["sql"])
            staging.setdefault(entity, set()).update(built["columns"])
            for lk, cs in built["lookups"].items():
                lookup_cols.setdefault(lk, set()).update(cs)
            tables[table] = {"approvedMappings": len(trows), "columns": built["target_columns"]}
        entities[entity] = {"approvedMappings": len(rows), "tables": tables,
                            "uncoveredFields": [n for n in fields if n not in mapped],
                            "reviewers": sorted({m.get("reviewer") or "" for m in rows} - {""})}

    tgt = [f"IF SCHEMA_ID(N'{schema}') IS NULL EXEC(N'CREATE SCHEMA {_q(schema)}');\nGO\n"]
    for entity in sorted(entities):
        flds = spec["entities"][entity]["fields"]
        for table in sorted({_ident(f["table"]) for f in flds}):
            cols = sorted({(_ident(f["column"]), sql_type(f.get("type"))) for f in flds if f["table"] == table})
            tgt.append(f"IF OBJECT_ID(N'{schema}.{table}', N'U') IS NULL\n    CREATE TABLE {_q(schema)}.{_q(table)} (\n        "
                       + ",\n        ".join(f"{_q(c)} {t} NULL" for c, t in cols) + "\n    );\nGO\n")
    files["sql/01_target_tables.sql"] = "-- Creates the target schema and tables when they do not exist yet. Never drops or alters anything.\n" + "\n".join(tgt)
    ddl = [f"IF SCHEMA_ID(N'{STAGING_SCHEMA}') IS NULL EXEC(N'CREATE SCHEMA {_q(STAGING_SCHEMA)}');\nGO\n"]
    for name, cols in sorted({**staging, **lookup_cols}.items()):
        ddl.append(f"IF OBJECT_ID(N'{STAGING_SCHEMA}.{name}', N'U') IS NULL\n    CREATE TABLE {_q(STAGING_SCHEMA)}.{_q(name)} (\n        "
                   + ",\n        ".join(f"{_q(c)} nvarchar(4000) NULL" for c in sorted(cols)) + "\n    );\nGO\n")
    files["sql/00_staging_tables.sql"] = ("-- Staging tables the pipeline fills from the Azure Blob source files (every column as text).\n" + "\n".join(ddl))
    proc = _q(schema) + "." + _q("usp_xodus_load_all")
    if env.get("loadMode") == "merge":  # the pipeline runs the MERGE statements itself: no procedure to create
        proc = ""
    else:
        files["sql/usp_load_all.sql"] = (
            f"-- Runs every transformation statement in one transaction. Generated from approved mappings only.\n"
            f"CREATE OR ALTER PROCEDURE {proc}\nAS\nBEGIN\n    SET NOCOUNT ON;\n    SET XACT_ABORT ON;\n    BEGIN TRY\n        BEGIN TRANSACTION;\n"
            + "\n".join("        " + line if line else line for st in statements for line in (st.rstrip("\n").split("\n") + [""]))
            + "\n        COMMIT TRANSACTION;\n    END TRY\n    BEGIN CATCH\n        IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;\n        THROW;\n    END CATCH\nEND\nGO\n")
    manifest = {
        "carrier": "SQL", "migrationId": migration_id, "generatedBy": generated_by, "generatedUtc": datetime.now(timezone.utc).isoformat(),
        "targetSystem": spec.get("target_system"), "targetModelVersion": spec.get("spec_version"), "targetSchema": schema,
        "stagingSchema": STAGING_SCHEMA, "stagingTables": sorted({**staging, **lookup_cols}),
        "stagingColumns": {n: sorted(c) for n, c in {**staging, **lookup_cols}.items()}, "procedure": proc,
        "entities": entities, "warnings": warnings, "provenance": provenance or {}, "executed": False,
        "note": "Nothing was executed. The pipeline copies the Azure Blob source files into the staging tables, then runs these statements against the target SQL database.",
    }
    files["manifest.json"] = json.dumps(manifest, indent=2)
    return {"files": files, "manifest": manifest, "statements": statements}
