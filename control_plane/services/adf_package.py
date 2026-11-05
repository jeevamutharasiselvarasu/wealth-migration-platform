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
    managed_sql = real and env.get("sqlAuth") == "managed"  # the factory's own identity signs in to Azure SQL: no Key Vault, no secret
    if not managed_sql:
        add("linkedservices", "linkedService", ls_kv, {"type": "AzureKeyVault", "typeProperties": {"baseUrl": env["keyVaultUrl"]}})
    if real:  # Blob storage reached with the factory's managed identity: no key or secret in the package
        add("linkedservices", "linkedService", ls_blob, {"type": "AzureBlobStorage", "typeProperties": {"serviceEndpoint": env["storageBlobUrl"], "accountKind": "StorageV2"}})
    else:
        add("linkedservices", "linkedService", ls_blob, {"type": "AzureBlobFS", "typeProperties": {"url": env["storageDfsUrl"]}})
    if managed_sql:
        add("linkedservices", "linkedService", ls_sql, {"type": "AzureSqlDatabase", "typeProperties": {
            "server": env["sqlServer"], "database": env["sqlDatabase"], "authenticationType": "SystemAssignedManagedIdentity"}})
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
    raw_files = env.get("rawFiles") or entity_raw_files(env.get("rawCandidates") or {}, {t for t in sm["stagingTables"] if t.startswith("lkp_")})
    src_cols = env.get("sourceColumns") or {}

    def translator(table: str) -> Dict[str, Any]:
        """Raw files carry the source firm's headers; staging columns are canonical names. Map one to the other from the approved
        source mappings. A file read as a lookup keeps its own headers."""
        if not (real and env.get("rawFolder")) or table.startswith("lkp_"):
            return {}
        m = src_cols.get(table) or {}
        missing = [c for c in sm["stagingColumns"][table] if c not in m]
        if missing:
            raise ValueError(f"No approved source mapping supplies staging column(s) {', '.join(missing)} for {table}.")
        return {"translator": {"type": "TabularTranslator", "mappings": [
            {"source": {"name": m[c]}, "sink": {"name": c}} for c in sm["stagingColumns"][table]]}}

    for table in sm["stagingTables"]:
        _ident(table)
        folder, file_name = f"{env['stagingFolder']}/lookups/{table[4:]}" if table.startswith("lkp_") else f"{env['stagingFolder']}/{table.lower()}", None
        if real and env.get("rawFolder"):  # read the source firm's raw files in place: one file per staging table
            file_name = raw_files.get(table) or (table[4:] + ".csv" if table.startswith("lkp_") else "")
            if not file_name:
                raise ValueError(f"No raw source file is known for {table}. Classify and confirm its source file in Discovery first.")
            folder = env["rawFolder"]
        ds_blob, ds_sql = f"DS_Staging_{table}", f"DS_StagingSql_{table}"
        location = ({"type": "AzureBlobStorageLocation", "container": env["stagingContainer"], "folderPath": folder, **({"fileName": file_name} if file_name else {})} if real
                    else {"type": "AzureBlobFSLocation", "fileSystem": env["stagingContainer"], "folderPath": folder})
        add("datasets", "dataset", ds_blob, {"type": "DelimitedText", "linkedServiceName": ref(ls_blob, "LinkedServiceReference"),
            "typeProperties": {"location": location, "columnDelimiter": ",", "firstRowAsHeader": True}})
        add("datasets", "dataset", ds_sql, {"type": "AzureSqlTable", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
            "typeProperties": {"schema": sql_package.STAGING_SCHEMA, "table": table}})
        activities.append({"name": f"Copy_{table}", "type": "Copy", **({} if merge else {"dependsOn": [{"activity": "Prepare_SQL", "dependencyConditions": ["Succeeded"]}]}),
                           "inputs": [ref(ds_blob, "DatasetReference")], "outputs": [ref(ds_sql, "DatasetReference")],
                           "typeProperties": {"source": {"type": "DelimitedTextSource"}, **translator(table),
                                              "sink": {"type": "AzureSqlSink", "preCopyScript": f"TRUNCATE TABLE [{sql_package.STAGING_SCHEMA}].[{table}]"}}})
        copies.append(f"Copy_{table}")
    run_sql = ([{"type": "NonQuery", "text": st.rstrip()} for st in sql["statements"]] if merge else [{"type": "NonQuery", "text": f"EXEC {sm['procedure']};"}])
    activities.append({"name": "Run_Transformation_SQL" if not merge else "Merge_Into_Target", "type": "Script", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
                       "dependsOn": [{"activity": c, "dependencyConditions": ["Succeeded"]} for c in copies],
                       "typeProperties": {"scripts": run_sql}})

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
        "stagingTables": sm["stagingTables"], "procedure": sm["procedure"], "loadMode": "merge" if merge else "procedure",
        "provenance": provenance or {}, "deployed": False, "unresolved": env.get("unresolved", []),
        "note": ("Nothing was deployed or sent to Azure. A human must review the package and replace the simulated environment values." if not real
                 else "Nothing was deployed by XODUS. Review in Data Factory; SQL and Key Vault values listed under unresolved must be set before the pipeline can run."),
    }
    out = {p: json.dumps(c, indent=2) for p, c in files.items()}
    out.update({p: t for p, t in sql["files"].items() if p.startswith("sql/")})
    out["arm/ARMTemplateForFactory.json"] = json.dumps(arm, indent=2)
    out["manifest.json"] = json.dumps(manifest, indent=2)
    if real:
        out["README.txt"] = (env["banner"] + "\n\nGenerated from approved mappings only. The transformation is the SQL under sql/; the pipeline copies the files from\n"
                             f"{env['stagingContainer']}/{env.get('rawFolder') or env['stagingFolder']}/ into staging tables and runs it. TR_XODUS_Curated_Ready starts the pipeline when a\n"
                             f"{env['readySuffix']} file is created under {env['readyContainer']}/{env['readyFolder']}/; it is published stopped and must be started in Data Factory.\n"
                             "Blob storage is reached with the factory's managed identity (give it Storage Blob Data Contributor). No secrets are included.\n"
                             + ("\nBEFORE THE FIRST RUN, a person runs these once against the target SQL database (they are not part of the pipeline):\n"
                                "  sql/01_target_tables.sql   creates the target schema and tables\n  sql/00_staging_tables.sql  creates the staging schema and tables\n"
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
