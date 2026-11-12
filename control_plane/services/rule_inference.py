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


# A statement that says where a target field comes from (a mapping choice, not a transformation):
#   Target Fund Name comes from Account CustodianName
#   Target Open/Closed comes from Account ClosedDate using the rule "If ClosedDate is NULL, then Open Else Closed"
_HINT = re.compile(r"^\s*target\s+(?P<target>.+?)\s+comes\s+from\s+(?P<entity>[A-Za-z]+)\s+(?P<source>[A-Za-z_]\w*)"
                   r"(?:\s+using\s+the\s+rule\s+[\"'“‘](?P<rule>.+?)[\"'”’])?\s*\.?\s*$", re.I)


def parse_hint(statement: str) -> Optional[Dict[str, Any]]:
    m = _HINT.match(statement or "")
    if not m:
        return None
    label = re.sub(r"[^A-Za-z0-9]+", "_", m.group("target")).strip("_").upper()
    return {"statement": statement.strip(), "target_field": label, "entity": m.group("entity").capitalize(),
            "source_field": m.group("source"), "rule_statement": (m.group("rule") or "").strip()}


def split_hints(text: str) -> List[Dict[str, Any]]:
    return [h for h in (parse_hint(s) for s in split_statements(text)) if h]


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
    if not pairs:
        return None
    params: Dict[str, Any] = {"map": pairs, "case_insensitive": True}
    # "...anything else is rejected / fails": a code outside the list must stop the record, not pass through
    if re.search(r"\b(?:reject(?:ed|s)?|fail(?:s|ed)?|errors?|not\s+allowed|must\s+be\s+one\s+of)\b|\bno\s+default\b", s, re.I):
        params["strict"] = True
    return {"type": "VALUE_MAP", "params": params}


def _if_blank(s: str) -> Optional[Dict[str, Any]]:
    """'If ClosedDate is NULL, then Open Else Closed' -> IF_BLANK."""
    m = re.search(rf"\b(?:if|when)\s+(?:the\s+)?[\w .]+?\s+(?:is|are)\s+(?:null|blank|empty|missing)\s*,?\s*(?:then\s+)?(?:use\s+|set\s+to\s+)?{_Q}(.+?){_Q}\s*[,;]?\s*(?:else|otherwise)\s+(?:use\s+|set\s+to\s+)?{_Q}(.+?){_Q}\s*\.?$", s, re.I)
    return {"type": "IF_BLANK", "params": {"when_blank": _clean(m.group(1)), "when_filled": _clean(m.group(2))}} if m else None


_DELIMS = {"comma": ",", "dash": " - ", "hyphen": " - ", "slash": "/", "colon": ":", "semicolon": ";", "space": " ", "pipe": "|"}
_WORD = "comma|dash|hyphen|slash|colon|semicolon|space|pipe"


def _split_words(s: str) -> Optional[Dict[str, Any]]:
    """'Take the part before the comma', 'the text after ' - '', 'remove everything after ' - '' -> SPLIT_PART."""
    strip = re.search(rf"\b(?:remove|strip|drop|trim)\s+(?:everything|the\s+(?:suffix|text|part))\s+(?:after|from)\s+(?:the\s+)?(?:first\s+)?(?:[\"'“‘](.+?)[\"'”’]|({_WORD}))", s, re.I)
    if strip:
        return {"type": "SPLIT_PART", "params": {"delimiter": strip.group(1) or _DELIMS[strip.group(2).lower()], "index": 0}}
    lit = re.search(r"\b(before|after)\s+(?:the\s+)?(?:first\s+)?[\"'“‘](.+?)[\"'”’]", s, re.I)
    if lit:
        return {"type": "SPLIT_PART", "params": {"delimiter": lit.group(2), "index": 0 if lit.group(1).lower() == "before" else 1}}
    word = re.search(rf"\b(before|after)\s+(?:the\s+|a\s+)?(?:first\s+)?({_WORD})\b", s, re.I)
    if word and re.search(r"\b(take|use|keep|parse|extract|part|text|name)\b", s, re.I):
        return {"type": "SPLIT_PART", "params": {"delimiter": _DELIMS[word.group(2).lower()], "index": 0 if word.group(1).lower() == "before" else 1}}
    return None


