"""Config-driven source-file entity inference.

Entity aliases, field weights, signature requirements and thresholds are maintained
in knowledge/entity_inference_rules.json so the business rules can be updated as
input configuration without editing this service.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "entity_inference_rules.json"


@lru_cache(maxsize=1)
def _load_rules_cached(rules_path: str, modified_ns: int) -> dict[str, Any]:
    path = Path(rules_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("entities"), dict) or not payload["entities"]:
        raise ValueError(f"Entity inference rules have no entities: {path}")
    if not isinstance(payload.get("header_aliases"), dict) or not isinstance(payload.get("decision"), dict):
        raise ValueError(f"Entity inference rules are missing required sections: {path}")
    return payload


def load_entity_inference_rules() -> dict[str, Any]:
    """Load editable inference configuration; reload automatically when its file changes."""
    if not RULES_PATH.is_file():
        raise FileNotFoundError(f"Entity inference configuration not found: {RULES_PATH}")
    return _load_rules_cached(str(RULES_PATH), RULES_PATH.stat().st_mtime_ns)


def normalize_header(header: Any, aliases: dict[str, str] | None = None) -> str:
    rules = aliases if aliases is not None else load_entity_inference_rules()["header_aliases"]
    raw = re.sub(r"[^A-Za-z0-9]+", "_", str(header or "").strip()).strip("_").upper()
    return str(rules.get(raw, raw))


def infer_entity_from_headers(headers: list[str], context: str = "") -> dict[str, Any]:
    """Infer a configured entity from column evidence; return ambiguity instead of guessing."""
    rules = load_entity_inference_rules()
    entities = rules["entities"]
    decision = rules["decision"]
    aliases = rules["header_aliases"]
    canonical = {normalize_header(header, aliases) for header in headers}
    scores: dict[str, int] = {}
    evidence: dict[str, dict[str, Any]] = {}

    for entity, config in entities.items():
        weights = {str(field): int(weight) for field, weight in config.get("field_weights", {}).items()}
        signatures = set(map(str, config.get("signature_fields", [])))
        matched = sorted(field for field in weights if field in canonical)
        score = sum(weights[field] for field in matched)
        signature_hits = sorted(signatures.intersection(canonical))
        scores[entity] = score
        evidence[entity] = {"matched_fields": matched, "signature_fields": signature_hits, "score": score}

    context_text = re.sub(r"[^a-z0-9]+", " ", str(context or "").lower())
    mentioned: list[str] = []
    for entity, config in entities.items():
        terms = [str(term).lower() for term in config.get("context_terms", [])]
        hits = [term for term in terms if term in context_text]
        if hits:
            mentioned.append(entity)
            evidence[entity]["context_terms"] = hits

    if len(mentioned) == 1:
        entity = mentioned[0]
        scores[entity] += int(decision.get("context_bonus", 0))
        evidence[entity]["score"] = scores[entity]

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_entity, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else -1
    has_signature_or_context = bool(evidence[best_entity]["signature_fields"] or evidence[best_entity].get("context_terms"))
    ambiguous = (
        best_score <= int(decision.get("minimum_positive_score", 0))
        or (decision.get("require_unique_top_score", True) and best_score == second_score)
        or (decision.get("require_signature_or_context", True) and not has_signature_or_context)
    )
    messages = rules.get("messages", {})
    if ambiguous:
        return {
            "status": "NEEDS_CLARIFICATION",
            "entity": None,
            "confidence": 0.0,
            "scores": scores,
            "evidence": evidence,
            "message": messages.get("needs_clarification", "Source entity needs clarification."),
        }

    total = sum(scores.values()) or 1
    confidence = round(best_score / total, 3)
    status = "INFERRED" if confidence >= float(decision.get("minimum_confidence", 0.6)) and evidence[best_entity]["signature_fields"] else "NEEDS_REVIEW"
    return {
        "status": status,
        "entity": best_entity,
        "confidence": confidence,
        "scores": scores,
        "evidence": evidence,
        "message": messages.get("inferred", "Entity inferred from source evidence.") if status == "INFERRED" else messages.get("needs_review", "Entity inference requires review."),
    }
