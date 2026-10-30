"""Governed, versioned transformation rules (source-to-canonical, P0 entities).

Rules are declarative: a rule type plus validated parameters. Nothing is evaluated as code.
A rule version is immutable once created; a human approves each version before a mapping may
reference it as ``RULE:<RULE_ID>@<VERSION>`` (pinned, so lineage stays reproducible).
"""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from control_plane.db import get_db_connection

RULE_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")
EXPRESSION_PATTERN = re.compile(r"^RULE:([A-Z][A-Z0-9_]{2,40})@(\d+)$")
FIELD_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# Allow-listed input date formats (strptime tokens). Output is always ISO YYYY-MM-DD.
DATE_FORMATS = {
    "MM/DD/YYYY": "%m/%d/%Y", "DD/MM/YYYY": "%d/%m/%Y", "YYYY-MM-DD": "%Y-%m-%d",
    "MM-DD-YYYY": "%m-%d-%Y", "DD-MON-YYYY": "%d-%b-%Y", "YYYYMMDD": "%Y%m%d", "MM/DD/YY": "%m/%d/%y",
}
NUMBER_STRIP_ALLOWED = set("$,£€ %")
MAX_MAP_ENTRIES = 500

RULE_TYPES: Dict[str, Dict[str, str]] = {
    "VALUE_MAP": {"label": "Value map (lookup)", "help": "Map coded source values to canonical values. Unlisted values pass through unchanged and are reported.",
                  "example": '{"map": {"SCH": "SCHWAB"}, "case_insensitive": true}'},
    "DATE_FORMAT": {"label": "Date to ISO", "help": "Parse one or more declared source date formats to YYYY-MM-DD.",
                    "example": '{"input_formats": ["MM/DD/YYYY", "YYYYMMDD"]}'},
    "NUMBER_CLEAN": {"label": "Number cleanup", "help": "Strip currency/grouping symbols, treat (x) as negative, round to fixed decimals.",
                     "example": '{"strip_chars": "$,", "decimals": 2, "parentheses_negative": true}'},
    "CONCAT": {"label": "Concatenate fields", "help": "Join several source fields of the same record. The mapped source field is the anchor.",
               "example": '{"sources": ["FIRST_NAME", "LAST_NAME"], "separator": " "}'},
    "SPLIT_PART": {"label": "Split and take part", "help": "Split a value on a delimiter and keep one part (0-based).",
                   "example": '{"delimiter": " ", "index": 0}'},
    "DEFAULT_IF_BLANK": {"label": "Default if blank", "help": "Use a documented default only when the source value is empty.",
                         "example": '{"default": "UNKNOWN"}'},
    "IF_EQUALS": {"label": "Conditional replace", "help": "Replace a value when it equals a given value; otherwise keep it (or use the else value).",
                  "example": '{"when_equals": "N/A", "then": "", "else": null}'},
}

