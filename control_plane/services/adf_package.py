"""Builds an Azure Data Factory package from APPROVED canonical-to-target mappings.

Deterministic and declarative: each approved mapping becomes one mapping-data-flow column expression. Only
DIRECT_COPY and approved, registered transformation rules are translated; anything else is refused, and no
arbitrary code is ever emitted. Azure values come from ``azure_simulated`` and nothing is deployed or called.
"""
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import transformation_rules as tr

IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# Rule input-format tokens to ADF data flow (Java) date patterns.
ADF_DATE = {"MM/DD/YYYY": "MM/dd/yyyy", "DD/MM/YYYY": "dd/MM/yyyy", "YYYY-MM-DD": "yyyy-MM-dd", "MM-DD-YYYY": "MM-dd-yyyy",
            "DD-MON-YYYY": "dd-MMM-yyyy", "YYYYMMDD": "yyyyMMdd", "MM/DD/YY": "MM/dd/yy"}


def _lit(text: str) -> str:
    """ADF data flow string literal."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ").replace("\r", " ") + "'"


def _ident(name: str) -> str:
    if not IDENT.match(str(name or "")):
        raise ValueError(f"'{name}' is not a safe column name.")
    return name


def translate(expression: str, column: str, rule: Optional[Dict[str, Any]]) -> str:
    """Data flow expression for one approved mapping. Raises ValueError for anything unregistered."""
    col = _ident(column)
    if expression == "DIRECT_COPY":
        return col
    if not rule or rule.get("status") != "APPROVED":
        raise ValueError(f"'{expression}' is not an approved, registered transformation.")
    p = tr.validate_params(rule["rule_type"], rule["params"])
    kind = rule["rule_type"]
    blank = f"isNull({col}) || trim({col}) == ''"
    if kind == "DEFAULT_IF_BLANK":
        return f"iif({blank}, {_lit(p['default'])}, {col})"
    if kind == "CONCAT":
        return f"concatWS({_lit(p['separator'])}, " + ", ".join(f"trim({_ident(s)})" for s in p["sources"]) + ")"
    if kind == "VALUE_MAP":
        key = f"upper(trim({col}))" if p["case_insensitive"] else f"trim({col})"
        pairs = ", ".join(f"{_lit(k)}, {_lit(v)}" for k, v in p["map"].items())
        body = f"case({key}, {pairs}, {col})"
    elif kind == "DATE_FORMAT":
        parsed = ", ".join(f"toDate(trim({col}), {_lit(ADF_DATE[f])})" for f in p["input_formats"])
        body = f"toString(coalesce({parsed}), 'yyyy-MM-dd')"
    elif kind == "NUMBER_CLEAN":
        def stripped(e: str) -> str:
            for ch in p["strip_chars"]:
                e = f"replace({e}, {_lit(ch)}, '')"
            return e
        t = f"trim({col})"
        plain = f"toDecimal({stripped(t)}, 38, {p['decimals']})"
        if p["parentheses_negative"]:
            inner = f"substring({t}, 2, length({t}) - 2)"
            body = f"toString(iif(startsWith({t}, '(') && endsWith({t}, ')'), -toDecimal({stripped(inner)}, 38, {p['decimals']}), {plain}))"
        else:
            body = f"toString({plain})"
    elif kind == "SPLIT_PART":
        body = f"trim(split({col}, {_lit(p['delimiter'])})[{p['index'] + 1}])"
    elif kind == "IF_EQUALS":
        other = col if p["else"] is None else _lit(p["else"])
        body = f"iif(trim({col}) == {_lit(p['when_equals'])}, {_lit(p['then'])}, {other})"
    else:
        raise ValueError(f"Unsupported rule type '{kind}'.")
    return f"iif({blank}, {col}, {body})"


def _table_flow(entity: str, table: str, fields: Dict[str, Dict[str, Any]], mappings: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]]) -> str:
    """Mapping data flow script for one target table of an entity."""
    cols: Dict[str, str] = {}
    derived: List[str] = []
    targets: List[str] = []
    for m in sorted(mappings, key=lambda r: r["target_field"]):
        tgt, src = _ident(fields[m["target_field"]]["column"]), _ident(m["canonical_field"] or "")
        expr = translate(m["transformation_expression"], src, rules.get(m["transformation_expression"]))
        for extra in re.findall(r"trim\(([A-Za-z][A-Za-z0-9_]*)\)", expr):
            cols[extra] = "string"
        cols[src] = "string"
        derived.append(f"{tgt} = {expr}")
        targets.append(tgt)
    src_cols = ",\n\t\t".join(f"{c} as string" for c in sorted(cols))
    node = f"{entity}_{table}"
    return (
        f"source(output(\n\t\t{src_cols}\n\t),\n\tallowSchemaDrift: false,\n\tvalidateSchema: false,\n\tignoreNoFilesFound: false) ~> src_{node}\n"
        f"src_{node} derive(" + ",\n\t".join(derived) + f") ~> derive_{node}\n"
        f"derive_{node} select(mapColumn(" + ", ".join(targets) + f")) ~> project_{node}\n"
        f"project_{node} sink(allowSchemaDrift: false,\n\tvalidateSchema: false,\n\tdeletable: false,\n\tinsertable: true,\n\tupdateable: false,\n\tupsertable: false,\n"
        f"\tskipDuplicateMapInputs: true,\n\tskipDuplicateMapOutputs: true) ~> sink_{node}"
    )


def build(migration_id: str, spec: Dict[str, Any], approved: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]],
          env: Dict[str, Any], generated_by: str) -> Dict[str, Any]:
    """Return {'files': {path: text}, 'manifest': {...}}. Raises ValueError when there is nothing safe to package."""
    approved = [m for m in approved if m.get("status") == "APPROVED"]
    if not spec or not approved:
        raise ValueError("Approve at least one target mapping before generating an ADF package.")
    mig = _ident(re.sub(r"[^A-Za-z0-9_]", "_", migration_id))
    files: Dict[str, Any] = {}
    resources: List[Dict[str, Any]] = []
    activities: List[Dict[str, Any]] = []
    entities: Dict[str, Any] = {}
    warnings: List[str] = []

    def add(kind: str, folder: str, name: str, props: Dict[str, Any]) -> None:
        files[f"{folder}/{name}.json"] = {"name": name, "properties": props}
        resources.append({"type": f"Microsoft.DataFactory/factories/{kind}", "apiVersion": "2018-06-01",
                          "name": f"[concat(parameters('factoryName'), '/{name}')]", "properties": props,
                          "dependsOn": []})

    add("linkedservices", "linkedService", "LS_KeyVault_Sim", {"type": "AzureKeyVault", "typeProperties": {"baseUrl": env["keyVaultUrl"]}})
    add("linkedservices", "linkedService", "LS_ADLS_Staging_Sim", {"type": "AzureBlobFS", "typeProperties": {"url": env["storageDfsUrl"]}})
    add("linkedservices", "linkedService", "LS_SQL_Target_Sim", {"type": "AzureSqlDatabase", "typeProperties": {"connectionString": {
        "type": "AzureKeyVaultSecret", "store": {"referenceName": "LS_KeyVault_Sim", "type": "LinkedServiceReference"},
        "secretName": env["keyVaultSqlSecretName"]}}})

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
        uncovered = [n for n in fields if n not in mapped]
        req_missing = [n for n, f in fields.items() if f.get("required") and n not in mapped]
        if req_missing:
            warnings.append(f"{entity}: required target field(s) without an approved mapping: {', '.join(req_missing)}")
        ds_src = f"DS_Staging_{entity}"
        add("datasets", "dataset", ds_src, {"type": "DelimitedText", "linkedServiceName": {"referenceName": "LS_ADLS_Staging_Sim", "type": "LinkedServiceReference"},
            "typeProperties": {"location": {"type": "AzureBlobFSLocation", "fileSystem": env["stagingContainer"], "folderPath": f"{env['stagingFolder']}/{entity.lower()}"},
                               "columnDelimiter": ",", "firstRowAsHeader": True}})
        tables: Dict[str, Any] = {}
        for table in sorted({fields[m["target_field"]]["table"] for m in rows}):
            _ident(table)
            trows = [m for m in rows if fields[m["target_field"]]["table"] == table]
            script = _table_flow(entity, table, fields, trows, rules)
            ds_tgt, flow, node = f"DS_Target_{table}", f"DF_{entity}_{table}", f"{entity}_{table}"
            add("datasets", "dataset", ds_tgt, {"type": "AzureSqlTable", "linkedServiceName": {"referenceName": "LS_SQL_Target_Sim", "type": "LinkedServiceReference"},
                "typeProperties": {"schema": env["sqlSchema"], "table": table}})
            add("dataflows", "dataflow", flow, {"type": "MappingDataFlow", "typeProperties": {
                "sources": [{"dataset": {"referenceName": ds_src, "type": "DatasetReference"}, "name": f"src_{node}"}],
                "sinks": [{"dataset": {"referenceName": ds_tgt, "type": "DatasetReference"}, "name": f"sink_{node}"}],
                "transformations": [{"name": f"derive_{node}"}, {"name": f"project_{node}"}],
                "scriptLines": script.split("\n")}})
            activities.append({"name": f"Load_{node}", "type": "ExecuteDataFlow", "typeProperties": {
                "dataflow": {"referenceName": flow, "type": "DataFlowReference"}, "compute": {"coreCount": 8, "computeType": "General"}}})
            tables[table] = {"approvedMappings": len(trows), "columns": sorted(fields[m["target_field"]]["column"] for m in trows)}
        entities[entity] = {"approvedMappings": len(rows), "tables": tables, "uncoveredFields": uncovered,
                            "reviewers": sorted({m.get("reviewer") or "" for m in rows} - {""})}

    pipeline = f"PL_XODUS_{mig}"
    add("pipelines", "pipeline", pipeline, {"description": "Canonical staging to simulated Simplicity target. Generated from approved mappings.",
        "parameters": {"MigrationId": {"type": "string", "defaultValue": migration_id}}, "activities": activities})
    arm = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#", "contentVersion": "1.0.0.0",
           "metadata": {"simulated": True, "note": env["banner"]},
           "parameters": {"factoryName": {"type": "string", "defaultValue": env["factoryName"]}}, "resources": resources}
    manifest = {
        "simulated": True, "banner": env["banner"], "migrationId": migration_id, "generatedBy": generated_by,
        "generatedUtc": datetime.now(timezone.utc).isoformat(), "targetSystem": spec.get("target_system"), "targetModelVersion": spec["spec_version"],
        "pipeline": pipeline, "environment": {k: v for k, v in env.items() if k != "banner"}, "entities": entities, "warnings": warnings,
        "deployed": False, "note": "Nothing was deployed or sent to Azure. A human must review the package and replace the simulated environment values.",
    }
    out = {p: json.dumps(c, indent=2) for p, c in files.items()}
    out["arm/ARMTemplateForFactory.json"] = json.dumps(arm, indent=2)
    out["manifest.json"] = json.dumps(manifest, indent=2)
    out["README_SIMULATED.txt"] = (env["banner"] + "\n\nGenerated from approved mappings only. Every Azure value (subscription, factory, storage, SQL, Key Vault) is fictitious.\n"
                                  "No secrets are included; the SQL connection string is a Key Vault secret reference.\n")
    return {"files": out, "manifest": manifest}


def to_zip(package: Dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, text in sorted(package["files"].items()):
            z.writestr(path, text)
    return buf.getvalue()
