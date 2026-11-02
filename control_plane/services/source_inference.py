"""Infer what unfamiliar SOURCE columns mean (source -> canonical), without needing the column names to be known.

Providers propose; a deterministic gate decides what is allowed through. A provider may be the built-in
heuristic matcher (local, no model), a locally hosted model, or later an Azure-hosted model: they all return the
same proposal shape and are all subject to the same gate. Nothing here approves anything; a human reviews every
proposal. Sample values are only inspected for their shape (date, decimal, ssn-like ...) and are never stored.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set

import yaml

from control_plane.services import target_inference

SRC_VOCAB_PATH = Path(__file__).resolve().parents[2] / "platform_knowledge" / "source_vocabulary.yaml"
MATCH_THRESHOLD = 0.3
_cache: Optional[Dict[str, Any]] = None
_TYPE_GROUPS = {"string": "text", "date": "date", "integer": "number", "number": "number", "decimal": "number", "boolean": "bool"}


def _vocab() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        base = target_inference._vocab()
        extra = yaml.safe_load(SRC_VOCAB_PATH.read_text(encoding="utf-8"))
        _cache = {"weak": set(base.get("weak", [])), "stop": set(extra.get("stop", [])),
                  "tokens": {**base["tokens"], **extra["tokens"]}, "shapes": extra.get("shapes", {}),
                  "value_hints": extra.get("value_hints", {})}
    return _cache


def _split(text: str) -> List[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or ""))          # camelCase
    return [t for t in re.split(r"[^A-Za-z0-9]+|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", text.lower()) if t]


def concepts(*texts: str, weight: float = 1.0) -> Dict[str, float]:
    v = _vocab()
    out: Dict[str, float] = {}
    for text in texts:
        for tok in _split(text):
            if tok in v["stop"] or tok.isdigit():
                continue
            for c in v["tokens"].get(tok, [tok]):
                out[c] = max(out.get(c, 0.0), (0.5 if c in v["weak"] else 1.0) * weight)
    return out


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%b-%Y", "%Y%m%d", "%m/%d/%y")


def _is_date(s: str) -> bool:
    for f in _DATE_FORMATS:
        try:
            datetime.strptime(s, f)
            return True
        except ValueError:
            pass
    return False


def _shape(s: str) -> str:
    if _is_date(s):
        return "date"
    if re.fullmatch(r"\d{3}-?\d{2}-?\d{4}", s):
        return "ssn"
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s):
        return "email"
    if re.fullmatch(r"\+?[\d\-\s().]{10,}", s) and sum(c.isdigit() for c in s) >= 10 and "-" in s + " ":
        return "phone"
    if re.fullmatch(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", s):
        return "guid"
    if re.fullmatch(r"-?\d+\.\d+", s):
        return "decimal"
    if re.fullmatch(r"-?\d+", s):
        return "zip" if re.fullmatch(r"\d{5}", s) else "integer"
    if re.fullmatch(r"[A-Za-z]{2}", s):
        return "state2"
    return "text"


def profile_column(values: List[Any]) -> Dict[str, Any]:
    """Shape of a column's sample values. Only the shape and a few lowercase tokens are kept, never the values."""
    vals = [str(v).strip() for v in values if v is not None and str(v).strip() != ""]
    if not vals:
        return {"kind": "empty", "shapes": {}, "blob": "", "non_blank": 0}
    shapes: Dict[str, int] = {}
    for v in vals:
        shapes[_shape(v)] = shapes.get(_shape(v), 0) + 1
    top = max(shapes, key=shapes.get)
    kind = {"date": "date", "decimal": "number", "integer": "number", "zip": "number"}.get(top, "text")
    if top == "guid":
        kind = "text"
    return {"kind": kind, "shapes": {k: round(n / len(vals), 2) for k, n in shapes.items()},
            "top": top, "blob": " ".join(vals).lower(), "non_blank": len(vals),
            "avg_len": round(sum(len(v) for v in vals) / len(vals), 1), "has_space": any(" " in v for v in vals)}


def _type_ok(profile: Dict[str, Any], canonical_type: str) -> bool:
    kind, group = profile["kind"], _TYPE_GROUPS.get(canonical_type or "string", "text")
    if kind == "empty":
        return True
    if group == "date":
        return kind == "date"
    if group == "number":
        return kind == "number"
    return kind != "date"  # text fields accept text and id-like numbers, never date-shaped columns


