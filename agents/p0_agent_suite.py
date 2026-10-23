"""P0 agent-assisted analysis for the CoreCap -> Simplicity demo.

These agents produce evidence-backed recommendations only. They do not approve mappings,
transform source values, load a target, or assign migration waves.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from control_plane.services.entity_inference import infer_entity_from_headers
from agents.discovery.schema_mapper_agent import SchemaMapperAgent

P0_ENTITIES = ("Account", "Household", "Client")


class SourceClassificationAgent:
    name = "Source Classification Agent"

    def run(self, headers: list[str], context: str = "", in_scope: list[str] | None = None) -> dict[str, Any]:
        result = infer_entity_from_headers(headers, context=context)
        allowed = set(in_scope or P0_ENTITIES)
        if result.get("entity") and result["entity"] not in allowed:
            return {**result, "status": "OUT_OF_SCOPE", "proposed_entity": result["entity"], "entity": None,
                    "message": "The inferred entity is not selected for this migration; excluded from downstream work."}
        return result


class ProfilingAgent:
    name = "Profiling & Grain Analysis Agent"

    def run(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        headers = list(records[0].keys()) if records else []
        columns = []
        for field in headers:
            values = [row.get(field) for row in records]
            nonempty = [v for v in values if v is not None and str(v).strip() != ""]
            distinct = {str(v) for v in nonempty}
            columns.append({
                "field": field,
                "row_count": len(records),
                "null_count": len(records) - len(nonempty),
                "null_rate": round((len(records) - len(nonempty)) / len(records), 3) if records else 0.0,
                "distinct_count": len(distinct),
                "sample_values": [str(v) for v in nonempty[:3]],
            })
        likely_keys = [c["field"] for c in columns if records and c["distinct_count"] == len(records) and c["null_count"] == 0]
        warnings = []
        if not records:
            warnings.append("No records available to profile.")
        if len(likely_keys) > 1:
            warnings.append("Multiple fields are unique in this sample; record grain/key needs confirmation.")
        if not likely_keys and records:
            warnings.append("No non-null unique field detected; do not invent a canonical key.")
        return {
            "agent": self.name, "status": "PROFILED" if records else "NO_RECORDS",
            "row_count": len(records), "column_count": len(headers), "columns": columns,
            "candidate_key_fields": likely_keys, "grain_assessment": "SAMPLE_LEVEL_ONLY",
            "warnings": warnings,
        }


class SchemaSemanticMappingAgent:
    name = "Schema & Semantic Mapping Agent"

    def __init__(self) -> None:
        self.catalog = SchemaMapperAgent.DEFAULT_CATALOG

    def run(self, entity: str, headers: list[str], in_scope: list[str]) -> dict[str, Any]:
        if entity not in P0_ENTITIES or entity not in in_scope:
            return {"agent": self.name, "status": "OUT_OF_SCOPE", "entity": entity, "proposals": []}
        proposals, unresolved = [], []
        seen_targets: set[str] = set()
        for source_field in headers:
            canonical_field = self.catalog.get(source_field)
            if not canonical_field:
                unresolved.append({"source_field": source_field, "disposition": "UNRESOLVED",
                                   "reason": "No configured source-to-canonical alias; SME decision required."})
                continue
            collision = canonical_field in seen_targets
            proposals.append({
                "source_field": source_field, "canonical_field": canonical_field,
                "recommendation": "PROPOSED_REQUIRES_REVIEW",
                "evidence": "Configured source-field alias in the mapping catalog.",
                "confidence": "RULE_MATCH",
                "risk": "POTENTIAL_COLLISION" if collision else "REVIEW_BUSINESS_SEMANTICS",
                "approved": False,
            })
            seen_targets.add(canonical_field)
        return {"agent": self.name, "status": "RECOMMENDATIONS_READY", "entity": entity,
                "proposals": proposals, "unresolved_fields": unresolved,
                "notice": "Suggestions are not approved mappings; no source values were transformed."}


class ReviewReadinessAgent:
    name = "Review & Validation Agent"

    def run(self, classification: dict[str, Any], profile: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
        blockers = []
        if classification.get("status") in {"NEEDS_CLARIFICATION", "OUT_OF_SCOPE"} or not classification.get("entity"):
            blockers.append("Entity classification requires clarification or is out of scope.")
        if profile.get("warnings"):
            blockers.extend(profile["warnings"])
        if mapping.get("unresolved_fields"):
            blockers.append(f"{len(mapping['unresolved_fields'])} source field(s) need disposition.")
        if any(p.get("risk") == "POTENTIAL_COLLISION" for p in mapping.get("proposals", [])):
            blockers.append("Multiple source fields may map to the same canonical field.")
        return {"agent": self.name, "status": "REVIEW_REQUIRED" if blockers else "READY_FOR_HUMAN_REVIEW",
                "blockers": blockers, "human_approval_required": True,
                "target_loading_enabled": False, "wave_assignment_enabled": False}


class MigrationAgentCoordinator:
    """Coordinates bounded agent capabilities for one source file."""

    def __init__(self) -> None:
        self.classifier = SourceClassificationAgent()
        self.profiler = ProfilingAgent()
        self.mapper = SchemaSemanticMappingAgent()
        self.review = ReviewReadinessAgent()

    def analyze(self, records: list[dict[str, Any]], context: str = "",
                in_scope: list[str] | None = None) -> dict[str, Any]:
        scope = [entity for entity in (in_scope or P0_ENTITIES) if entity in P0_ENTITIES]
        headers = list(records[0].keys()) if records else []
        classification = self.classifier.run(headers, context, scope)
        profile = self.profiler.run(records)
        entity = classification.get("entity")
        mapping = self.mapper.run(entity, headers, scope) if entity else {
            "agent": self.mapper.name, "status": "BLOCKED_CLASSIFICATION", "entity": None,
            "proposals": [], "unresolved_fields": [],
            "notice": "Mapping recommendations are blocked until an in-scope entity is confirmed."
        }
        readiness = self.review.run(classification, profile, mapping)
        return {
            "source_firm": "CoreCap", "target_firm": "Simplicity", "scope": scope,
            "classification": classification, "profile": profile, "mapping": mapping,
            "review": readiness, "execution": {"target_loading": "DEFERRED", "wave_assignment": "DEFERRED"},
        }
