"""Canonical model v2: the universal model every source maps into and every target maps out of, with its code sets.

The definition lives in platform_knowledge/canonical_model_v2.yaml; releasing it records an immutable version on the platform
(Canonical model page). Coded fields are bound to code sets; source values are mapped to canonical codes per migration
(code_mappings) and canonical codes to target values per target system (target_crosswalk). Suggestions here are deterministic
(exact code, synonym or description match after normalising); a person approves every mapping.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

DEFINITION = Path(__file__).resolve().parents[2] / "platform_knowledge" / "canonical_model_v2.yaml"
TYPES = {"string", "date", "integer", "decimal", "boolean"}
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_DDL = """
CREATE TABLE IF NOT EXISTS canonical_model_releases (
    version TEXT PRIMARY KEY, content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL, released_by TEXT NOT NULL, released_utc TEXT NOT NULL
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


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
    if not str(model.get("version") or "").strip():
        raise ValueError("The model has no version.")
    return model


def load_definition(path: Path = DEFINITION) -> Dict[str, Any]:
    return validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _sha(model: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(model, sort_keys=True).encode("utf-8")).hexdigest()


def release(conn: sqlite3.Connection, actor: str, path: Path = DEFINITION) -> Dict[str, Any]:
    """Record the definition as an immutable release. A version can be released once; a changed definition needs a new version."""
    ensure_table(conn)
    model = load_definition(path)
    row = conn.execute("SELECT content_sha256 FROM canonical_model_releases WHERE version = ?", (model["version"],)).fetchone()
    if row:
        if row["content_sha256"] == _sha(model):
            raise ValueError(f"Version {model['version']} is already released.")
        raise ValueError(f"Version {model['version']} was released with different content. Give the changed definition a new version.")
    rec = {"version": model["version"], "content_sha256": _sha(model), "released_by": actor, "released_utc": datetime.now(timezone.utc).isoformat()}
    conn.execute("INSERT INTO canonical_model_releases (version, content_json, content_sha256, released_by, released_utc) VALUES (?, ?, ?, ?, ?)",
                 (rec["version"], json.dumps(model), rec["content_sha256"], actor, rec["released_utc"]))
    return rec


def releases(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute("SELECT version, content_sha256, released_by, released_utc FROM canonical_model_releases ORDER BY released_utc DESC").fetchall()]


def current(conn: sqlite3.Connection) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """(model, release) for the latest release; (definition, None) while nothing is released yet."""
    ensure_table(conn)
    row = conn.execute("SELECT * FROM canonical_model_releases ORDER BY released_utc DESC LIMIT 1").fetchone()
    if row:
        return json.loads(row["content_json"]), {k: row[k] for k in ("version", "content_sha256", "released_by", "released_utc")}
    return load_definition(), None


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
