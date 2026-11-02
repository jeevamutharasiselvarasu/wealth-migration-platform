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

# Tokens in a source column name that support a canonical field. Name alone is never enough.
NAME_HINTS: Dict[str, set] = {
    "ACCT_NUM": {"ACCT", "ACCOUNT"},
    "OPEN_DATE": {"OPEN", "OPENED", "OPENING"},
    "REG_TYPE": {"REG", "REGISTRATION"},
    "HH_ID": {"GROUP", "HOUSEHOLD", "HH"},
    "HH_NAME": {"GROUP", "HOUSEHOLD", "HH"},
    "CLIENT_ID": {"CLIENT", "PARTY", "CUSTOMER"},
    "LEGAL_NAME": {"NAME", "NM", "LEGAL", "DISPLAY"},
    "CLIENT_TYPE": {"TYPE", "KIND", "CLASS"},
    "TAX_ID": {"TAX", "TIN", "SSN", "EIN"},
}
ID_TOKENS = {"REF", "ID", "NUM", "NO", "KEY", "NUMBER"}
NAME_TOKENS = {"LABEL", "NAME", "NM", "TITLE"}
KEY_FIELDS = {"ACCT_NUM", "HH_ID", "CLIENT_ID"}

REG_VOCAB = {"IRA", "ROTH", "ROTH_IRA", "TOD", "JOINT", "INDIVIDUAL", "TRUST", "UTMA", "SEP_IRA", "JTWROS"}
CLIENT_TYPE_VOCAB = {"INDIVIDUAL", "INDV", "TRUST", "CORP", "CORPORATION", "LLC", "JOINT", "ENTITY", "ESTATE"}
VOCABULARIES = {"REG_TYPE": REG_VOCAB, "CLIENT_TYPE": CLIENT_TYPE_VOCAB}
TAX_ID_PATTERN = re.compile(r"^(\d{3}-?\d{2}-?\d{4}|\d{2}-?\d{7})$")
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
    if field in KEY_FIELDS:
        if len(values) == total_rows and len(set(values)) == len(values):
            return ("values are unique and non-null", True)
        return None
    if field == "OPEN_DATE":
        return ("all values parse as dates", True) if all(_is_date(v) for v in values) else None
    if field in VOCABULARIES:
        share = sum(1 for v in values if v.upper() in VOCABULARIES[field]) / len(values)
        if share >= 0.6:
            return (f"{share:.0%} of values are known {field} vocabulary", share >= 0.8)
        return None
    if field == "TAX_ID":
        return ("all values match a tax-identifier pattern", True) if all(TAX_ID_PATTERN.match(v) for v in values) else None
    if field in {"HH_NAME", "LEGAL_NAME"}:
        textual = all(re.search(r"[A-Za-z]", v) and " " in v for v in values)
        return ("values are multi-word text", False) if textual else None
    return None


def _name_hint(field: str, column: str) -> Optional[str]:
    tokens = _tokens(column)
    hits = tokens & NAME_HINTS.get(field, set())
    if not hits:
        return None
    if field in KEY_FIELDS and not (tokens & ID_TOKENS):
        return None
    if field == "HH_NAME" and not (tokens & NAME_TOKENS):
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
