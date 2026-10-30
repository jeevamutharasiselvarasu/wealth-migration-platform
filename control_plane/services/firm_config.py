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

P0_ENTITIES = ("Account", "Client", "Household")
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "firm_config.yaml"
CONFIG_PATH_ENV = "XODUS_FIRM_CONFIG"


class FirmConfigError(Exception):
    """Raised when the firm configuration is missing or invalid."""


class FieldContract(BaseModel):
    name: str
    type: Literal["string", "integer", "decimal", "date", "boolean"]
    nullable: bool = True
    is_key: bool = False
    description: str = ""


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
        unexpected = sorted(set(value) - set(P0_ENTITIES))
        missing = sorted(set(P0_ENTITIES) - set(value))
        if unexpected or missing:
            raise ValueError(f"entities must be exactly {list(P0_ENTITIES)}; unexpected={unexpected} missing={missing}")
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
