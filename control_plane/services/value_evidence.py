"""Deterministic value-and-name evidence for proposing source-to-canonical mappings.

Proposals only: they stay PENDING, carry a capped confidence, name their evidence, and are
never auto-approved. A concept without enough evidence is left unresolved, never guessed.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional

MAX_EVIDENCE_CONFIDENCE = 0.60
BASE_EVIDENCE_CONFIDENCE = 0.50
STRONG_EVIDENCE_BONUS = 0.10
EVIDENCE_REVIEW_THRESHOLD = 0.70  # below this a proposal needs individual review, never bulk approval

def _evidence_data() -> Dict:
    """Words, vocabularies and patterns (platform_knowledge/value_evidence.yaml); none are built in."""
    import yaml
    from control_plane.paths import platform_knowledge_dir
    try:
        return yaml.safe_load((platform_knowledge_dir() / "value_evidence.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _config_fields() -> tuple:
    """(key fields, date fields) of the canonical entities, from the firm configuration."""
    from control_plane.services.firm_config import FirmConfigError, get_firm_config
    try:
        cfg = get_firm_config()
    except FirmConfigError:
        return set(), set()
    keys = {f.name for d in cfg.entities.values() for f in d.fields if f.is_key}
    dates = {f.name for d in cfg.entities.values() for f in d.fields if f.type == "date"}
    return keys, dates


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d")


def _tokens(column: str) -> set:
    return {t for t in re.split(r"[^A-Za-z0-9]+", str(column).upper()) if t}


def _values(raw: List) -> List[str]:
    return [str(v).strip() for v in raw if v is not None and str(v).strip() != ""]


def _is_date(value: str) -> bool:
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def _value_evidence(field: str, column: str, values: List[str], total_rows: int) -> Optional[tuple]:
    """Return (description, is_strong) when the values support the canonical field, else None."""
    if not values:
        return None
    data = _evidence_data()
    keys, dates = _config_fields()
    if field in keys:
        if len(values) == total_rows and len(set(values)) == len(values):
            return ("values are unique and non-null", True)
        return None
    if field in dates:
        return ("all values parse as dates", True) if all(_is_date(v) for v in values) else None
    vocab = {k: {str(x).upper() for x in v} for k, v in (data.get("vocabularies") or {}).items()}
    if field in vocab:
        share = sum(1 for v in values if v.upper() in vocab[field]) / len(values)
        if share >= 0.6:
            return (f"{share:.0%} of values are known {field} vocabulary", share >= 0.8)
        return None
    pattern = (data.get("patterns") or {}).get(field)
    if pattern:
        return (f"all values match the {field} pattern", True) if all(re.match(pattern, v) for v in values) else None
    if field in set(data.get("text_fields") or []):
        textual = all(re.search(r"[A-Za-z]", v) and " " in v for v in values)
        return ("values are multi-word text", False) if textual else None
    return None


def _name_hint(field: str, column: str) -> Optional[str]:
    data = _evidence_data()
    keys, _dates = _config_fields()
    tokens = _tokens(column)
    hits = tokens & {str(x).upper() for x in (data.get("name_hints") or {}).get(field, [])}
    if not hits:
        return None
    if field in keys and not (tokens & {str(x).upper() for x in data.get("id_tokens") or []}):
        return None
    if field in set(data.get("needs_name_token") or []) and not (tokens & {str(x).upper() for x in data.get("name_tokens") or []}):
        return None
    return "/".join(sorted(hits))


def propose_from_evidence(needed_fields: List[str], columns: Dict[str, List], total_rows: int) -> List[dict]:
    """Propose at most one column per needed canonical field; ties and ambiguity yield no proposal."""
    candidates: Dict[str, List[dict]] = {}
    for field in needed_fields:
        for column, raw in columns.items():
            hint = _name_hint(field, column)
            evidence = _value_evidence(field, column, _values(raw), total_rows) if hint else None
            if not hint or not evidence:
                continue
            description, strong = evidence
            confidence = BASE_EVIDENCE_CONFIDENCE + (STRONG_EVIDENCE_BONUS if strong else 0.0)
            candidates.setdefault(field, []).append({
                "source_field": column, "canonical_field": field,
                "confidence": round(min(confidence, MAX_EVIDENCE_CONFIDENCE), 2),
                "evidence": f"name token {hint}; {description}",
            })
    proposals: List[dict] = []
    claimed: set = set()
    for field, found in candidates.items():
        found.sort(key=lambda c: -c["confidence"])
        if len(found) > 1 and found[0]["confidence"] == found[1]["confidence"]:
            continue  # ambiguous: more than one equally supported column
        best = found[0]
        if best["source_field"] in claimed:
            continue
        claimed.add(best["source_field"])
        proposals.append(best)
    return proposals
