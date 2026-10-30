"""Deterministic inference of governed transformation rules from business-rule text.

Business rules written as plain sentences or Markdown bullets are matched against a fixed set of allow-listed
patterns. A match yields a PROPOSED rule of an existing registered type with validated parameters; it is saved
as a DRAFT that a Migration Analyst or Platform Admin must approve before any mapping can use it. Nothing is
evaluated as code, and a sentence that does not clearly match a pattern is reported, never guessed.
"""
import hashlib
import re
import sqlite3
from typing import Any, Dict, List, Optional

from control_plane.services import transformation_rules as tr

MAX_STATEMENTS = 50
_Q = r"[\"'“‘]?"
_DATE_TOKENS = sorted(tr.DATE_FORMATS, key=len, reverse=True)
_SEPARATORS = {"space": " ", "comma": ",", "dash": "-", "hyphen": "-", "underscore": "_", "slash": "/", "colon": ":", "nothing": ""}
_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": None}


def split_statements(text: str) -> List[str]:
    out = []
    for line in str(text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line.strip()).strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out[:MAX_STATEMENTS]


def _clean(v: str) -> str:
    return v.strip().strip("\"'“”‘’ ").strip()


def _fields(text: str) -> List[str]:
    parts = [p for p in re.split(r"\s*(?:,|\band\b|\+)\s*", text) if p.strip()]
    return [re.sub(r"\s+", "_", _clean(p)).upper() for p in parts]


def _default(s: str) -> Optional[Dict[str, Any]]:
    m = (re.search(rf"\bdefault(?:s)?\s+(?:to\s+)?{_Q}(.+?){_Q}\s+(?:if|when)\s+(?:the\s+)?(?:value\s+is\s+)?(?:blank|empty|missing|null)", s, re.I)
         or re.search(rf"\b(?:if|when)\s+(?:the\s+)?(?:value\s+is\s+)?(?:blank|empty|missing|null)[, ]+\s*(?:use|set\s+to|default\s+to)\s+{_Q}(.+?){_Q}\s*\.?$", s, re.I))
    return {"type": "DEFAULT_IF_BLANK", "params": {"default": _clean(m.group(1))}} if m else None


