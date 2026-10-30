"""Infer a target system's meaning from its introspected schema (deterministic; no record data).

Given the schema a target exposes (tables, columns, types, descriptions, code lists) and the canonical model,
propose which canonical entity each table belongs to and which canonical field each column draws from, with a
confidence and the evidence used. Names and data-dictionary descriptions are compared through a generic
platform vocabulary. Nothing here is a decision: a human confirms the inferred model before mapping starts.
"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

VOCAB_PATH = Path(__file__).resolve().parents[2] / "platform_knowledge" / "field_vocabulary.yaml"
ENTITIES = ("Account", "Client", "Household")
_TYPE_GROUPS = {"string": "text", "date": "date", "integer": "number", "number": "number", "decimal": "number", "boolean": "bool"}
MATCH_THRESHOLD = 0.3
_vocab_cache: Optional[Dict[str, Any]] = None


def _vocab() -> Dict[str, Any]:
    global _vocab_cache
    if _vocab_cache is None:
        _vocab_cache = yaml.safe_load(VOCAB_PATH.read_text(encoding="utf-8"))
    return _vocab_cache


def concepts(*texts: str) -> Dict[str, float]:
    """Normalise names/descriptions to weighted concept words."""
    vocab = _vocab()
    weak = set(vocab.get("weak", []))
    out: Dict[str, float] = {}
    for text in texts:
        for tok in re.split(r"[^A-Za-z0-9]+", str(text or "").lower()):
            if not tok:
                continue
            for c in vocab["tokens"].get(tok, [tok]):
                out[c] = 0.5 if c in weak else 1.0
    return out


def _similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    inter = sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    union = sum(max(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    return inter / union if union else 0.0


def _type_ok(target_type: str, canonical_type: str) -> bool:
    if not canonical_type:  # fields added by change requests carry no declared type: do not exclude them on type
        return True
    return _TYPE_GROUPS.get(target_type, "text") == _TYPE_GROUPS.get(canonical_type, "text")


def _columns(table: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in table["columns"] if not c.get("generated")]


def _pair_scores(table: Dict[str, Any], entity: str, canonical: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """All scored (column, canonical field) pairs for one table against one entity."""
    pairs = []
    for col in _columns(table):
        # column names carry the signal; the data-dictionary description only breaks near-ties
        col_c = concepts(col["name"])
        desc_c = concepts(col["name"], col.get("description", ""))
        for cname, ctype in canonical.get(entity, {}).items():
            if not _type_ok(col["type"], ctype):
                continue
            can_c = concepts(cname)
            score = max(_similarity(col_c, can_c), 0.9 * _similarity(desc_c, can_c))
            if score > 0:
                pairs.append({"column": col["name"], "canonical": cname, "score": round(score, 3),
                              "evidence": sorted((set(col_c) | set(desc_c)) & set(can_c))})
    return pairs


def _assign(table: Dict[str, Any], entity: str, canonical: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    """Greedy one-to-one assignment of columns to canonical fields within an entity (best score first)."""
    pairs = sorted(_pair_scores(table, entity, canonical), key=lambda p: -p["score"])
    taken_cols: Set[str] = set()
    taken_can: Set[str] = set()
    result: Dict[str, Dict[str, Any]] = {}
    for p in pairs:
        if p["score"] < MATCH_THRESHOLD or p["column"] in taken_cols:
            continue
        # a canonical field may be fed by one column per table
        if p["canonical"] in taken_can:
            continue
        taken_cols.add(p["column"])
        taken_can.add(p["canonical"])
        result[p["column"]] = p
    return result


def infer(schema: Dict[str, Any], canonical: Dict[str, Dict[str, str]], scope_tables: Optional[List[str]] = None,
          include_code_lists: bool = True) -> Dict[str, Any]:
    """Return the inferred target model. `canonical` is {entity: {field: type}}."""
    tables = [t for t in schema["tables"] if not scope_tables or t["name"] in scope_tables]
    code_lists = schema.get("code_lists", {}) if include_code_lists else {}
    scores: Dict[str, Dict[str, float]] = {}
    for t in tables:
        per = {}
        for e in ENTITIES:
            if e in canonical:
                assigned = _assign(t, e, canonical)
                per[e] = sum(p["score"] for p in assigned.values())
        scores[t["name"]] = per
    # surrogate-key families: tables sharing a generated key belong together
    key_tables: Dict[str, List[str]] = {}
    for t in tables:
        for c in t["columns"]:
            if c.get("generated"):
                key_tables.setdefault(c["name"], []).append(t["name"])
    table_entity: Dict[str, Optional[str]] = {}
    for t in tables:
        per = scores[t["name"]]
        best = max(per.values(), default=0.0)
        winners = [e for e, v in per.items() if v == best and v >= MATCH_THRESHOLD]
        table_entity[t["name"]] = winners[0] if len(winners) == 1 else None
    # tie (or no signal): inherit the entity of a table that shares its surrogate key
    for t in tables:
        if table_entity[t["name"]] is None:
            for c in t["columns"]:
                if c.get("generated"):
                    peers = [table_entity[n] for n in key_tables[c["name"]] if n != t["name"] and table_entity.get(n)]
                    if peers:
                        table_entity[t["name"]] = max(set(peers), key=peers.count)
                        break
    entities: Dict[str, Any] = {}
    unassigned: List[str] = []
    for t in tables:
        entity = table_entity[t["name"]]
        if not entity:
            unassigned.append(t["name"])
            continue
        assigned = _assign(t, entity, canonical)
        body = entities.setdefault(entity, {"target_table": [], "fields": [], "table_columns": {}})
        body["target_table"].append(t["name"])
        body["table_columns"][t["name"]] = [c["name"] for c in t["columns"]]
        for col in _columns(t):
            match = assigned.get(col["name"])
            codes = code_lists.get(col.get("code_list") or "", [])
            body["fields"].append({
                "name": f"{t['name']}.{col['name']}", "table": t["name"], "column": col["name"], "type": col["type"],
                "required": bool(col.get("required")), "description": col.get("description", ""),
                "canonical_hint": match["canonical"] if match else None,
                "suggested_canonical_field": None if match else suggest_canonical_name(entity, col["name"], list(canonical.get(entity, {}))),
                "confidence": match["score"] if match else 0.0,
                "evidence": match["evidence"] if match else [],
                "code_list": col.get("code_list") if codes else None, "codes": codes})
    for body in entities.values():
        body["target_table"] = ", ".join(body["target_table"])
    digest = hashlib.sha256(json.dumps({"s": schema, "scope": sorted(scope_tables or []), "cl": include_code_lists},
                                       sort_keys=True).encode()).hexdigest()[:12]
    return {"schema_hash": digest, "target_system": schema.get("system", ""), "entities": entities, "unassigned_tables": unassigned}


_EXPAND = {"ACCT": "ACCOUNT", "CUST": "CLIENT", "CUSTOMER": "CLIENT", "CD": "CODE", "DT": "DATE", "NO": "NUMBER", "NUM": "NUMBER",
           "REF": "ID", "REP": "REP", "HH": "HOUSEHOLD", "GROUP": "HOUSEHOLD"}


def suggest_canonical_name(entity: str, column: str, known_fields: List[str]) -> str:
    """Canonical field name to propose for an unmatched target column (used when raising a change request)."""
    tokens = [_EXPAND.get(t, t) for t in re.split(r"[^A-Za-z0-9]+", column.upper()) if t and t not in {"ON"}]
    if column.upper().endswith("_ON"):
        tokens.append("DATE")
    name = "_".join(tokens)
    first = tokens[0] if tokens else ""
    if first != entity.upper() and not any(k.split("_")[0] == first for k in known_fields):
        name = f"{entity.upper()}_{name}"
    return name


def to_spec(model: Dict[str, Any], status: str) -> Dict[str, Any]:
    """Mapping-view spec used by gap report, proposals and preview."""
    return {"spec_id": f"INFERRED-{model['schema_hash']}", "spec_version": model["schema_hash"], "status": status,
            "target_system": model["target_system"], "entities": model["entities"]}