def _value_evidence(profile: Dict[str, Any], cname: str, ccon: Dict[str, float], description: str = "") -> (float, List[str]):
    v = _vocab()
    bonus, ev = 0.0, []
    top = profile.get("top")
    key = "tax_id" if {"tax", "id"} <= set(ccon) else None
    for concept, shape in v["shapes"].items():
        probe = key if concept == "tax_id" else concept
        if probe and (probe in ccon) and top == shape:
            bonus += 0.3
            ev.append(f"values look like {shape}")
    for concept, words in v["value_hints"].items():
        if concept in ccon and any(w in profile.get("blob", "") for w in words):
            bonus += 0.3
            ev.append(f"sample values mention {concept} terms")
    guid_field = "guid" in description.lower()
    if guid_field:  # a GUID-style identifier is only a good fit for GUID-shaped values
        bonus += 0.2 if top == "guid" else -0.15
        if top == "guid":
            ev.append("values look like GUIDs")
    elif top == "guid":
        bonus -= 0.1
    if "id" in ccon and "name" not in ccon and profile.get("has_space"):  # identifiers rarely contain spaces
        bonus -= 0.3
    if "name" in ccon and "code" not in ccon and profile.get("has_space"):
        bonus += 0.1
        ev.append("values read like names")
    if "code" in ccon and profile.get("kind") == "text" and not profile.get("has_space") and profile.get("avg_len", 99) <= 10:
        bonus += 0.05
    return bonus, ev


class InferenceProvider(Protocol):
    name: str

    def propose(self, entity: str, columns: Dict[str, Dict[str, Any]], canonical: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """columns: {source_column: profile}; canonical: {field: {"type", "description", "is_key"}}.
        Return [{source_field, canonical_field, confidence, evidence, provider}], any number per column."""


class HeuristicProvider:
    """Local, deterministic matcher: vocabulary concepts of names and descriptions plus the shape of sample values."""
    name = "heuristic"

    def propose(self, entity, columns, canonical):
        out = []
        for col, prof in columns.items():
            col_c = concepts(col)
            for cname, meta in canonical.items():
                can_c = concepts(cname)
                desc_c = concepts(cname, meta.get("description", ""))
                name_sim = target_inference._similarity(col_c, can_c)
                desc_sim = 0.0
                if col_c:
                    desc_sim = 0.5 * sum(1 for k in col_c if k in desc_c and k not in can_c) / len(col_c)
                bonus, ev = _value_evidence(prof, cname, can_c, meta.get("description", ""))
                base = max(name_sim, desc_sim)
                if base <= 0 and bonus < 0.3:
                    continue
                score = max(0.0, min(1.0, base + bonus + (0.02 if meta.get("is_key") else 0.0)))
                out.append({"source_field": col, "canonical_field": cname, "confidence": round(score, 3),
                            "evidence": sorted(set(col_c) & set(desc_c)) + ev, "provider": self.name})
        return out


def gate(entity: str, proposals: List[Dict[str, Any]], columns: Dict[str, Dict[str, Any]],
         canonical: Dict[str, Dict[str, Any]], threshold: float = MATCH_THRESHOLD) -> List[Dict[str, Any]]:
    """Deterministic checks every provider's output must pass: a real canonical field, a compatible value shape,
    a minimum confidence, and one source column per canonical field (best score first)."""
    ok = []
    for p in proposals:
        meta = canonical.get(p["canonical_field"])
        if meta is None or p["source_field"] not in columns:
            continue
        if p["confidence"] < threshold or not _type_ok(columns[p["source_field"]], meta.get("type", "string")):
            continue
        ok.append(p)
    ok.sort(key=lambda p: -p["confidence"])
    cols: Set[str] = set()
    fields: Set[str] = set()
    result = []
    for p in ok:
        if p["source_field"] in cols or p["canonical_field"] in fields:
            continue
        cols.add(p["source_field"])
        fields.add(p["canonical_field"])
        result.append(p)
    return result


def canonical_from_config(entity: str, cfg=None) -> Dict[str, Dict[str, Any]]:
    from control_plane.services.firm_config import get_firm_config
    cfg = cfg or get_firm_config()
    return {f.name: {"type": f.type, "description": f.description or "", "is_key": bool(getattr(f, "is_key", False))}
            for f in cfg.entities[entity].fields}


def infer_source_mappings(entity: str, rows: List[Dict[str, Any]], canonical: Optional[Dict[str, Dict[str, Any]]] = None,
                          providers: Optional[List[InferenceProvider]] = None) -> List[Dict[str, Any]]:
    canonical = canonical or canonical_from_config(entity)
    columns = {c: profile_column([r.get(c) for r in rows]) for c in (rows[0].keys() if rows else [])}
    proposals: List[Dict[str, Any]] = []
    for provider in providers or [HeuristicProvider()]:
        proposals.extend(provider.propose(entity, columns, canonical))
    return gate(entity, proposals, columns, canonical)
