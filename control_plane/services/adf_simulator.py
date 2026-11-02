"""SIMULATED Azure Data Factory: deploy a stored ADF package into a fictitious factory and trigger its pipeline.

No Azure call is made. Deployment inspects the package the way a real deployment would reject it (broken references, secrets in
clear text, a package that is not marked simulated) and records a per-resource outcome. A pipeline trigger is only allowed against
a deployment whose package is still current, and the run itself reuses the simulated data plane (execution_simulator).
This module is the seam where a real ADF client would plug in later; its records say SIMULATED everywhere.
"""
import io
import json
import re
import sqlite3
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

KINDS = {"linkedService": "LinkedService", "dataset": "Dataset", "dataflow": "DataFlow", "pipeline": "Pipeline"}
_SECRET = re.compile(r"(AccountKey|SharedAccessSignature|Password|Pwd)\s*=\s*[^;\"\s]+|\"(password|accountKey|sasToken)\"\s*:\s*\"(?!@)", re.I)

_DDL = """CREATE TABLE IF NOT EXISTS adf_simulated_deployments (
    deployment_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, artifact_id TEXT NOT NULL, artifact_version INTEGER NOT NULL,
    factory_name TEXT NOT NULL, status TEXT NOT NULL, resources_json TEXT NOT NULL, errors_json TEXT NOT NULL,
    deployed_by TEXT NOT NULL, deployed_utc TEXT NOT NULL, last_run_id TEXT, last_adf_run_id TEXT, last_triggered_by TEXT, last_triggered_utc TEXT)"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def inspect(zip_bytes: bytes) -> Dict[str, Any]:
    """Pure check of a package zip. Returns {factory, resources: [{kind, name, status, detail}], errors: [str], pipeline}."""
    errors: List[str] = []
    resources: List[Dict[str, Any]] = []
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = z.namelist()
    except zipfile.BadZipFile:
        return {"factory": None, "resources": [], "errors": ["The stored file is not a valid zip."], "pipeline": None}
    docs: Dict[str, Dict[str, Any]] = {}
    for n in names:
        if not n.endswith(".json"):
            continue
        text = z.read(n).decode("utf-8", "replace")
        if _SECRET.search(text):
            errors.append(f"{n}: contains a secret in clear text (use a Key Vault reference).")
        try:
            docs[n] = json.loads(text)
        except ValueError:
            errors.append(f"{n}: is not valid JSON.")
    manifest = docs.get("manifest.json", {})
    if not manifest:
        errors.append("manifest.json is missing.")
    elif manifest.get("simulated") is not True:
        errors.append("The package is not marked simulated; it will not be deployed to the simulated factory.")
    if "arm/ARMTemplateForFactory.json" not in docs:
        errors.append("arm/ARMTemplateForFactory.json is missing.")
    by_kind: Dict[str, Dict[str, Dict[str, Any]]] = {k: {} for k in KINDS}
    for path, doc in docs.items():
        folder = path.split("/")[0]
        if folder in KINDS:
            by_kind[folder][doc.get("name", path)] = doc
    ls, ds, df, pl = (by_kind[k] for k in ("linkedService", "dataset", "dataflow", "pipeline"))
    problems: Dict[tuple, List[str]] = {}

    def bad(kind: str, name: str, msg: str):
        problems.setdefault((kind, name), []).append(msg)
        errors.append(f"{kind} {name}: {msg}")
    for name, d in ds.items():
        ref = ((d.get("properties") or {}).get("linkedServiceName") or {}).get("referenceName")
        if ref not in ls:
            bad("dataset", name, f"references missing linked service {ref}.")
    for name, d in df.items():
        tp = (d.get("properties") or {}).get("typeProperties") or {}
        for side in (tp.get("sources") or []) + (tp.get("sinks") or []):
            ref = (side.get("dataset") or {}).get("referenceName")
            if ref not in ds:
                bad("dataflow", name, f"references missing dataset {ref}.")
    pipeline_name = None
    for name, d in pl.items():
        pipeline_name = name
        acts = (d.get("properties") or {}).get("activities") or []
        if not acts:
            bad("pipeline", name, "has no activities.")
        for a in acts:
            ref = ((a.get("typeProperties") or {}).get("dataflow") or {}).get("referenceName")
            if ref not in df:
                bad("pipeline", name, f"activity {a.get('name')} references missing data flow {ref}.")
    if not pl:
        errors.append("The package has no pipeline.")
    for folder, label in KINDS.items():
        for name in sorted(by_kind[folder]):
            issues = problems.get((folder, name))
            resources.append({"kind": label, "name": name, "status": "REJECTED" if issues else "DEPLOYED_SIMULATED", "detail": "; ".join(issues or [])})
    env = manifest.get("environment") or {}
    return {"factory": env.get("factoryName"), "resources": resources, "errors": errors, "pipeline": pipeline_name}


def deploy(conn: sqlite3.Connection, migration_id: str, artifact: Dict[str, Any], zip_bytes: bytes, actor: str) -> Dict[str, Any]:
    ensure_table(conn)
    result = inspect(zip_bytes)
    if result["errors"]:  # nothing is deployed when anything is wrong, like an ARM deployment
        for r in result["resources"]:
            if r["status"] == "DEPLOYED_SIMULATED":
                r["status"] = "NOT_DEPLOYED"
    status = "REJECTED" if result["errors"] else "DEPLOYED_SIMULATED"
    row = {"deployment_id": "ADFDEP-" + uuid.uuid4().hex[:8].upper(), "migration_id": migration_id, "artifact_id": artifact["artifact_id"],
           "artifact_version": artifact["artifact_version"], "factory_name": result["factory"] or "unknown-simulated-factory", "status": status,
           "resources_json": json.dumps(result["resources"]), "errors_json": json.dumps(result["errors"]), "deployed_by": actor,
           "deployed_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
    conn.execute("INSERT INTO adf_simulated_deployments (deployment_id, migration_id, artifact_id, artifact_version, factory_name, status, resources_json, "
                 "errors_json, deployed_by, deployed_utc) VALUES (:deployment_id, :migration_id, :artifact_id, :artifact_version, :factory_name, :status, "
                 ":resources_json, :errors_json, :deployed_by, :deployed_utc)", row)
    return {**row, "resources": result["resources"], "errors": result["errors"], "pipeline": result["pipeline"]}


def latest(conn: sqlite3.Connection, migration_id: str) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    cur = conn.execute("SELECT * FROM adf_simulated_deployments WHERE migration_id = ? ORDER BY deployed_utc DESC, artifact_version DESC LIMIT 1", (migration_id,))
    r = cur.fetchone()
    if not r:
        return None
    d = dict(zip([c[0] for c in cur.description], r))
    d["resources"], d["errors"] = json.loads(d.pop("resources_json")), json.loads(d.pop("errors_json"))
    return d


def record_trigger(conn: sqlite3.Connection, deployment_id: str, run_id: str, actor: str) -> str:
    adf_run_id = "ADF-SIM-" + run_id
    conn.execute("UPDATE adf_simulated_deployments SET last_run_id = ?, last_adf_run_id = ?, last_triggered_by = ?, last_triggered_utc = ? WHERE deployment_id = ?",
                 (run_id, adf_run_id, actor, datetime.now(timezone.utc).isoformat(timespec="seconds"), deployment_id))
    return adf_run_id


def trigger_blocker(deployment: Optional[Dict[str, Any]], package_stale_reasons: Optional[List[str]], latest_artifact_id: Optional[str]) -> Optional[str]:
    """Why a pipeline trigger must be refused (None = allowed)."""
    if not deployment:
        return "Deploy the stored ADF package to the simulated factory first."
    if deployment["status"] != "DEPLOYED_SIMULATED":
        return "The last deployment was rejected; fix the package and deploy again."
    if latest_artifact_id and deployment["artifact_id"] != latest_artifact_id:
        return f"A newer package ({latest_artifact_id}) exists than the deployed one ({deployment['artifact_id']}); deploy it first."
    if package_stale_reasons:
        return "The deployed package is stale: " + " ".join(package_stale_reasons) + " Regenerate and redeploy it."
    return None
