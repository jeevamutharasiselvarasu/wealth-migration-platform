"""Evidence-based source-file classification for the canonical entities of the firm configuration.

This is a deterministic, explainable discovery agent, not an auto-approval mechanism.
It uses header semantics and observed record shape; optional dictionary/context is enrichment.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

import yaml

def _split_words(name: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name)).lower()) if w}


def _derived_terms(entity: str, definition: Any) -> dict[str, set]:
    """Header words for an entity taken only from its definition in the firm configuration: its name, its key fields (the identifier
    words) and the names of its other fields (the attributes)."""
    ids = {_token(entity), _token(entity) + "id", _token(entity) + "ref"}
    attrs: set = set()
    for f in definition.fields:
        token = _token(f.name)
        (ids if f.is_key else attrs).add(token)
        if f.is_key:
            ids |= {w for w in _split_words(f.name) if len(w) > 3}
    return {"id": ids, "attributes": attrs - ids}


def entity_terms() -> dict[str, dict[str, set]]:
    """{entity: {"id": words, "attributes": words}}: the data file first, then the firm configuration for any entity it does not list."""
    from control_plane.paths import platform_knowledge_dir
    from control_plane.services.firm_config import FirmConfigError, get_firm_config
    try:
        raw = yaml.safe_load((platform_knowledge_dir() / "entity_terms.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    try:
        cfg = get_firm_config()
        names = list(cfg.entities)
    except FirmConfigError:
        cfg, names = None, list(raw)
    out: dict[str, dict[str, set]] = {}
    for entity in names:
        if entity in raw:
            out[entity] = {k: set(raw[entity].get(k, [])) for k in ("id", "attributes")}
            out[entity]["vocab"] = False  # hand-tuned words: matched as written
        elif cfg is not None:
            out[entity] = {**_derived_terms(entity, cfg.entities[entity]), "vocab": True}  # from the definition: the source vocabulary may translate headers
    return out


RELATIONSHIP_TERMS = {
    "owner", "ownerref", "object", "objectref", "link", "linkkind", "relationship",
    "relationshiptype", "relatedid", "fromid", "toid", "sourceid", "targetid",
    "validfrom", "validto", "startdt", "enddt", "startdate", "enddate",
}
_KEY_SUFFIXES = {"id", "ref", "key", "no", "num", "number", "code", "uid", "guid", "name", "propername", "label"}
GENERIC_ID_TERMS = {"id", "ref", "key", "code", "type", "kind", "status", "active", "updatedat", "validfrom", "validto"}


def _token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _vocab_token(header: Any) -> str:
    """The header with each word replaced by the concept the platform's source vocabulary gives it (patron -> member), so a firm's own
    words reach the canonical terms. With no vocabulary the header is unchanged."""
    from control_plane.services import source_inference as si
    vocab = si._vocab().get("tokens", {})
    words = si._split(header)
    return "".join((vocab.get(w) or [w])[0] for w in words)


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
    header_alts = {header: {header_tokens[header], _vocab_token(header)} for header in headers}  # the header as written, and as the vocabulary reads it
    plain_tokens = set(header_tokens.values())
    all_tokens = set().union(*header_alts.values()) if header_alts else set()
    tokens = plain_tokens
    stats = _value_stats(headers, records)
    relationships = sorted(t for t in tokens if t in RELATIONSHIP_TERMS or any(term in t for term in ("linkkind", "relationship", "validfrom", "validto")))
    endpoint_like = sum(any(mark in t for mark in ("ownerref", "objectref", "fromid", "toid", "sourceid", "targetid")) for t in tokens)
    relation_score = len(relationships) + endpoint_like
    relation_likely = relation_score >= 2 or (endpoint_like >= 1 and any("link" in t or "relationship" in t for t in tokens))

    scores: dict[str, float] = {}
    evidence: dict[str, dict[str, Any]] = {}
    ENTITY_TERMS = entity_terms()
    for entity, categories in ENTITY_TERMS.items():
        use_vocab = bool(categories.get("vocab"))
        tokens = all_tokens if use_vocab else plain_tokens
        alts = (lambda h: header_alts[h]) if use_vocab else (lambda h: {header_tokens[h]})
        # a prefixed column counts as an identifier only when what follows is key-like (householdid, clientref);
        # householdorder or householdrelationshiptype describe membership and are not identifiers of the household
        id_hits = sorted(t for t in tokens if t in categories["id"] or any(t.startswith(alias) and t[len(alias):] in _KEY_SUFFIXES for alias in categories["id"] if len(alias) >= 5))
        attr_hits = sorted(t for t in tokens if t in categories["attributes"])
        # Semantic features are stronger than generic profile features.
        score = 3.0 * len(id_hits) + 1.2 * len(attr_hits)
        unique_id_hits = []
        for header in headers:
            if alts(header) & set(id_hits) and stats[header]["uniqueness_ratio"] >= 0.8 and stats[header]["null_rate"] is not None and stats[header]["null_rate"] <= 0.2:
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
            "sample_profile": {h: stats[h] for h in headers if alts(h) & set(id_hits + attr_hits)},
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
        reasons.append("No strong entity identity evidence was found in the headers and observed profile.")
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
            reasons.append("Distinctive header semantics and a plausible candidate key support this proposal.")
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
