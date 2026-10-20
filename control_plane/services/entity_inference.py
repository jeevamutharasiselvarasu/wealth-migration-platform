"""Conservative source-file entity inference for the XODUS three-entity P0 pilot."""
from __future__ import annotations
import re
from typing import Any

ALIASES = {
    "ACCOUNT_NUMBER": "ACCT_NUM", "ACCOUNT_NO": "ACCT_NUM", "ACCT_NUMBER": "ACCT_NUM",
    "ACCT_NUM": "ACCT_NUM", "ACCOUNT_ID": "ACCT_NUM", "LEGACY_ACCT_NO": "ACCT_NUM",
    "CUSTODIAN": "CUSTODIAN_CODE", "CUSTODIAN_CODE": "CUSTODIAN_CODE",
    "REP_CODE": "REP_CODE", "ADVISOR_REP_NUM": "REP_CODE", "PRIMARY_REP_NUM": "REP_CODE",
    "REG_TYPE": "REG_TYPE", "REGISTRATION_TYPE": "REG_TYPE", "REGISTRATION_SRC_TYPE": "REG_TYPE",
    "OPEN_DATE": "OPEN_DATE", "ACCOUNT_OPEN_DT": "OPEN_DATE",
    "HH_ID": "HH_ID", "HOUSEHOLD_ID": "HH_ID", "HOUSEHOLD_LINK_ID": "HH_ID",
    "HOUSEHOLD_NAME": "HH_NAME", "HH_NAME": "HH_NAME", "HH_TITLE": "HH_NAME",
    "CLIENT_ID": "CLIENT_ID", "LEGACY_CLIENT_ID": "CLIENT_ID", "PARTY_SRC_ID": "CLIENT_ID",
    "LEGAL_NAME": "LEGAL_NAME", "FULL_LEGAL_NAME": "LEGAL_NAME", "CLIENT_NAME": "LEGAL_NAME",
    "PARTY_NAME": "LEGAL_NAME", "FIRST_NAME": "FIRST_NAME", "CLIENT_FIRST_NAME": "FIRST_NAME",
    "LAST_NAME": "LAST_NAME", "CLIENT_LAST_NAME": "LAST_NAME",
    "CLIENT_TYPE": "CLIENT_TYPE", "CLIENT_TYPE_RAW": "CLIENT_TYPE", "CLIENT_TYPE_SRC": "CLIENT_TYPE",
    "TAX_ID": "TAX_ID", "TAX_IDENTIFIER": "TAX_ID", "SSN_TIN": "TAX_ID",
    "EMAIL": "EMAIL", "EMAIL_ADDR": "EMAIL", "PRIMARY_EMAIL": "EMAIL",
}
ENTITY_FIELDS = {
    "Account": {"ACCT_NUM": 3, "CUSTODIAN_CODE": 2, "REG_TYPE": 1, "OPEN_DATE": 1, "REP_CODE": 1},
    "Household": {"HH_ID": 3, "HH_NAME": 3, "REP_CODE": 1},
    "Client": {"CLIENT_ID": 3, "LEGAL_NAME": 3, "CLIENT_TYPE": 2, "TAX_ID": 1, "FIRST_NAME": 1, "LAST_NAME": 1, "EMAIL": 1},
}
SIGNATURES = {
    "Account": {"ACCT_NUM", "CUSTODIAN_CODE"},
    "Household": {"HH_ID", "HH_NAME"},
    "Client": {"CLIENT_ID", "LEGAL_NAME", "CLIENT_TYPE"},
}

def normalize_header(header: Any) -> str:
    raw = re.sub(r"[^A-Za-z0-9]+", "_", str(header or "").strip()).strip("_").upper()
    return ALIASES.get(raw, raw)

def infer_entity_from_headers(headers: list[str], context: str = "") -> dict[str, Any]:
    """Infer likely entity using canonical field evidence; return ambiguous instead of guessing."""
    canonical = {normalize_header(header) for header in headers}
    scores = {}
    evidence = {}
    for entity, weights in ENTITY_FIELDS.items():
        matched = sorted(field for field in weights if field in canonical)
        score = sum(weights[field] for field in matched)
        signature_hits = sorted(SIGNATURES[entity].intersection(canonical))
        scores[entity] = score
        evidence[entity] = {"matched_fields": matched, "signature_fields": signature_hits, "score": score}
    # Explicit source-purpose / dictionary notes can disambiguate headers, but cannot override
    # contradictory field evidence without a review state.
    context_text = re.sub(r"[^a-z0-9]+", " ", str(context or "").lower())
    context_terms = {
        "Account": ("account", "custodian", "registration"),
        "Household": ("household", "family group"),
        "Client": ("client", "party", "person master"),
    }
    mentioned = [entity for entity, terms in context_terms.items()
                 if any(term in context_text for term in terms)]
    if len(mentioned) == 1:
        entity = mentioned[0]
        scores[entity] += 2
        evidence[entity]["context_terms"] = [term for term in context_terms[entity] if term in context_text]
        evidence[entity]["score"] = scores[entity]
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_entity, best_score = ranked[0]
    second_score = ranked[1][1]
    # Require a positive score, a unique winner, and at least one identity signature.
    if best_score <= 0 or best_score == second_score or not evidence[best_entity]["signature_fields"]:
        return {"status": "NEEDS_CLARIFICATION", "entity": None, "confidence": 0.0,
                "scores": scores, "evidence": evidence,
                "message": "The columns do not uniquely identify Account, Household or Client. Provide a data dictionary or explain the key fields."}
    total = sum(scores.values()) or 1
    confidence = round(best_score / total, 3)
    status = "INFERRED" if confidence >= 0.6 else "NEEDS_REVIEW"
    return {"status": status, "entity": best_entity, "confidence": confidence,
            "scores": scores, "evidence": evidence,
            "message": ("Entity inferred from column names and canonical field evidence." if status == "INFERRED"
                        else "A likely entity was detected, but the evidence is mixed; review before mapping.")}