_DDL = """
CREATE TABLE IF NOT EXISTS transformation_rules (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    rule_type TEXT NOT NULL,
    description TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_by TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_utc TEXT,
    review_comments TEXT,
    PRIMARY KEY (rule_id, version)
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def validate_params(rule_type: str, params: Any) -> Dict[str, Any]:
    """Return normalized params or raise ValueError. Only registered types and shapes pass."""
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Unsupported rule type '{rule_type}'.")
    if not isinstance(params, dict):
        raise ValueError("Parameters must be a JSON object.")
    if rule_type == "VALUE_MAP":
        mapping = params.get("map")
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("VALUE_MAP needs a non-empty 'map' object.")
        if len(mapping) > MAX_MAP_ENTRIES:
            raise ValueError(f"VALUE_MAP is limited to {MAX_MAP_ENTRIES} entries.")
        ci = bool(params.get("case_insensitive", True))
        norm: Dict[str, str] = {}
        for key, val in mapping.items():
            k = _text(key).strip()
            k = k.upper() if ci else k
            if not k:
                raise ValueError("VALUE_MAP source values must not be blank.")
            if k in norm and norm[k] != _text(val):
                raise ValueError(f"VALUE_MAP has conflicting targets for '{k}'.")
            norm[k] = _text(val)
        return {"map": norm, "case_insensitive": ci}
    if rule_type == "DATE_FORMAT":
        formats = params.get("input_formats")
        if not isinstance(formats, list) or not formats:
            raise ValueError("DATE_FORMAT needs 'input_formats'.")
        bad = [f for f in formats if f not in DATE_FORMATS]
        if bad:
            raise ValueError(f"Unsupported date format(s): {', '.join(map(str, bad))}. Allowed: {', '.join(DATE_FORMATS)}.")
        return {"input_formats": list(dict.fromkeys(formats))}
    if rule_type == "NUMBER_CLEAN":
        strip = _text(params.get("strip_chars", "$,"))
        if any(ch not in NUMBER_STRIP_ALLOWED for ch in strip):
            raise ValueError("strip_chars may only contain: $ , £ € % or space.")
        decimals = params.get("decimals", 2)
        if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 8:
            raise ValueError("decimals must be an integer from 0 to 8.")
        return {"strip_chars": strip, "decimals": decimals, "parentheses_negative": bool(params.get("parentheses_negative", True))}
    if rule_type == "CONCAT":
        sources = params.get("sources")
        if not isinstance(sources, list) or len(sources) < 2 or len(sources) > 6:
            raise ValueError("CONCAT needs 2 to 6 source field names.")
        cleaned = [_text(s).strip().upper() for s in sources]
        if any(not FIELD_PATTERN.match(s) for s in cleaned):
            raise ValueError("CONCAT source fields must look like FIELD_NAME (letters, digits, underscore).")
        sep = _text(params.get("separator", " "))
        if len(sep) > 5:
            raise ValueError("separator may be at most 5 characters.")
        return {"sources": cleaned, "separator": sep}
    if rule_type == "SPLIT_PART":
        delim = _text(params.get("delimiter"))
        index = params.get("index", 0)
        if not delim or len(delim) > 5:
            raise ValueError("SPLIT_PART needs a delimiter of 1 to 5 characters.")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 20:
            raise ValueError("index must be an integer from 0 to 20.")
        return {"delimiter": delim, "index": index}
    if rule_type == "DEFAULT_IF_BLANK":
        if "default" not in params or _text(params["default"]).strip() == "":
            raise ValueError("DEFAULT_IF_BLANK needs a non-empty documented 'default'.")
        return {"default": _text(params["default"])}
    # IF_EQUALS
    if "when_equals" not in params or "then" not in params:
        raise ValueError("IF_EQUALS needs 'when_equals' and 'then'.")
    else_val = params.get("else")
    return {"when_equals": _text(params["when_equals"]), "then": _text(params["then"]),
            "else": None if else_val is None else _text(else_val)}


def apply_rule(rule_type: str, params: Dict[str, Any], value: Any, record: Optional[Dict[str, Any]] = None) -> Tuple[Any, Optional[str]]:
    """Apply a validated rule. Returns (new_value, warning). Never raises on bad data: it warns and keeps the value."""
    blank = value is None or _text(value).strip() == ""
    if rule_type == "DEFAULT_IF_BLANK":
        return (params["default"], None) if blank else (value, None)
    if rule_type == "CONCAT":
        rec = {str(k).strip().upper(): v for k, v in (record or {}).items()}
        parts = [_text(rec.get(s)).strip() for s in params["sources"]]
        missing = [s for s, p in zip(params["sources"], parts) if not p]
        joined = params["separator"].join(p for p in parts if p)
        return joined, (f"blank source field(s): {', '.join(missing)}" if missing else None)
    if blank:
        return value, None
    text = _text(value)
    if rule_type == "VALUE_MAP":
        key = text.strip().upper() if params["case_insensitive"] else text.strip()
        if key in params["map"]:
            return params["map"][key], None
        return value, f"unmapped value '{text}'"
    if rule_type == "DATE_FORMAT":
        from datetime import datetime as dt
        for name in params["input_formats"]:
            try:
                return dt.strptime(text.strip(), DATE_FORMATS[name]).strftime("%Y-%m-%d"), None
            except ValueError:
                continue
        return value, f"date '{text}' matches none of the declared formats"
    if rule_type == "NUMBER_CLEAN":
        from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
        cleaned = text.strip()
        negative = False
        if params["parentheses_negative"] and cleaned.startswith("(") and cleaned.endswith(")"):
            negative, cleaned = True, cleaned[1:-1]
        for ch in params["strip_chars"]:
            cleaned = cleaned.replace(ch, "")
        cleaned = cleaned.strip()
        try:
            number = Decimal(cleaned)
        except InvalidOperation:
            return value, f"'{text}' is not a number after cleanup"
        if negative:
            number = -number
        quant = Decimal(1).scaleb(-params["decimals"])
        return str(number.quantize(quant, rounding=ROUND_HALF_UP)), None
    if rule_type == "SPLIT_PART":
        parts = text.split(params["delimiter"])
        if params["index"] < len(parts):
            return parts[params["index"]].strip(), None
        return value, f"no part {params['index']} after splitting '{text}'"
    if rule_type == "IF_EQUALS":
        if text.strip() == params["when_equals"]:
            return params["then"], None
        return (value if params["else"] is None else params["else"]), None
    raise ValueError(f"Unsupported rule type '{rule_type}'.")


def parse_expression(expression: str) -> Optional[Tuple[str, int]]:
    match = EXPRESSION_PATTERN.match(str(expression or "").strip().upper())
    return (match.group(1), int(match.group(2))) if match else None


def make_expression(rule_id: str, version: int) -> str:
    return f"RULE:{rule_id}@{int(version)}"


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["params"] = json.loads(data.pop("params_json"))
    data["expression"] = make_expression(data["rule_id"], data["version"])
    return data


def list_rules(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    ensure_table(conn)
    rows = conn.execute("SELECT * FROM transformation_rules ORDER BY rule_id, version DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_rule(conn: sqlite3.Connection, rule_id: str, version: int) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM transformation_rules WHERE rule_id = ? AND version = ?", (rule_id, version)).fetchone()
    return _row_to_dict(row) if row else None


def create_rule_version(conn: sqlite3.Connection, rule_id: str, rule_type: str, description: str,
                        params: Any, created_by: str) -> Dict[str, Any]:
    """Create a DRAFT version (version 1, or the next version of an existing rule id)."""
    ensure_table(conn)
    rule_id = _text(rule_id).strip().upper()
    if not RULE_ID_PATTERN.match(rule_id):
        raise ValueError("Rule id must be 3-41 characters: capital letters, digits and underscores, starting with a letter.")
    if not _text(description).strip():
        raise ValueError("A description of the business reason is required.")
    normalized = validate_params(rule_type, params)
    existing = conn.execute("SELECT rule_type, MAX(version) AS v FROM transformation_rules WHERE rule_id = ?", (rule_id,)).fetchone()
    version = 1
    if existing and existing["v"]:
        if existing["rule_type"] != rule_type:
            raise ValueError(f"{rule_id} already exists as {existing['rule_type']}; a rule keeps its type across versions.")
        version = int(existing["v"]) + 1
    conn.execute(
        "INSERT INTO transformation_rules (rule_id, version, rule_type, description, params_json, status, created_by, created_utc) "
        "VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?)",
        (rule_id, version, rule_type, description.strip(), json.dumps(normalized, sort_keys=True), created_by, _now()),
    )
    return get_rule(conn, rule_id, version)


def decide_rule(conn: sqlite3.Connection, rule_id: str, version: int, decision: str, reviewer: str, comments: str) -> Dict[str, Any]:
    """APPROVED / REJECTED for a DRAFT version. Approved versions are immutable."""
    rule = get_rule(conn, rule_id, version)
    if rule is None:
        raise LookupError("Rule version not found.")
    if rule["status"] != "DRAFT":
        raise ValueError(f"{rule_id}@{version} is already {rule['status']}.")
    if decision not in {"APPROVED", "REJECTED"}:
        raise ValueError("Decision must be APPROVED or REJECTED.")
    if decision == "REJECTED" and not _text(comments).strip():
        raise ValueError("A rejection reason is required.")
    conn.execute(
        "UPDATE transformation_rules SET status = ?, reviewed_by = ?, reviewed_utc = ?, review_comments = ? "
        "WHERE rule_id = ? AND version = ? AND status = 'DRAFT'",
        (decision, reviewer, _now(), _text(comments).strip(), rule_id, version),
    )
    return get_rule(conn, rule_id, version)


def retire_rule(conn: sqlite3.Connection, rule_id: str, version: int, reviewer: str) -> Dict[str, Any]:
    rule = get_rule(conn, rule_id, version)
    if rule is None:
        raise LookupError("Rule version not found.")
    if rule["status"] != "APPROVED":
        raise ValueError("Only an approved version can be retired.")
    in_use = conn.execute(
        "SELECT COUNT(*) FROM canonical_mappings WHERE is_confirmed = 1 AND UPPER(transformation_expression) = ?",
        (make_expression(rule_id, version),),
    ).fetchone()[0]
    if in_use:
        raise ValueError(f"{rule_id}@{version} is used by {in_use} approved mapping(s); replace those mappings first.")
    conn.execute("UPDATE transformation_rules SET status = 'RETIRED', reviewed_by = ?, reviewed_utc = ? WHERE rule_id = ? AND version = ?",
                 (reviewer, _now(), rule_id, version))
    return get_rule(conn, rule_id, version)


def approved_rule_for_expression(expression: str) -> Optional[Dict[str, Any]]:
    """Resolve a pinned RULE expression to an APPROVED version, or None."""
    parsed = parse_expression(expression)
    if not parsed:
        return None
    conn = get_db_connection()
    try:
        rule = get_rule(conn, parsed[0], parsed[1])
        conn.commit()
    finally:
        conn.close()
    return rule if rule and rule["status"] == "APPROVED" else None


def approved_rules() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        rules = [r for r in list_rules(conn) if r["status"] == "APPROVED"]
        conn.commit()
    finally:
        conn.close()
    return rules


def preview(rule_type: str, params: Any, samples: List[str], record: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Dry-run a rule on sample values. Used before and after approval; changes nothing."""
    normalized = validate_params(rule_type, params)
    out = []
    for sample in samples[:25]:
        rec = dict(record or {})
        if rule_type == "CONCAT" and not rec:
            pieces = [p.strip() for p in str(sample).split("|")]
            rec = {s: pieces[i] if i < len(pieces) else "" for i, s in enumerate(normalized["sources"])}
        result, warning = apply_rule(rule_type, normalized, sample, rec)
        out.append({"input": sample, "output": result, "warning": warning})
    return out