def _stem(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\.[A-Za-z0-9]+$", "", str(name)).lower())


def _lookup(s: str, relationships: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """'<Target> is the <Col> of the <File> file, joined through <ChildCol>'. The parent file and key come only from a CONFIRMED
    relationship (each item has child and parent as 'file.column'); with none that matches, the sentence is not recognised."""
    cue = re.search(r"\b(?:look\s*up|lookup|joined?\s+(?:through|via|on|using)|taken\s+from|comes?\s+from|fetch(?:ed)?)\b", s, re.I)
    col = re.search(r"\bthe\s+([A-Za-z][\w ]*?)\s+(?:column\s+)?(?:of|from|in)\s+(?:the\s+)?(\w+)\s+(?:file|table)", s, re.I)
    if not cue or not col or not relationships:
        return None
    ret = col.group(1).strip()
    want = _stem(col.group(2))
    via = re.search(r"\b(?:through|via|on|using)\s+(?:the\s+)?(\w+)", s, re.I)
    via_stem = _stem(via.group(1)) if via else ""
    for rel in relationships:
        pfile, _, pcol = str(rel["parent"]).rpartition(".")
        ccol = str(rel["child"]).rpartition(".")[2]
        pst = _stem(pfile)
        if not (pst == want or pst.rstrip("s").endswith(want.rstrip("s"))):
            continue
        if via_stem and via_stem != _stem(ccol):
            continue
        return {"type": "LOOKUP", "params": {"parent_file": pfile, "parent_key": pcol, "return_column": ret}}
    return None


_MATCHERS = (_default, _if_blank, _concat, _split, _split_words, _date, _number, _if_equals, _value_map)


def meaning(rule_type: str, p: Dict[str, Any]) -> str:
    """One plain-English sentence saying exactly what the validated rule will do (shown beside each business-rule line)."""
    if rule_type == "VALUE_MAP":
        pairs = list(p["map"].items())
        shown = ", ".join(f"{k} becomes {v}" for k, v in pairs[:4]) + (f" and {len(pairs) - 4} more" if len(pairs) > 4 else "")
        return f"Looks each source value up and replaces it: {shown}. Any other value passes through unchanged and is reported."
    if rule_type == "DATE_FORMAT":
        return f"Reads dates written as {', '.join(p['input_formats'])} and writes them as YYYY-MM-DD. A date in any other layout is reported."
    if rule_type == "NUMBER_CLEAN":
        return f"Removes {p['strip_chars'] or 'no'} characters, treats (x) as negative if enabled, and rounds to {p['decimals']} decimals."
    if rule_type == "CONCAT":
        return f"Joins {', '.join(p['sources'])} with '{p['separator']}' (blank parts are skipped and reported)."
    if rule_type == "SPLIT_PART":
        return f"Splits on '{p['delimiter']}' and keeps part {p['index'] + 1}."
    if rule_type == "DEFAULT_IF_BLANK":
        return f"Uses '{p['default']}' only when the source value is empty."
    if rule_type == "IF_BLANK":
        return f"Writes '{p['when_blank']}' when the source value is empty, otherwise '{p['when_filled']}'."
    if rule_type == "LOOKUP":
        return f"Uses the mapped value as the key and returns {p['return_column']} from {p['parent_file']} where {p['parent_key']} matches. No match, or a blank key, is reported."
    return f"Replaces '{p['when_equals']}' with '{p['then']}'; other values are kept."


def infer(text: str, relationships: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """One result per statement: a validated proposal or an explicit 'not recognised'."""
    results = []
    for stmt in split_statements(text):
        if parse_hint(stmt):
            continue  # a mapping choice, read by target mapping (split_hints), not a transformation rule
        hit = _lookup(stmt, relationships or [])
        for matcher in (() if hit else _MATCHERS):
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
                        "explanation": f"{tr.RULE_TYPES[hit['type']]['label']}: {tr.RULE_TYPES[hit['type']]['help']}",
                        "meaning": meaning(hit["type"], params)})
    return results


def rule_id_for(rule_type: str, params: Dict[str, Any]) -> str:
    digest = hashlib.sha1((rule_type + repr(sorted(params.items(), key=lambda kv: kv[0]))).encode()).hexdigest()[:6].upper()
    return f"INF_{rule_type}_{digest}"[:41]


def save_drafts(conn: sqlite3.Connection, text: str, actor: str, source_label: str = "", relationships: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Create DRAFT rule versions for recognised statements. Identical live rules are not duplicated."""
    created, skipped, unrecognised = [], [], []
    for r in infer(text, relationships):
        if not r["recognised"]:
            unrecognised.append(r)
            continue
        rid = rule_id_for(r["rule_type"], r["params"])
        live = [v for v in tr.list_rules(conn) if v["rule_id"] == rid and v["status"] in ("DRAFT", "APPROVED")]
        if live:
            skipped.append({"statement": r["statement"], "rule": live[-1]["expression"]})
            continue
        origin = f" ({source_label})" if source_label else ""
        desc = f"From business rule{origin}: \"{r['statement']}\". Inferred as {r['explanation']}"
        rule = tr.create_rule_version(conn, rid, r["rule_type"], desc[:900], r["params"], actor)
        created.append({"statement": r["statement"], "rule": rule["expression"]})
    return {"created": created, "skipped": skipped, "unrecognised": unrecognised}
