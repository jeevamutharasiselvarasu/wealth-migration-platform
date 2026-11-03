"""Suggest governed transformation rules from what the source data actually looks like.

Everything here is a recommendation. A suggestion either points at a rule that already exists (from the business-rules
file or a reviewer) and fits the column, or proposes a NEW draft rule of an allow-listed type with the evidence that led
to it. Lookup targets the model cannot know are left blank for a person to supply; nothing is created, approved or
executed here, and no arbitrary code is involved.
"""
import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from control_plane.services import transformation_rules as tr

MAX_VALUES = 500
MAX_LOOKUP_DISTINCT = 30
_ID_HINT = re.compile(r"(?:^|_)(ID|NO|NUM|NUMBER|EMAIL|NAME|PHONE|ADDR|ADDRESS|SSN|TAX_?ID|ZIP|POSTAL)(?:_|$)|_ID$|_NO$", re.I)
_LOOKUP_HINT = re.compile(r"CUSTODIAN|TYPE|STATUS|CODE|REGISTRATION|RELATIONSHIP|CLASS|CATEGORY|SRC|SOURCE", re.I)
_DATE_HINT = re.compile(r"DATE|(?:^|_)(DT|DOB|OPEN|CLOSED|DELETED)(?:_|$)", re.I)
_NUM_RE = re.compile(r"^\(?[$£€]?\s*-?[\d,]*\.?\d+\)?%?$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(values: List[Any]) -> List[str]:
    return [str(v).strip() for v in values[:MAX_VALUES] if v is not None and str(v).strip() != ""]


def _parses(fmt: str, text: str) -> bool:
    try:
        datetime.strptime(text, tr.DATE_FORMATS[fmt])
        return True
    except ValueError:
        return False


def date_formats_for(values: List[str]) -> List[str]:
    """Allow-listed formats that parse EVERY value, in registry order (ambiguous layouts list all that fit)."""
    if not values:
        return []
    return [f for f in tr.DATE_FORMATS if all(_parses(f, v) for v in values)]


def _is_numeric_with_symbols(values: List[str]) -> Optional[Dict[str, Any]]:
    if not values or not all(_NUM_RE.match(v) for v in values):
        return None
    symbols = "".join(dict.fromkeys(ch for ch in "$£€%," if any(ch in v for v in values)))
    paren = any(v.startswith("(") and v.endswith(")") for v in values)
    if not symbols and not paren:
        return None
    decimals = max((len(v.split(".")[1].rstrip(")%")) if "." in v else 0) for v in values)
    return {"strip_chars": symbols, "decimals": max(2, min(decimals, 8)), "parentheses_negative": True}


def sug_rule_id(prefix: str, rule_type: str, field: str, params: Dict[str, Any]) -> str:
    digest = hashlib.sha1((rule_type + field + repr(sorted(params.items(), key=lambda kv: kv[0]))).encode()).hexdigest()[:5].upper()
    base = re.sub(r"[^A-Z0-9]+", "_", field.upper()).strip("_")[:18] or "FIELD"
    return f"{prefix}_{base}_{digest}"[:41]


def _crosswalk_for(field: str, crosswalks: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    f = field.upper()
    if "CUSTODIAN" in f:
        return crosswalks.get("custodian", {})
    if "REGIST" in f:
        return crosswalks.get("registration", {})
    if "TYPE" in f:
        return crosswalks.get("client_type", {})
    return {}


def _rule_origin(rule: Dict[str, Any]) -> str:
    return "business-rules file" if str(rule.get("description", "")).startswith("From business rule") else "governed rule"


def _existing_fit(col: Dict[str, Any], values: List[str], rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rtype, params = rule["rule_type"], rule["params"]
    distinct = list(dict.fromkeys(values))
    if rtype == "VALUE_MAP":
        key = (lambda v: v.upper()) if params.get("case_insensitive", True) else (lambda v: v)
        hit = [v for v in distinct if key(v) in params["map"]]
        if hit and len(hit) * 2 >= len(distinct) and len(distinct) <= MAX_LOOKUP_DISTINCT:
            miss = [v for v in distinct if v not in hit]
            ev = f"{rule['expression']} maps {len(hit)} of {len(distinct)} observed values ({', '.join(hit[:5])})."
            if miss:
                ev += f" Not covered: {', '.join(miss[:5])} - they would pass through and be reported."
            return {"evidence": ev, "uncovered": miss}
    elif rtype == "DATE_FORMAT":
        if (_DATE_HINT.search(col["field"]) or date_formats_for(values)) and values:
            ok = [v for v in distinct if tr.apply_rule(rtype, params, v)[1] is None]
            if ok and len(ok) == len(distinct) and not all(_ISO.match(v) for v in distinct):
                return {"evidence": f"{rule['expression']} parses all {len(distinct)} observed date values to ISO.", "uncovered": []}
    elif rtype == "NUMBER_CLEAN":
        if _is_numeric_with_symbols(values):
            ok = [v for v in distinct if tr.apply_rule(rtype, params, v)[1] is None]
            if len(ok) == len(distinct):
                return {"evidence": f"{rule['expression']} cleans all {len(distinct)} observed numeric values.", "uncovered": []}
    return None


def suggest(columns: List[Dict[str, Any]], rules: List[Dict[str, Any]], crosswalks: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """columns: [{entity, file, field, values}]. rules: list_rules() dicts (any status). Returns suggestions, strongest first."""
    live = [r for r in rules if r["status"] in ("DRAFT", "APPROVED")]
    live_ids = {r["rule_id"] for r in live}
    out: List[Dict[str, Any]] = []
    seen_fields = set()
    files: Dict[str, List[str]] = {}
    for col in columns:
        files.setdefault(col["file"], []).append(col["field"].upper())
    for col in columns:
        values = _clean(col.get("values", []))
        field, key = col["field"], (col["entity"], col["file"], col["field"])
        if not values or key in seen_fields:
            continue
        seen_fields.add(key)
        base = {"entity": col["entity"], "file": col["file"], "source_field": field, "sample": list(dict.fromkeys(values))[:4]}
        distinct = list(dict.fromkeys(values))
        found = False
        # 1. An existing rule (business-rules file or reviewer-authored) that fits this column.
        for rule in live:
            fit = _existing_fit(col, values, rule)
            if fit:
                found = True
                out.append({**base, "kind": "EXISTING", "rule_type": rule["rule_type"], "expression": rule["expression"], "status": rule["status"],
                            "origin": _rule_origin(rule), "evidence": fit["evidence"], "needs_input": len(fit["uncovered"]), "uncovered": fit["uncovered"]})
        if found:
            continue
        # 2. A new rule proposed from the data pattern.
        fmts = date_formats_for(distinct) if (_DATE_HINT.search(field) or date_formats_for(distinct)) else []
        if fmts and not all(_ISO.match(v) for v in distinct):
            params = tr.validate_params("DATE_FORMAT", {"input_formats": fmts})
            note = f" {', '.join(fmts[1:])} would also fit; confirm the layout." if len(fmts) > 1 else ""
            rid = sug_rule_id("SUG", "DATE_FORMAT", field, params)
            out.append({**base, "kind": "NEW", "rule_type": "DATE_FORMAT", "rule_id": rid, "params": params, "exists": rid in live_ids, "origin": "data pattern",
                        "evidence": f"All {len(distinct)} observed values parse as {fmts[0]}; output is ISO YYYY-MM-DD.{note}", "needs_input": 1 if len(fmts) > 1 else 0})
            continue
        numeric = _is_numeric_with_symbols(distinct)
        if numeric:
            params = tr.validate_params("NUMBER_CLEAN", numeric)
            rid = sug_rule_id("SUG", "NUMBER_CLEAN", field, params)
            out.append({**base, "kind": "NEW", "rule_type": "NUMBER_CLEAN", "rule_id": rid, "params": params, "exists": rid in live_ids, "origin": "data pattern",
                        "evidence": f"Values carry symbols or grouping ({numeric['strip_chars'] or 'parentheses'}); cleaned to {params['decimals']} decimals.", "needs_input": 0})
            continue
        lookup_like = (_LOOKUP_HINT.search(field) or (len(distinct) <= 12 and len(values) >= 3 * len(distinct))) and not _ID_HINT.search(field)
        if lookup_like and 2 <= len(distinct) <= MAX_LOOKUP_DISTINCT:
            cross = _crosswalk_for(field, crosswalks)
            entries = []
            for v in distinct:
                target = cross.get(v.upper(), "")
                entries.append({"value": v, "target": target, "known": bool(target), "count": values.count(v)})
            known = sum(e["known"] for e in entries)
            out.append({**base, "kind": "NEW", "rule_type": "VALUE_MAP", "rule_id": sug_rule_id("SUG", "VALUE_MAP", field, {"v": tuple(distinct)}),
                        "entries": entries, "origin": "data pattern", "exists": False, "needs_input": len(entries) - known,
                        "evidence": f"{len(distinct)} distinct coded values; {known} match a known crosswalk. Supply the canonical value for the rest (blank values stay unmapped and are reported)."})
    # 3. Concatenation when a file carries name parts but no full name.
    for fname, cols in files.items():
        first = next((c for c in cols if re.search(r"FIRST", c)), None)
        last = next((c for c in cols if re.search(r"LAST|SURNAME", c)), None)
        if first and last and not any(c in ("LEGAL_NAME", "FULL_NAME", "CLIENT_NAME") for c in cols):
            mid = next((c for c in cols if re.search(r"MIDDLE", c)), None)
            sources = [c for c in (first, mid, last) if c]
            entity = next((c["entity"] for c in columns if c["file"] == fname), "")
            params = tr.validate_params("CONCAT", {"sources": sources, "separator": " "})
            rid = sug_rule_id("SUG", "CONCAT", "LEGAL_NAME", params)
            out.append({"entity": entity, "file": fname, "source_field": first, "sample": [], "kind": "NEW", "rule_type": "CONCAT", "rule_id": rid,
                        "params": params, "exists": rid in live_ids, "origin": "data pattern", "needs_input": 0,
                        "evidence": f"{fname} has {', '.join(sources)} but no full-name column; LEGAL_NAME is derived by joining them."})
    order = {"EXISTING": 0, "NEW": 1}
    out.sort(key=lambda s: (order[s["kind"]], s["needs_input"], s["entity"], s["source_field"]))
    return out
