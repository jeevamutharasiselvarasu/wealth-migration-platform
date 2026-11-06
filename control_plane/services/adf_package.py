"""Builds an Azure Data Factory package that RUNS the transformation SQL.

The transformation itself is SQL (see ``sql_package``): the pipeline copies the source files from Azure Blob storage into
staging tables, then runs the generated load procedure against the target SQL database. ADF only orchestrates; it holds no
transformation logic of its own. Azure values come from ``azure_simulated`` and nothing is deployed or called.
"""
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from control_plane.services import sql_package

IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _ident(name: str) -> str:
    if not IDENT.match(str(name or "")):
        raise ValueError(f"'{name}' is not a safe name.")
    return name


def _batches(script: str) -> List[str]:
    """Split a SQL script on GO lines: a Script activity runs batches, not GO."""
    out = [b.strip() for b in re.split(r"(?im)^\s*GO\s*$", script)]
    return [b for b in out if b and not all(l.strip().startswith("--") or not l.strip() for l in b.split("\n"))]


RUN_LOG_TABLE = "xodus_run_log"


def run_log_ddl(schema: str) -> str:
    """Idempotent create script for the run-log table the pipeline writes to (a person runs it once, like the other create scripts)."""
    t = f"[{schema}].[{RUN_LOG_TABLE}]"
    return (f"-- Run log: one row per pipeline step per run, written by the pipeline. Safe to run more than once.\n"
            f"IF SCHEMA_ID(N'{schema}') IS NULL EXEC (N'CREATE SCHEMA [{schema}]');\nGO\n"
            f"IF OBJECT_ID(N'{t}', N'U') IS NULL\nCREATE TABLE {t} (\n"
            "    log_id          bigint IDENTITY(1,1) NOT NULL CONSTRAINT PK_xodus_run_log PRIMARY KEY,\n"
            "    migration_id    nvarchar(40)   NOT NULL,\n    pipeline_run_id nvarchar(64)   NOT NULL,\n    trigger_file    nvarchar(260)  NULL,\n"
            "    step_name       nvarchar(120)  NOT NULL,\n    status          nvarchar(20)   NOT NULL,\n    rows_read       bigint         NULL,\n"
            "    rows_written    bigint         NULL,\n    message         nvarchar(4000) NULL,\n"
            "    logged_utc      datetime2(3)   NOT NULL CONSTRAINT DF_xodus_run_log_utc DEFAULT SYSUTCDATETIME()\n);\nGO\n"
            f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_xodus_run_log_run' AND object_id = OBJECT_ID(N'{t}'))\n"
            f"CREATE INDEX IX_xodus_run_log_run ON {t} (migration_id, pipeline_run_id, logged_utc);\nGO\n")


def all_or_nothing(statements: List[str]) -> str:
    """One script that runs every transformation statement in a single transaction and rolls everything back on any error."""
    body = "\n".join(st.rstrip() if st.rstrip().endswith(";") else st.rstrip() + ";" for st in statements)
    return ("SET XACT_ABORT ON;\nBEGIN TRY\n    BEGIN TRANSACTION;\n" + body +
            "\n    COMMIT TRANSACTION;\nEND TRY\nBEGIN CATCH\n    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;\n    THROW;\nEND CATCH")


def _expr(value: str) -> Dict[str, str]:
    return {"value": value, "type": "Expression"}


def _log_insert(schema: str, step: str, status: str, rows_read: Optional[str] = None, rows_written: Optional[str] = None,
                message: Optional[str] = None) -> Dict[str, Any]:
    """A Script-activity statement that appends one row to the run log. Every value is passed as a script parameter (never spliced
    into the SQL text), so a file name or an error message cannot change the statement."""
    def prm(name: str, ptype: str, value: Any) -> Dict[str, Any]:
        return {"name": name, "type": ptype, "direction": "Input", "value": value}
    params = [prm("mig", "String", _expr("@pipeline().parameters.MigrationId")), prm("run", "String", _expr("@pipeline().RunId")),
              prm("file", "String", _expr("@pipeline().parameters.ReadyFile")), prm("step", "String", step), prm("status", "String", status),
              prm("rr", "Int64", _expr(rows_read or "@null")), prm("rw", "Int64", _expr(rows_written or "@null")),
              prm("msg", "String", _expr(message or "@null"))]
    return {"type": "NonQuery", "parameters": params,
            "text": (f"INSERT INTO [{schema}].[{RUN_LOG_TABLE}] (migration_id, pipeline_run_id, trigger_file, step_name, status, rows_read, rows_written, message) "
                     "VALUES (@mig, @run, NULLIF(@file, ''), @step, @status, @rr, @rw, @msg)")}