def _concat(s: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\b(?:concatenate|concat|combine|join)\s+(.+?)(?:\s+(?:with|using|separated\s+by)\s+(.+?))?\s*\.?$", s, re.I)
    if not m:
        return None
    sep = " "
    if m.group(2):
        raw = m.group(2).strip()
        lit = re.search(r"[\"'“‘](.*?)[\"'”’]", raw)
        word = re.search(r"\b(space|comma|dash|hyphen|underscore|slash|colon|nothing)\b", raw, re.I)
        if lit:
            sep = lit.group(1)
        elif word:
            sep = _SEPARATORS[word.group(1).lower()]
        else:
            return None
    return {"type": "CONCAT", "params": {"sources": _fields(m.group(1)), "separator": sep}}


def _split(s: str) -> Optional[Dict[str, Any]]:
    m = re.search(rf"\bsplit\b.*?\b(?:on|by|at)\s+(?:a\s+|the\s+)?(?:delimiter\s+)?[\"'“‘](.+?)[\"'”’].*?\b(?:take|keep|use)\s+(?:the\s+)?(?:part\s+|piece\s+)?(first|second|third|fourth|\d+)(?:\s+(?:part|piece))?", s, re.I)
    if not m:
        return None
    which = m.group(2).lower()
    index = _ORDINALS[which] if which in _ORDINALS else int(which) - 1
    return {"type": "SPLIT_PART", "params": {"delimiter": m.group(1), "index": index}} if index is not None and index >= 0 else None


def _date(s: str) -> Optional[Dict[str, Any]]:
    if not re.search(r"\bdate", s, re.I):
        return None
    found = [t for t in _DATE_TOKENS if t in s.upper()]
    # drop a shorter token only when it is just a substring of a longer one that was also found
    found = [t for t in found if not any(t != o and t in o for o in found)]
    return {"type": "DATE_FORMAT", "params": {"input_formats": found}} if found else None


def _number(s: str) -> Optional[Dict[str, Any]]:
    if not re.search(r"\b(number|numeric|amount|currency|decimal|strip|remove)\b", s, re.I):
        return None
    dec = re.search(r"(\d+)\s+decimal", s, re.I)
    chars = "".join(dict.fromkeys(ch for ch in "$£€%," if ch in s))
    if not dec and not chars and not re.search(r"currency|symbol", s, re.I):
        return None
    return {"type": "NUMBER_CLEAN", "params": {"strip_chars": chars or "$,", "decimals": int(dec.group(1)) if dec else 2,
                                               "parentheses_negative": not re.search(r"\bnot?\s+(?:treat\s+)?parenthes", s, re.I)}}


def _if_equals(s: str) -> Optional[Dict[str, Any]]:
    m = (re.search(rf"\breplace\s+{_Q}(.+?){_Q}\s+with\s+{_Q}(.*?){_Q}\s*\.?$", s, re.I)
         or re.search(rf"\b(?:if|when)\s+(?:the\s+)?value\s+(?:is|equals|=)\s+{_Q}(.+?){_Q}\s*(?:,|then)?\s*(?:then\s+)?(?:use|set\s+to|replace\s+with|becomes?)\s+{_Q}(.*?){_Q}\s*\.?$", s, re.I))
    return {"type": "IF_EQUALS", "params": {"when_equals": _clean(m.group(1)), "then": _clean(m.group(2)), "else": None}} if m else None


def _value_map(s: str) -> Optional[Dict[str, Any]]:
    body = re.sub(r"^\s*(?:map|translate|convert)\s+(?:the\s+)?(?:codes?\s+|values?\s+)?", "", s, flags=re.I)
    explicit = bool(re.search(r"->|=>|→", s)) or re.match(r"^\s*(?:map|translate|convert)\b", s, re.I)
    if not explicit:
        return None
    pairs: Dict[str, str] = {}
    for chunk in re.split(r"[;,]|\band\b(?=\s+\S+\s*(?:->|=>|→|\bto\b))", body):
        m = re.match(rf"^\s*{_Q}([^\"'“‘]+?){_Q}\s*(?:->|=>|→|\bto\b|\bas\b)\s*{_Q}([^\"'”’]+?){_Q}\s*\.?\s*$", chunk, re.I)
        if m:
            pairs[_clean(m.group(1))] = _clean(m.group(2))
    return {"type": "VALUE_MAP", "params": {"map": pairs, "case_insensitive": True}} if pairs else None


_MATCHERS = (_default, _concat, _split, _date, _number, _if_equals, _value_map)


def infer(text: str) -> List[Dict[str, Any]]:
    """One result per statement: a validated proposal or an explicit 'not recognised'."""
    results = []
    for stmt in split_statements(text):
        hit = None
        for matcher in _MATCHERS:
            hit = matcher(stmt)
            if hit:
                break
        if not hit:
            results.append({"statement": stmt, "recognised": False, "reason": "No supported pattern matched; write it as a rule or create it manually."})
            continue
        try:
            params = tr.validate_params(hit["type"], hit["params"])
        except ValueError as exc:
            results.append({"statement": stmt, "recognised": False, "reason": f"Matched {tr.RULE_TYPES[hit['type']]['label']} but it is invalid: {exc}"})
            continue
        results.append({"statement": stmt, "recognised": True, "rule_type": hit["type"], "params": params,
                        "explanation": f"{tr.RULE_TYPES[hit['type']]['label']}: {tr.RULE_TYPES[hit['type']]['help']}"})
    return results


def rule_id_for(rule_type: str, params: Dict[str, Any]) -> str:
    digest = hashlib.sha1((rule_type + repr(sorted(params.items(), key=lambda kv: kv[0]))).encode()).hexdigest()[:6].upper()
    return f"INF_{rule_type}_{digest}"[:41]


def save_drafts(conn: sqlite3.Connection, text: str, actor: str) -> Dict[str, Any]:
    """Create DRAFT rule versions for recognised statements. Identical live rules are not duplicated."""
    created, skipped, unrecognised = [], [], []
    for r in infer(text):
        if not r["recognised"]:
            unrecognised.append(r)
            continue
        rid = rule_id_for(r["rule_type"], r["params"])
        live = [v for v in tr.list_rules(conn) if v["rule_id"] == rid and v["status"] in ("DRAFT", "APPROVED")]
        if live:
            skipped.append({"statement": r["statement"], "rule": live[-1]["expression"]})
            continue
        desc = f"From business rule: \"{r['statement']}\". Inferred as {r['explanation']}"
        rule = tr.create_rule_version(conn, rid, r["rule_type"], desc[:900], r["params"], actor)
        created.append({"statement": r["statement"], "rule": rule["expression"]})
    return {"created": created, "skipped": skipped, "unrecognised": unrecognised}
