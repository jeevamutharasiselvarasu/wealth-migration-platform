"""Deployment package contract (DRAFT, not yet agreed with the technical owner) and package generation.

The package is a versioned package.yaml generated only from APPROVED configuration: a confirmed target model,
approved source-to-canonical and canonical-to-target mappings, approved governed rules and active validation
scenarios. It is validated against the pydantic model below (``json_schema()`` exports it) before it is stored.
Replace the model with the agreed template when the technical owner supplies it; the generator is the only
place that maps XODUS state to the package.
"""
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "0.1-DRAFT"
_FORBIDDEN = ("password", "secret", "token", "accountkey", "connectionstring")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(_M):
    package_id: str
    package_version: int = Field(ge=1)
    schema_version: Literal["0.1-DRAFT"]
    contract_status: Literal["DRAFT_NOT_AGREED"]
    generated_utc: str
    generated_by: str
    simulated: bool
    content_sha256: str


class MigrationRef(_M):
    migration_id: str
    source_system: str
    target_system: str


class FirmRef(_M):
    firm_id: str
    config_version: str
    canonical_contract_ref: str


class TargetRef(_M):
    name: str
    model_version: str
    confirmed_by: str
    confirmed_utc: str


class SourceFile(_M):
    filename: str
    entity: str


class SourceMapping(_M):
    entity: str
    source_field: str
    canonical_field: str
    transformation: str


class TargetMapping(_M):
    entity: str
    target_table: str
    target_column: str
    canonical_field: str
    transformation: str
    approved_by: str


class RuleDef(_M):
    expression: str
    rule_type: str
    params: Dict[str, Any]
    description: str
    approved_by: str


class ScenarioDef(_M):
    scenario_id: str
    entity: str
    check_type: str
    field: Optional[str] = None
    params: Dict[str, Any]
    stage: Literal["PRE", "POST"]
    description: str


class Validation(_M):
    scenarios: List[ScenarioDef]
    default_checks: List[str]


class Environment(_M):
    simulated: bool
    values: Dict[str, str]


class PackageDoc(_M):
    meta: Meta
    migration: MigrationRef
    firm: FirmRef
    target: TargetRef
    source_files: List[SourceFile]
    source_to_canonical: List[SourceMapping]
    canonical_to_target: List[TargetMapping] = Field(min_length=1)
    transformation_rules: List[RuleDef]
    validation: Validation
    environment: Environment


DEFAULT_CHECKS = ["REQUIRED_FIELDS_MAPPED", "REQUIRED_VALUES_PRESENT", "KEY_UNIQUE", "RELATIONSHIP"]

