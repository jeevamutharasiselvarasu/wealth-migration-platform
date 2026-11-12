"""Versioned firm configuration: registered target system and canonical contract.

Source-to-canonical only. Entity scope is limited to the P0 entities; field
nullability and keys are declared here so validation can be target-aware.
"""
import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Literal

import yaml
from pydantic import BaseModel, ValidationError, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "firm_config.yaml"
CONFIG_PATH_ENV = "XODUS_FIRM_CONFIG"


def _canonical_entity_names() -> tuple:
    """The canonical entities, as the firm configuration file defines them (in file order). Nothing about them is written in code."""
    try:
        path = Path(os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return tuple((raw.get("entities") or {}).keys())
    except (OSError, yaml.YAMLError, AttributeError):
        return ()


P0_ENTITIES = _canonical_entity_names()


def _reference_map(cfg) -> Dict[str, Dict[str, str]]:
    """{entity: {other entity: the field that points at it}}: a field named like another entity's key refers to that entity."""
    keys = {e: {f.name for f in d.fields if f.is_key} for e, d in cfg.entities.items()}
    refs: Dict[str, Dict[str, str]] = {}
    for e, d in cfg.entities.items():
        for f in d.fields:
            for other, k in keys.items():
                if other != e and f.name in k and not f.is_key:
                    refs.setdefault(e, {}).setdefault(other, f.name)
    return refs


def key_fields(entity: str, cfg=None) -> List[str]:
    """The entity's own key fields, as the firm configuration marks them."""
    cfg = cfg or get_firm_config()
    return [f.name for f in cfg.entities[entity].fields if f.is_key] if entity in cfg.entities else []


def reference_fields(cfg=None) -> Dict[str, Dict[str, tuple]]:
    """{entity: {field: (other entity, other entity's key field)}}: every field that points at another entity's key."""
    cfg = cfg or get_firm_config()
    out: Dict[str, Dict[str, tuple]] = {}
    for e, others in _reference_map(cfg).items():
        for other, field in others.items():
            out.setdefault(e, {})[field] = (other, field)
    return out


def supporting_plan(selected_targets, target_entities=None, cfg=None) -> Dict[str, str]:
    """Canonical entities the platform adds for a selection of target entities, with the reason. The user never chooses these.

    Only canonical entities the target has no table for qualify (a target entity is the user's own choice). One qualifies when
    a selected entity has a field that points at it (a household's advisor), or when it only links entities that are all selected
    (an account role links accounts and clients). Both come from the key and field names in the firm configuration."""
    cfg = cfg or get_firm_config()
    refs = _reference_map(cfg)
    targets = set(target_entities if target_entities is not None else selected_targets)
    chosen, why = set(selected_targets), {}
    changed = True
    while changed:
        changed = False
        for e in list(chosen):
            for other, field in refs.get(e, {}).items():
                if other not in targets and other not in chosen:
                    chosen.add(other)
                    why[other] = f"{e}.{field} points at it"
                    changed = True
        for e in cfg.entities:
            if e in targets or e in chosen:
                continue
            linked = refs.get(e, {})
            if linked and all(o in chosen for o in linked):
                chosen.add(e)
                why[e] = "it links " + " and ".join(sorted(linked)) + " records"
                changed = True
    return {e: why[e] for e in P0_ENTITIES if e in why}


def with_supporting_entities(selected_targets, target_entities=None, cfg=None) -> List[str]:
    """The canonical scope for a selection of target entities: the selection plus the supporting entities it needs, in file order."""
    plan = supporting_plan(selected_targets, target_entities, cfg)
    chosen = set(selected_targets) | set(plan)
    return [e for e in P0_ENTITIES if e in chosen]


class FirmConfigError(Exception):
    """Raised when the firm configuration is missing or invalid."""


class FieldContract(BaseModel):
    name: str
    type: Literal["string", "integer", "decimal", "date", "boolean"]
    nullable: bool = True
    is_key: bool = False
    description: str = ""
    # Personal data: lineage, previews and logs must never show its values.
    sensitive: bool = False
    # Target layouts (for example Orion MCL, SPARC) that require this field. Enforced at canonical-to-target
    # mapping (a later phase); it does not make the field required for source-to-canonical mapping.
    required_for_targets: List[str] = []


class EntityContract(BaseModel):
    grain: str = ""
    fields: List[FieldContract]

    @model_validator(mode="after")
    def _check_fields(self):
        names = [f.name for f in self.fields]
        if not names:
            raise ValueError("entity must define at least one field")
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate field definitions: {duplicates}")
        nullable_keys = [f.name for f in self.fields if f.is_key and f.nullable]
        if nullable_keys:
            raise ValueError(f"key fields must be non-nullable: {nullable_keys}")
        return self


class TargetSystem(BaseModel):
    name: str
    kind: str
    load_strategy: Literal["CURATED_FILE_OUTPUT", "SQL_STAGE"]


class FirmConfig(BaseModel):
    schema_version: Literal[1]
    firm_id: str
    firm_name: str
    config_version: str
    scope_direction: Literal["source_to_canonical"]
    canonical_contract_ref: str
    target_system: TargetSystem
    entities: Dict[str, EntityContract]

    @field_validator("entities")
    @classmethod
    def _p0_entities_only(cls, value):
        if not value:
                raise ValueError("entities must define at least one canonical entity")
        return value

    @property
    def content_hash(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def version_id(self) -> str:
        """Human version plus a content hash so silent edits yield a new identifier."""
        return f"{self.config_version}+{self.content_hash[:12]}"

    def required_fields(self, entity: str) -> List[str]:
        return [f.name for f in self.entities[entity].fields if not f.nullable]

    def canonical_field_names(self, entity: str) -> List[str]:
        return [f.name for f in self.entities[entity].fields]

    def field_contract(self, entity: str, field: str) -> FieldContract:
        for item in self.entities[entity].fields:
            if item.name == field:
                return item
        raise KeyError(f"{entity}.{field} is not defined in the firm configuration")


def _resolve_path(path: Path | str | None) -> Path:
    return Path(path or os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)


_CACHE: Dict[tuple, FirmConfig] = {}


def get_firm_config(path: Path | str | None = None) -> FirmConfig:
    """Cached loader for hot paths; reloads when the file path or modification time changes."""
    config_path = _resolve_path(path)
    try:
        key = (str(config_path), config_path.stat().st_mtime_ns)
    except OSError as exc:
        raise FirmConfigError(f"Firm configuration not found: {config_path.name}") from exc
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = load_firm_config(config_path)
    return _CACHE[key]


def load_firm_config(path: Path | str | None = None) -> FirmConfig:
    config_path = _resolve_path(path)
    if not config_path.is_file():
        raise FirmConfigError(f"Firm configuration not found: {config_path.name}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return FirmConfig.model_validate(raw)
    except (yaml.YAMLError, ValidationError, TypeError) as exc:
        raise FirmConfigError(f"Invalid firm configuration: {exc}") from exc
