"""Evidence-based wealth source-file classification for the XODUS P0 entities.

This is a deterministic, explainable discovery agent, not an auto-approval mechanism.
It uses header semantics and observed record shape; optional dictionary/context is enrichment.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

ENTITY_TERMS = {
    "Account": {
        "id": {"account", "acct", "accountref", "acctref", "accountnumber", "acctnum", "acctno", "accountid", "accountkey"},
        "attributes": {"registration", "regcode", "regtype", "openedon", "opendate", "custodian", "status", "repcode", "accounttype"},
    },
    "Household": {
        "id": {"household", "householdid", "hhid", "groupref", "groupid", "familygroup", "householdref"},
        "attributes": {"householdname", "hhname", "grouplabel", "groupname", "leadref", "reviewdate"},
    },
    "Client": {
        "id": {"client", "clientid", "clientref", "party", "partyref", "partyid", "personid", "individualid"},
        "attributes": {"legalname", "fullname", "displaytxt", "displayname", "givenname", "givennm", "firstname", "surname", "lastname", "email", "emailtxt", "taxid", "clienttype", "recordkind", "activeflag", "channelcd", "updatedat"},
    },
}
RELATIONSHIP_TERMS = {
    "owner", "ownerref", "object", "objectref", "link", "linkkind", "relationship",
    "relationshiptype", "relatedid", "fromid", "toid", "sourceid", "targetid",
    "validfrom", "validto", "startdt", "enddt", "startdate", "enddate",
}
GENERIC_ID_TERMS = {"id", "ref", "key", "code", "type", "kind", "status", "active", "updatedat", "validfrom", "validto"}


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _value_stats(headers: list[str], records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    columns = {}
    for header in headers:
        values = [str(row.get(header, "") or "").strip() for row in records]
        nonempty = [v for v in values if v]
        columns[header] = {
            "non_null_count": len(nonempty),
            "null_rate": round((total - len(nonempty)) / total, 3) if total else None,
            "unique_count": len(set(nonempty)),
            "uniqueness_ratio": round(len(set(nonempty)) / len(nonempty), 3) if nonempty else 0.0,
            "sample_values": nonempty[:3],
            "inferred_type": (
                "boolean" if nonempty and all(v.lower() in {"true", "false", "yes", "no", "y", "n", "0", "1"} for v in nonempty)
                else "date" if nonempty and all(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", v) for v in nonempty)
                else "integer" if nonempty and all(re.match(r"^-?\d+$", v) for v in nonempty)
                else "numeric" if nonempty and all(re.match(r"^-?\d+(\.\d+)?$", v) for v in nonempty)
                else "string" if nonempty else "empty"
            ),
        }
    return columns


def classify_source_file(headers: list[str], records: list[dict[str, Any]] | None = None, context: str = "") -> dict[str, Any]:
    """Return candidate, confidence, evidence, competing candidates, review reasons and profile."""
    records = records or []
    header_tokens = {header: _token(header) for header in headers}
    tokens = set(header_tokens.values())
    stats = _value_stats(headers, records)
    relationships = sorted(t for t in tokens if t in RELATIONSHIP_TERMS or any(term in t for term in ("linkkind", "relationship", "validfrom", "validto")))
    endpoint_like = sum(any(mark in t for mark in ("ownerref", "objectref", "fromid", "toid", "sourceid", "targetid")) for t in tokens)
    relation_score = len(relationships) + endpoint_like
    relation_likely = relation_score >= 2 or (endpoint_like >= 1 and any("link" in t or "relationship" in t for t in tokens))

    scores: dict[str, float] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for entity, categories in ENTITY_TERMS.items():
        id_hits = sorted(t for t in tokens if t in categories["id"] or any(t.startswith(alias) and len(t) > len(alias) for alias in categories["id"] if len(alias) >= 5))
        attr_hits = sorted(t for t in tokens if t in categories["attributes"])
        # Semantic features are stronger than generic profile features.
        score = 3.0 * len(id_hits) + 1.2 * len(attr_hits)
        unique_id_hits = []
        for header in headers:
            token = header_tokens[header]
            if token in id_hits and stats[header]["uniqueness_ratio"] >= 0.8 and stats[header]["null_rate"] is not None and stats[header]["null_rate"] <= 0.2:
                unique_id_hits.append(header)
                score += 0.8
        if context:
            context_token = _token(context)
            if entity.lower() in context.lower():
                score += 0.5
        scores[entity] = round(score, 2)
        evidence[entity] = {
            "matched_fields": id_hits + attr_hits,
            "signature_fields": id_hits,
            "unique_candidate_key_fields": unique_id_hits,
            "sample_profile": {h: stats[h] for h in headers if header_tokens[h] in id_hits + attr_hits},
            "score": round(score, 2),
        }

    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    best, best_score = ranked[0]
    second_score = ranked[1][1]
    reasons = []
    if relation_likely:
        candidate = "Relationship"
        status = "RELATIONSHIP_REVIEW"
        confidence = min(0.97, 0.55 + 0.06 * relation_score)
        reasons.append("Columns indicate relationship/link grain or paired endpoints; do not treat as an entity master.")
    elif best_score < 3.0:
        candidate = None
        status = "NEEDS_CLARIFICATION"
        confidence = 0.0
        reasons.append("No strong wealth-entity identity evidence was found in the headers and observed profile.")
    elif best_score - second_score < 1.0:
        candidate = best
        status = "NEEDS_REVIEW"
        confidence = round(best_score / max(1.0, sum(scores.values())), 3)
        reasons.append("The leading entity is too close to a competing candidate for automatic classification.")
    else:
        candidate = best
        confidence = round(best_score / max(1.0, sum(scores.values())), 3)
        has_key = bool(evidence[best]["unique_candidate_key_fields"])
        status = "INFERRED" if has_key and best_score >= 4.0 else "NEEDS_REVIEW"
        if not has_key:
            reasons.append("Entity semantics are present, but no sufficiently complete, unique candidate key was observed.")
        if status == "INFERRED":
            reasons.append("Distinctive wealth-management header semantics and a plausible candidate key support this proposal.")
    if not records:
        reasons.append("No sample rows were available; classification relies on column names only.")
    if candidate == "Relationship":
        competing = ranked[:3]
    else:
        competing = ranked[1:]
    return {
        "status": status,
        "entity": candidate if candidate in ENTITY_TERMS else None,
        "classification_type": "RELATIONSHIP" if candidate == "Relationship" else ("MASTER_ENTITY" if candidate else "UNKNOWN"),
        "proposed_classification": candidate,
        "confidence": confidence,
        "scores": scores,
        "evidence": evidence,
        "matched_evidence": evidence.get(candidate, {}) if candidate in evidence else {"relationship_fields": relationships, "endpoint_like_fields": endpoint_like},
        "competing_candidates": [{"entity": name, "score": score} for name, score in competing],
        "review_reasons": reasons,
        "profile": {"row_count": len(records), "column_count": len(headers), "columns": stats},
        "approval_status": "PENDING",
        "message": "Classification is a proposal only; human review is required before approval or mapping.",
    }
