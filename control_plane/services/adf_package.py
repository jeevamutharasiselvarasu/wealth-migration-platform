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
from typing import Any, Dict, List, Optional

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
    add("linkedservices", "linkedService", ls_kv, {"type": "AzureKeyVault", "typeProperties": {"baseUrl": env["keyVaultUrl"]}})
    if real:  # Blob storage reached with the factory's managed identity: no key or secret in the package
        add("linkedservices", "linkedService", ls_blob, {"type": "AzureBlobStorage", "typeProperties": {"serviceEndpoint": env["storageBlobUrl"], "accountKind": "StorageV2"}})
    else:
        add("linkedservices", "linkedService", ls_blob, {"type": "AzureBlobFS", "typeProperties": {"url": env["storageDfsUrl"]}})
    add("linkedservices", "linkedService", ls_sql, {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": {
        "type": "AzureKeyVaultSecret", "store": ref(ls_kv, "LinkedServiceReference"), "secretName": env["keyVaultSqlSecretName"]}}})

    prepare = _batches(sql["files"]["sql/00_staging_tables.sql"]) + _batches(sql["files"]["sql/usp_load_all.sql"])
    activities: List[Dict[str, Any]] = [{
        "name": "Prepare_SQL", "type": "Script", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
        "typeProperties": {"scripts": [{"type": "NonQuery", "text": b} for b in prepare]}}]
    copies: List[str] = []
    for table in sm["stagingTables"]:
        _ident(table)
        folder = f"{env['stagingFolder']}/lookups/{table[4:]}" if table.startswith("lkp_") else f"{env['stagingFolder']}/{table.lower()}"
        ds_blob, ds_sql = f"DS_Staging_{table}", f"DS_StagingSql_{table}"
        location = ({"type": "AzureBlobStorageLocation", "container": env["stagingContainer"], "folderPath": folder} if real
                    else {"type": "AzureBlobFSLocation", "fileSystem": env["stagingContainer"], "folderPath": folder})
        add("datasets", "dataset", ds_blob, {"type": "DelimitedText", "linkedServiceName": ref(ls_blob, "LinkedServiceReference"),
            "typeProperties": {"location": location, "columnDelimiter": ",", "firstRowAsHeader": True}})
        add("datasets", "dataset", ds_sql, {"type": "AzureSqlTable", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
            "typeProperties": {"schema": sql_package.STAGING_SCHEMA, "table": table}})
        activities.append({"name": f"Copy_{table}", "type": "Copy", "dependsOn": [{"activity": "Prepare_SQL", "dependencyConditions": ["Succeeded"]}],
                           "inputs": [ref(ds_blob, "DatasetReference")], "outputs": [ref(ds_sql, "DatasetReference")],
                           "typeProperties": {"source": {"type": "DelimitedTextSource"},
                                              "sink": {"type": "AzureSqlSink", "preCopyScript": f"TRUNCATE TABLE [{sql_package.STAGING_SCHEMA}].[{table}]"}}})
        copies.append(f"Copy_{table}")
    activities.append({"name": "Run_Transformation_SQL", "type": "Script", "linkedServiceName": ref(ls_sql, "LinkedServiceReference"),
                       "dependsOn": [{"activity": c, "dependencyConditions": ["Succeeded"]} for c in copies],
                       "typeProperties": {"scripts": [{"type": "NonQuery", "text": f"EXEC {sm['procedure']};"}]}})

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
        "stagingTables": sm["stagingTables"], "procedure": sm["procedure"],
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
                             f"{env['stagingContainer']}/{env['stagingFolder']}/ into staging tables and runs it. TR_XODUS_Curated_Ready starts the pipeline when a\n"
                             f"{env['readySuffix']} file is created under {env['readyContainer']}/{env['readyFolder']}/; it is published stopped and must be started in Data Factory.\n"
                             "Blob storage is reached with the factory's managed identity (give it Storage Blob Data Contributor). No secrets are included.\n")
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
