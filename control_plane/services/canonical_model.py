"""Canonical model v2: the universal model every source maps into and every target maps out of, with its code sets.

The definition lives in platform_knowledge/canonical_model_v2.yaml and is read as it is (no version, no release). Coded fields are bound to code sets; source values are mapped to canonical codes per migration
(code_mappings) and canonical codes to target values per target system (target_crosswalk). Suggestions here are deterministic
(exact code, synonym or description match after normalising); a person approves every mapping.
"""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from control_plane.paths import platform_knowledge_dir

DEFINITION = platform_knowledge_dir() / "canonical_model_v2.yaml"
TYPES = {"string", "date", "integer", "decimal", "boolean"}
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
def validate(model: Dict[str, Any]) -> Dict[str, Any]:
    """Structural checks on a definition; raises ValueError naming the first problem."""
    sets = model.get("code_sets") or {}
    for name, cs in sets.items():
        codes = [c.get("code") for c in cs.get("codes") or []]
        if not codes or not all(CODE.match(str(c)) for c in codes) or len(set(codes)) != len(codes):
            raise ValueError(f"Code set {name}: codes must be unique UPPER_SNAKE_CASE values.")
    entities = model.get("entities") or {}
    if not entities:
        raise ValueError("The model defines no entities.")
    for ename, e in entities.items():
        names = [f.get("name") for f in e.get("fields") or []]
        if len(set(names)) != len(names) or not all(NAME.match(str(n)) for n in names):
            raise ValueError(f"{ename}: field names must be unique UPPER_SNAKE_CASE.")
        if not any(f.get("key") for f in e["fields"]):
            raise ValueError(f"{ename} has no key field.")
        for f in e["fields"]:
            if f.get("type", "string") not in TYPES:
                raise ValueError(f"{ename}.{f['name']}: unknown type {f.get('type')}.")
            if f.get("code_set") and f["code_set"] not in sets:
                raise ValueError(f"{ename}.{f['name']}: unknown code set {f['code_set']}.")
    return model


def load_definition(path: Path = DEFINITION) -> Dict[str, Any]:
    return validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def current(conn=None) -> Dict[str, Any]:
    """The canonical model: the definition file as it is now. There is no version or release to pin (the system is not in production); edit the
    definition and the platform uses it at once. (The connection argument is kept so callers need not change.)"""
    return load_definition()


def coded_fields(model: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """{entity: {field: code_set}} for every field bound to a code set."""
    return {e: {f["name"]: f["code_set"] for f in body["fields"] if f.get("code_set")} for e, body in model["entities"].items()
            if any(f.get("code_set") for f in body["fields"])}


def codes(model: Dict[str, Any], code_set: str) -> List[str]:
    return [c["code"] for c in model["code_sets"][code_set]["codes"]]


def normalise(value: Any) -> str:
    """How a source value is compared and stored: trimmed and upper case (the generated SQL matches on UPPER(LTRIM(RTRIM(value))))."""
    return str(value if value is not None else "").strip().upper()


def _key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalise(value))


def suggest(model: Dict[str, Any], code_set: str, value: Any) -> Tuple[Optional[str], str]:
    """(canonical code, reason) for a source value, or (None, reason). Exact code first, then a listed synonym, then the description."""
    k = _key(value)
    if not k:
        return None, "Blank value."
    entries = model["code_sets"][code_set]["codes"]
    for c in entries:
        if _key(c["code"]) == k:
            return c["code"], "Same as the canonical code."
    for c in entries:
        if any(_key(s) == k for s in c.get("synonyms") or []):
            return c["code"], "A known spelling of this code."
    for c in entries:
        if _key(c.get("description")) == k:
            return c["code"], "Same as the code's description."
    return None, "No known spelling matches; choose the code."