_DDL = """CREATE TABLE IF NOT EXISTS deployment_packages (
    package_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, package_version INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
    yaml_text TEXT NOT NULL, schema_version TEXT NOT NULL, generated_by TEXT NOT NULL, generated_utc TEXT NOT NULL,
    UNIQUE (migration_id, package_version))"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def json_schema() -> Dict[str, Any]:
    return PackageDoc.model_json_schema()


def validate_text(yaml_text: str) -> PackageDoc:
    """Parse and validate package YAML. Raises ValueError with a readable message."""
    try:
        data = yaml.safe_load(yaml_text)
        doc = PackageDoc.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"Package does not satisfy the draft contract: {exc}")
    flat = json.dumps(data).lower()
    for word in _FORBIDDEN:
        if f'"{word}"' in flat:
            raise ValueError(f"Package must not contain secret-like key '{word}'.")
    return doc


def _digest(body: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def build_body(*, migration_id: str, source_system: str, target_system: str, firm: Dict[str, str], target: Dict[str, str],
               source_files: List[Dict[str, str]], source_mappings: List[Dict[str, str]], spec: Dict[str, Any],
               approved_target: List[Dict[str, Any]], rules: Dict[str, Dict[str, Any]], scenarios: List[Dict[str, Any]],
               environment: Dict[str, Any]) -> Dict[str, Any]:
    """Package content without meta. Raises ValueError when approved configuration is incomplete."""
    approved_target = [m for m in approved_target if m["status"] == "APPROVED"]
    if not approved_target:
        raise ValueError("Approve at least one target mapping before generating a deployment package.")
    if not source_mappings:
        raise ValueError("Approve at least one source-to-canonical mapping before generating a deployment package.")
    used_rules: List[Dict[str, Any]] = []
    t_rows = []
    for m in approved_target:
        expr = m["transformation_expression"]
        if expr != "DIRECT_COPY":
            rule = rules.get(expr)
            if not rule or rule["status"] != "APPROVED":
                raise ValueError(f"Mapping uses '{expr}', which is not an approved rule.")
            if expr not in {r["expression"] for r in used_rules}:
                used_rules.append({"expression": expr, "rule_type": rule["rule_type"], "params": rule["params"], "description": rule["description"],
                                   "approved_by": rule.get("reviewed_by") or ""})
        field = next((f for f in spec["entities"][m["entity_name"]]["fields"] if f["name"] == m["target_field"]), None)
        if not field:
            raise ValueError(f"{m['target_field']} is not in the confirmed target model.")
        t_rows.append({"entity": m["entity_name"], "target_table": field["table"], "target_column": field["column"], "canonical_field": m["canonical_field"],
                       "transformation": expr, "approved_by": m.get("reviewer") or ""})
    return {
        "migration": {"migration_id": migration_id, "source_system": source_system, "target_system": target_system},
        "firm": firm, "target": target,
        "source_files": sorted(source_files, key=lambda f: (f["entity"], f["filename"])),
        "source_to_canonical": sorted(source_mappings, key=lambda m: (m["entity"], m["canonical_field"], m["source_field"])),
        "canonical_to_target": sorted(t_rows, key=lambda m: (m["entity"], m["target_table"], m["target_column"])),
        "transformation_rules": sorted(used_rules, key=lambda r: r["expression"]),
        "validation": {"scenarios": [{"scenario_id": s["scenario_id"], "entity": s["entity_name"], "check_type": s["check_type"], "field": s.get("field_name"),
                                      "params": s["params"], "stage": s["stage"], "description": s["description"]}
                       for s in sorted(scenarios, key=lambda s: s["scenario_id"]) if s["status"] == "ACTIVE"],
                       "default_checks": DEFAULT_CHECKS},
        "environment": {"simulated": bool(environment.get("simulated")), "values": {k: str(v) for k, v in environment.items() if k not in ("simulated", "banner")}},
    }


def generate(conn: sqlite3.Connection, migration_id: str, actor: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and store a new package version; an identical package returns the existing version."""
    ensure_table(conn)
    sha = _digest(body)
    last = conn.execute("SELECT package_id, package_version, content_sha256 FROM deployment_packages WHERE migration_id = ? "
                        "ORDER BY package_version DESC LIMIT 1", (migration_id,)).fetchone()
    if last and last["content_sha256"] == sha:
        return {"package_id": last["package_id"], "package_version": last["package_version"], "new": False}
    version = (last["package_version"] + 1) if last else 1
    pid = f"PKG-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    doc = {"meta": {"package_id": pid, "package_version": version, "schema_version": SCHEMA_VERSION, "contract_status": "DRAFT_NOT_AGREED",
                    "generated_utc": now, "generated_by": actor, "simulated": body["environment"]["simulated"], "content_sha256": sha}, **body}
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    validate_text(text)
    conn.execute("INSERT INTO deployment_packages (package_id, migration_id, package_version, content_sha256, yaml_text, schema_version, generated_by, generated_utc) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (pid, migration_id, version, sha, text, SCHEMA_VERSION, actor, now))
    return {"package_id": pid, "package_version": version, "new": True}


def list_packages(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute("SELECT package_id, package_version, content_sha256, schema_version, generated_by, generated_utc "
                                          "FROM deployment_packages WHERE migration_id = ? ORDER BY package_version DESC", (migration_id,)).fetchall()]


def get_package(conn: sqlite3.Connection, package_id: str) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM deployment_packages WHERE package_id = ?", (package_id,)).fetchone()
    return dict(row) if row else None