def _error_text(activity: str) -> str:
    """ADF expression for an activity's error message, cut to fit the log column."""
    e = f"string(activity('{activity}').error.message)"
    return f"@if(greater(length({e}), 3900), substring({e}, 0, 3900), {e})"


def entity_raw_files(candidates: Dict[str, List[Optional[str]]], lookup_tables: Set[str]) -> Dict[str, str]:
    """Entity -> the raw file that feeds its staging table. Files an approved rule reads as a lookup (advisors) are not the entity's own
    file; a file named like the entity (households -> Household) wins, otherwise the first remaining file."""
    out: Dict[str, str] = {}
    for entity, names in candidates.items():
        names = [n for n in dict.fromkeys(names) if n]
        own = [n for n in names if sql_package.lookup_table(n) not in lookup_tables] or names
        named = [n for n in own if re.sub(r"\.[A-Za-z0-9]+$", "", n).lower().endswith((entity.lower(), entity.lower() + "s"))]
        if named or own:
            out[entity] = (named or own)[0]
    return out


def build(migration_id: str, spec: Dict[str, Any], approved: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
          env: Dict[str, Any], generated_by: str, provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {'files': {path: text}, 'manifest': {...}}. Raises ValueError when there is nothing safe to package."""
    sql = sql_package.build(migration_id, spec, approved, rules, env, generated_by, provenance)
    mig = _ident(re.sub(r"[^A-Za-z0-9_]", "_", migration_id))
    sm = sql["manifest"]
    files: Dict[str, Any] = {}
    resources: List[Dict[str, Any]] = []

    def add(kind: str, folder: str, name: str, props: Dict[str, Any]) -> None:
        files[f"{folder}/{name}.json"] = {"name": name, "properties": props}
        resources.append({"type": f"Microsoft.DataFactory/factories/{kind}", "apiVersion": "2018-06-01",
                          "name": f"[concat(parameters('factoryName'), '/{name}')]", "properties": props, "dependsOn": []})

    def ref(name: str, kind: str) -> Dict[str, str]:
        return {"referenceName": name, "type": kind}

    real = not env.get("simulated", True)
    sfx = "" if real else "_Sim"
    ls_kv, ls_blob, ls_sql = f"LS_KeyVault{sfx}", f"LS_ADLS_Staging{sfx}", f"LS_SQL_Target{sfx}"
    if real:
        # Real environment: the linked services are created and tested by hand in Data Factory. The package only references them
        # and never ships (or overwrites) a linked service.
        ls_blob, ls_sql = "LS_BLOB", "LS_SQL_Target"
    managed_sql = real and env.get("sqlAuth") == "managed"  # the factory's own identity signs in to Azure SQL: no Key Vault, no secret
    if real:
        pass
    elif not managed_sql:
        add("linkedservices", "linkedService", ls_kv, {"type": "AzureKeyVault", "typeProperties": {"baseUrl": env["keyVaultUrl"]}})
    if not real:
        add("linkedservices", "linkedService", ls_blob, {"type": "AzureBlobFS", "typeProperties": {"url": env["storageDfsUrl"]}})
    if real:
        pass
    else:
        add("linkedservices", "linkedService", ls_sql, {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": {
            "type": "AzureKeyVaultSecret", "store": ref(ls_kv, "LinkedServiceReference"), "secretName": env["keyVaultSqlSecretName"]}}})

    merge = real and env.get("loadMode") == "merge"  # the create scripts (schema, tables, staging) are run by hand; the pipeline only loads
    activities: List[Dict[str, Any]] = []
    if not merge:
        prepare = _batches(sql["files"]["sql/01_target_tables.sql"]) + _batches(sql["files"]["sql/00_staging_tables.sql"]) + _batches(sql["files"]["sql/usp_load_all.sql"])
        activities.append({"name": "Prepare_SQL", "type": "Script", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
                           "typeProperties": {"scripts": [{"type": "NonQuery", "text": b} for b in prepare]}})
    copies: List[str] = []
    log_schema = _ident(str(env.get("sqlSchema") or "simplicity")) if merge else ""
    ls_ref = ref(ls_sql, "LinkedServiceReference")
    if merge:
        activities.append({"name": "Log_Start", "type": "Script", "linkedServiceName": ls_ref,
                           "typeProperties": {"scripts": [_log_insert(log_schema, "Pipeline", "STARTED")]}})
    for table in sm["stagingTables"]:
        _ident(table)
        if table.startswith("lkp_"):
            folder, file_name = (f"{env['stagingFolder']}/lookups", table[4:] + ".csv") if real else (f"{env['stagingFolder']}/lookups/{table[4:]}", None)
        else:  # real run: XODUS wrote one canonical-named file per entity under stage/ (source columns already mapped to canonical)
            folder, file_name = env["stagingFolder"] if real else f"{env['stagingFolder']}/{table.lower()}", (f"{table}.csv" if real else None)
        ds_blob, ds_sql = f"DS_Staging_{table}", f"DS_StagingSql_{table}"
        location = ({"type": "AzureBlobStorageLocation", "container": env["stagingContainer"], "folderPath": folder, **({"fileName": file_name} if file_name else {})} if real
                    else {"type": "AzureBlobFSLocation", "fileSystem": env["stagingContainer"], "folderPath": folder})
        add("datasets", "dataset", ds_blob, {"type": "DelimitedText", "linkedServiceName": ref(ls_blob, "LinkedServiceReference"),
            "typeProperties": {"location": location, "columnDelimiter": ",", "firstRowAsHeader": True,
                                              # real files are standard CSV: a quote inside a quoted value is written as two quotes (the JSON in advisors.RepCodes);
                                              # Data Factory's default escape character is a backslash, which splits those rows into extra columns
                                              **({"quoteChar": "\"", "escapeChar": "\""} if real else {})}})
        add("datasets", "dataset", ds_sql, {"type": "AzureSqlTable", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
            "typeProperties": {"schema": sql_package.STAGING_SCHEMA, "table": table}})
        activities.append({"name": f"Copy_{table}", "type": "Copy",
                           **({"dependsOn": [{"activity": "Log_Start", "dependencyConditions": ["Succeeded"]}]} if merge else {"dependsOn": [{"activity": "Prepare_SQL", "dependencyConditions": ["Succeeded"]}]}),
                           "inputs": [ref(ds_blob, "DatasetReference")], "outputs": [ref(ds_sql, "DatasetReference")],
                           "typeProperties": {"source": {"type": "DelimitedTextSource"},
                                              "sink": {"type": "AzureSqlSink", "preCopyScript": f"TRUNCATE TABLE [{sql_package.STAGING_SCHEMA}].[{table}]"}}})
        copies.append(f"Copy_{table}")
        if merge:
            name = f"Copy_{table}"
            activities.append({"name": f"Log_OK_{name}", "type": "Script", "linkedServiceName": ls_ref,
                               "dependsOn": [{"activity": name, "dependencyConditions": ["Succeeded"]}],
                               "typeProperties": {"scripts": [_log_insert(log_schema, name, "SUCCEEDED", f"@activity('{name}').output.rowsRead",
                                                                          f"@activity('{name}').output.rowsCopied")]}})
            activities.append({"name": f"Log_Fail_{name}", "type": "Script", "linkedServiceName": ls_ref,
                               "dependsOn": [{"activity": name, "dependencyConditions": ["Failed"]}],
                               "typeProperties": {"scripts": [_log_insert(log_schema, name, "FAILED", message=_error_text(name))]}})
    # All or nothing: every MERGE runs in one transaction, so a failure leaves the target tables exactly as they were (the whole load stops).
    run_sql = ([{"type": "NonQuery", "text": all_or_nothing(sql["statements"])}] if merge else [{"type": "NonQuery", "text": f"EXEC {sm['procedure']};"}])
    activities.append({"name": "Run_Transformation_SQL" if not merge else "Merge_Into_Target", "type": "Script", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
                       "dependsOn": [{"activity": c, "dependencyConditions": ["Succeeded"]} for c in copies],
                       "typeProperties": {"scripts": run_sql}})

    if merge:
        merge_name = "Merge_Into_Target"
        activities.append({"name": "Log_OK_Merge", "type": "Script", "linkedServiceName": ls_ref,
                           "dependsOn": [{"activity": merge_name, "dependencyConditions": ["Succeeded"]}],
                           "typeProperties": {"scripts": [_log_insert(log_schema, "Pipeline", "SUCCEEDED")]}})
        activities.append({"name": "Log_Fail_Merge", "type": "Script", "linkedServiceName": ls_ref,
                           "dependsOn": [{"activity": merge_name, "dependencyConditions": ["Failed"]}],
                           "typeProperties": {"scripts": [_log_insert(log_schema, merge_name, "FAILED", message=_error_text(merge_name))]}})

    pipeline = f"PL_XODUS_{mig}"
    add("pipelines", "pipeline", pipeline, {"description": "Azure Blob source files to staging tables, then the transformation SQL into the target SQL database. Generated from approved mappings.",
        "parameters": {"MigrationId": {"type": "string", "defaultValue": migration_id}, **({"ReadyFile": {"type": "string", "defaultValue": ""}} if real else {})},
        "activities": activities})
    if real:  # starts the pipeline when a ready file is created under curated/ (the trigger is published stopped; the factory owner starts it)
        add("triggers", "trigger", "TR_XODUS_Curated_Ready", {
            "description": f"Starts {pipeline} when a {env['readySuffix']} file is created in {env['readyContainer']}/{env['readyFolder']}/.",
            "annotations": [], "runtimeState": "Stopped", "type": "BlobEventsTrigger",
            "pipelines": [{"pipelineReference": {"referenceName": pipeline, "type": "PipelineReference"},
                           "parameters": {"ReadyFile": "@triggerBody().fileName"}}],
            "typeProperties": {"blobPathBeginsWith": f"/{env['readyContainer']}/blobs/{env['readyFolder']}/", "blobPathEndsWith": env["readySuffix"],
                               "ignoreEmptyBlobs": False, "scope": env["storageResourceId"], "events": ["Microsoft.Storage.BlobCreated"]}})
    arm = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0",
           "metadata": {"simulated": not real, "note": env["banner"]},
           "parameters": {"factoryName": {"type": "string", "defaultValue": env["factoryName"]}}, "resources": resources}
    manifest = {
        "simulated": not real, "banner": env["banner"], "carrier": "SQL", "migrationId": migration_id, "generatedBy": generated_by,
        "generatedUtc": datetime.now(timezone.utc).isoformat(), "targetSystem": spec.get("target_system"), "targetModelVersion": spec["spec_version"],
        "pipeline": pipeline, "environment": {k: v for k, v in env.items() if k != "banner"}, "entities": sm["entities"], "warnings": sm["warnings"],
        "stagingTables": sm["stagingTables"], "stagingColumns": sm["stagingColumns"], "procedure": sm["procedure"], "loadMode": "merge" if merge else "procedure",
        "provenance": provenance or {}, "deployed": False, "unresolved": env.get("unresolved", []),
        "note": ("Nothing was deployed or sent to Azure. A human must review the package and replace the simulated environment values." if not real
                 else "Nothing was deployed by XODUS. Review in Data Factory; SQL and Key Vault values listed under unresolved must be set before the pipeline can run."),
    }
    out = {p: json.dumps(c, indent=2) for p, c in files.items()}
    out.update({p: t for p, t in sql["files"].items() if p.startswith("sql/")})
    if merge:
        out["sql/02_run_log.sql"] = run_log_ddl(log_schema)
    out["arm/ARMTemplateForFactory.json"] = json.dumps(arm, indent=2)
    out["manifest.json"] = json.dumps(manifest, indent=2)
    if real:
        out["README.txt"] = (env["banner"] + "\n\nGenerated from approved mappings only. The transformation is the SQL under sql/; the pipeline copies the files from\n"
                             f"{env['stagingContainer']}/{env['stagingFolder']}/ (canonical files written by XODUS) into staging tables and runs it. TR_XODUS_Curated_Ready starts the pipeline when a\n"
                             f"{env['readySuffix']} file is created under {env['readyContainer']}/{env['readyFolder']}/; it is published stopped and must be started in Data Factory.\n"
                             "Blob storage is reached with the factory's managed identity (give it Storage Blob Data Contributor). No secrets are included.\n"
                             + ("\nBEFORE THE FIRST RUN, a person runs these once against the target SQL database (they are not part of the pipeline):\n"
                                "  sql/01_target_tables.sql   creates the target schema and tables\n  sql/00_staging_tables.sql  creates the staging schema and tables\n  sql/02_run_log.sql         creates the run-log table the pipeline writes to\n"
                                "The pipeline then copies the raw files into staging and runs MERGE statements (update matching rows, insert new ones) into the target tables.\n"
                                if merge else ""))
    else:
        out["README_SIMULATED.txt"] = (env["banner"] + "\n\nGenerated from approved mappings only. The transformation is the SQL under sql/; the pipeline copies the Azure Blob source files\n"
                                      "into staging tables and runs it. Every Azure value (subscription, factory, storage, SQL, Key Vault) is fictitious.\n"
                                      "No secrets are included; the SQL connection string is a Key Vault secret reference.\n")
    return {"files": out, "manifest": manifest}


def to_zip(package: Dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in sorted(package["files"].items()):
            z.writestr(path, text)
    return buf.getvalue()
