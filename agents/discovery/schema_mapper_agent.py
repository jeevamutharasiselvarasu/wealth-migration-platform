import logging
from typing import Dict, Any, List, Optional, Set

from control_plane.services import source_inference, transformation_rules
from control_plane.services.firm_config import FirmConfigError, get_firm_config
from control_plane.services.value_evidence import propose_from_evidence

logger = logging.getLogger("SchemaMapperAgent")


class SchemaMapperAgent:
    """Source-to-canonical schema mapping engine with telemetry scoring and validation."""

    INFERENCE_CONFIDENCE_CAP = 0.85  # inferred (not catalogued) proposals never look as certain as a known mapping

    @staticmethod
    def _aliases() -> Dict[str, Any]:
        """Source-name knowledge (platform_knowledge/source_aliases.yaml); none is built in, and a missing file means no aliases."""
        import yaml
        from control_plane.paths import platform_knowledge_dir
        try:
            return yaml.safe_load((platform_knowledge_dir() / "source_aliases.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}

    _ALIAS_DATA: Dict[str, Any] = {}

    MAPPING_KINDS = ("DIRECT", "DERIVED", "LOOKUP", "RELATIONSHIP", "EXCLUDED", "UNRESOLVED")

    @classmethod
    def mapping_kind(cls, rule_applied: str, canonical_field: Any = None) -> str:
        """Classify a proposal into the governed mapping kinds from its registered rule family."""
        rule = str(rule_applied or "DIRECT_COPY").strip().upper()
        if rule.startswith("RELATIONSHIP_CANDIDATE"):
            return "RELATIONSHIP"
        if rule.startswith("UNRESOLVED") or not canonical_field:
            return "UNRESOLVED"
        if "_LOOKUP" in rule.split(" ")[0] or rule.startswith("APPROVED_LOOKUP"):
            return "LOOKUP"
        if cls.transformation_family(rule) == "DIRECT_COPY":
            return "DIRECT"
        return "DERIVED"

    @classmethod
    def required_fields(cls, entity_name: str) -> List[str]:
        """Required canonical fields: the firm configuration is the only source. An entity it does not define has none."""
        try:
            config = get_firm_config()
            if entity_name in config.entities:
                return config.required_fields(entity_name)
        except FirmConfigError as exc:
            logger.warning("Firm configuration unavailable; no required fields known: %s", exc)
        return []

    @classmethod
    def _identity_target(cls, entity_name: str, source_field: str) -> Optional[str]:
        """A source column already named like a canonical field of the entity maps to itself."""
        try:
            config = get_firm_config()
        except FirmConfigError:
            return None
        if entity_name not in config.entities:
            return None
        candidate = str(source_field).strip().upper()
        from control_plane.services.canonical_overlay import active_added_fields
        known = config.canonical_field_names(entity_name) + active_added_fields(entity_name)
        return candidate if candidate in known else None

    # Per-field name-evidence confidence for a proposed mapping. This reflects how the
    # source column name matched, not value profiling, and never replaces human review.
    IDENTITY_MATCH_CONFIDENCE = 0.95
    ALIAS_MATCH_CONFIDENCE = 0.85

    # Declarative rule families emitted by discovery plus simple scalar transforms.
    # Never eval arbitrary rule text; only registered families are executable.
    SUPPORTED_APPROVED_TRANSFORMS = {
        "DIRECT_COPY", "ALIAS", "TRIM", "UPPERCASE", "LOWERCASE", "NORMALIZE_WHITESPACE", "DATE_FORMAT",
    }

    @classmethod
    def transformation_family(cls, expression: str) -> str:
        """Extract the registered rule family from a descriptive proposal."""
        normalized = str(expression or "DIRECT_COPY").strip().upper()
        if transformation_rules.parse_expression(normalized):
            return normalized  # pinned governed rule: RULE:<ID>@<VERSION>
        if normalized in cls.SUPPORTED_APPROVED_TRANSFORMS:
            return normalized
        # Discovery recommendations may include examples in parentheses, e.g. DATE_FORMAT (MM/DD/YYYY -> YYYY-MM-DD).
        family = normalized.split("(", 1)[0].strip()
        return family if family in cls.SUPPORTED_APPROVED_TRANSFORMS else normalized

    @classmethod
    def supports_transformation(cls, expression: str) -> bool:
        if transformation_rules.parse_expression(expression):
            return transformation_rules.approved_rule_for_expression(expression) is not None
        return cls.transformation_family(expression) in cls.SUPPORTED_APPROVED_TRANSFORMS

    def __init__(
        self,
        custom_catalog: Dict[str, str] = None,
        approved_transformations: Dict[str, str] = None,
        infer: bool = True,
    ):
        # infer=False: only catalogued and approved mappings fill the rows. Evidence and name-and-value guesses stay proposals
        # (they are shown for review, never used to build the canonical rows the approved mappings produce).
        self.infer = infer
        self.catalog = {**self.DEFAULT_CATALOG, **(custom_catalog or {})}
        self.approved_transformations = {
            source: str(expression or "DIRECT_COPY").strip().upper()
            for source, expression in (approved_transformations or {}).items()
        }
        unsupported = {
            source: expression for source, expression in self.approved_transformations.items()
            if not self.supports_transformation(expression)
        }
        self._rule_cache = {}
        self._rule_warnings: Dict[str, int] = {}
        for expression in self.approved_transformations.values():
            if transformation_rules.parse_expression(expression) and expression not in unsupported.values():
                self._rule_cache[expression] = transformation_rules.approved_rule_for_expression(expression)
        if unsupported:
            raise ValueError(
                "Unsupported approved transformation expression(s): "
                + ", ".join(f"{source}={expression}" for source, expression in sorted(unsupported.items()))
            )

    @classmethod
    def _apply_approved_transformation(cls, value: Any, expression: str) -> Any:
        """Apply an approved registered transformation without evaluating arbitrary text."""
        family = cls.transformation_family(expression)
        if family in {"DIRECT_COPY", "ALIAS"}:
            return value
        if value is None:
            return None
        text_value = str(value)
        if family == "TRIM":
            return text_value.strip()
        if family == "UPPERCASE":
            return text_value.upper()
        if family == "LOWERCASE":
            return text_value.lower()
        if family == "NORMALIZE_WHITESPACE":
            return " ".join(text_value.split())
        if family == "DATE_FORMAT":
            date_str = text_value.strip()
            parts = date_str.split("/")
            if len(parts) == 3:
                return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
            return date_str
        raise ValueError(f"Unsupported approved transformation: {expression}")

    def _apply_for_record(self, value: Any, expression: str, record: Dict[str, Any]) -> Any:
        """Apply an approved expression; governed rules may read other fields of the same record."""
        rule = self._rule_cache.get(expression)
        if rule is None:
            return self._apply_approved_transformation(value, expression)
        result, warning = transformation_rules.apply_rule(rule["rule_type"], rule["params"], value, record)
        if warning:
            note = f"{rule['rule_id']}@{rule['version']}: {warning}"
            self._rule_warnings[note] = self._rule_warnings.get(note, 0) + 1
        return result

    def map_source_schema(self, raw_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Backward-compatible mapping function for 9-agent pipeline."""
        res = self.map_source_to_canonical("Account", raw_records)
        return {
            "mapped_records": res["mapped_records"],
            "unmapped_fields": res["unmapped_fields"],
            "record_count": res["record_count"]
        }

    def map_source_to_canonical(self, entity_name: str, raw_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Translates raw records to canonical format, computes lineage, and calculates confidence."""
        if not raw_records:
            return {
                "entity": entity_name,
                "status": "NO_SOURCE_RECORDS",
                "record_count": 0,
                "mapped_records": [],
                "mapped_fields": [],
                "unmapped_fields": [],
                "missing_required": self.required_fields(entity_name),
                "confidence_score": 0.0,
                "relationship_candidates": [],
                "field_lineages": [],
                "warnings": [f"No source records provided for entity {entity_name}."]
            }

        self._rule_warnings = {}
        mapped_records = []
        source_fields_seen: Set[str] = set()
        mapped_target_fields: Set[str] = set()
        unmapped_source_fields: Set[str] = set()
        field_lineages = []
        lineage_fields_seen: Set[str] = set()
        field_collisions: Set[str] = set()
        relationship_candidates = []

        for raw_row in raw_records:
            canonical_row = {}
            for raw_key, raw_val in raw_row.items():
                source_fields_seen.add(raw_key)
                link = next((r for r in self.RELATIONSHIP_COLUMNS if r.get("entity") == entity_name and r.get("source_field") == raw_key), None)
                if link:
                    source_key = next((raw_row.get(key) for key in link.get("source_key_fields", []) if raw_row.get(key) not in (None, "")), None)
                    relationship_candidates.append({
                        "relationship": link["relationship"],
                        "source_entity": entity_name,
                        "source_key": str(source_key) if source_key is not None else None,
                        "source_field": raw_key,
                        "target_entity": link["target_entity"],
                        "target_field": link["target_field"],
                        "target_key_value": str(raw_val).strip() if raw_val is not None else "",
                        "status": "PROPOSED_REQUIRES_SOURCE_CONFIRMATION",
                    })
                    if raw_key not in lineage_fields_seen:
                        field_lineages.append({
                            "source_field": raw_key,
                            "canonical_field": None,
                            "rule_applied": "RELATIONSHIP_CANDIDATE_PROPOSED",
                        })
                        lineage_fields_seen.add(raw_key)
                    continue
                target_key = self.ENTITY_CATALOG.get(entity_name, {}).get(raw_key) or self.catalog.get(raw_key)
                identity_match = False
                if not target_key:
                    target_key = self._identity_target(entity_name, raw_key)
                    identity_match = bool(target_key)

                if target_key and identity_match:
                    # Exact canonical name: record a direct copy; reviewers may propose rules.
                    mapped_target_fields.add(target_key)
                    if target_key in canonical_row:
                        field_collisions.add(f"{target_key} <- {raw_key}")
                    identity_rule = self.approved_transformations.get(raw_key)
                    identity_value = raw_val
                    identity_applied = "DIRECT_COPY"
                    if identity_rule:
                        identity_value = self._apply_for_record(raw_val, identity_rule, raw_row)
                        identity_applied = f"APPROVED_{self.transformation_family(identity_rule)}"
                    canonical_row[target_key] = identity_value
                    if raw_key not in lineage_fields_seen:
                        field_lineages.append({
                            "source_field": raw_key,
                            "canonical_field": target_key,
                            "rule_applied": identity_applied,
                            "confidence": self.IDENTITY_MATCH_CONFIDENCE,
                            "sample_value": str(identity_value)
                        })
                        lineage_fields_seen.add(raw_key)
                elif target_key:
                    mapped_target_fields.add(target_key)
                    transformed_val = raw_val
                    applied_rule = "DIRECT_COPY"

                    approved_expression = self.approved_transformations.get(raw_key)
                    if approved_expression:
                        transformed_val = self._apply_for_record(raw_val, approved_expression, raw_row)
                        applied_rule = f"APPROVED_{self.transformation_family(approved_expression)}"

                    if target_key in canonical_row:
                        field_collisions.add(f"{target_key} <- {raw_key}")
                    canonical_row[target_key] = transformed_val
                    if raw_key not in lineage_fields_seen:
                        field_lineages.append({
                            "source_field": raw_key,
                            "canonical_field": target_key,
                            "rule_applied": applied_rule,
                            "confidence": self.ALIAS_MATCH_CONFIDENCE,
                            "sample_value": str(transformed_val)
                        })
                        lineage_fields_seen.add(raw_key)
                else:
                    unmapped_source_fields.add(raw_key)

            mapped_records.append(canonical_row)

        # Evidence-based proposals for still-unmet required fields. Proposals only: low capped
        # confidence, named evidence, never auto-approved, ambiguity leaves the field unresolved.
        if unmapped_source_fields and self.infer:
            unmet = [f for f in self.required_fields(entity_name) if f not in mapped_target_fields]
            columns = {c: [row.get(c) for row in raw_records] for c in unmapped_source_fields}
            for proposal in propose_from_evidence(unmet, columns, len(raw_records)):
                column, target = proposal["source_field"], proposal["canonical_field"]
                unmapped_source_fields.discard(column)
                mapped_target_fields.add(target)
                for row_out, row_in in zip(mapped_records, raw_records):
                    row_out[target] = row_in.get(column)
                field_lineages.append({
                    "source_field": column,
                    "canonical_field": target,
                    "rule_applied": f"DIRECT_COPY (VALUE_EVIDENCE: {proposal['evidence']})",
                    "confidence": proposal["confidence"],
                    "mapping_basis": "VALUE_EVIDENCE",
                    "sample_value": str(raw_records[0].get(column)),
                })
                lineage_fields_seen.add(column)

        # Semantic inference for columns whose names the catalog has never seen: proposals only, confidence
        # capped, evidence recorded, every proposal passes the deterministic gate and is human-reviewed.
        if unmapped_source_fields and self.infer:
            try:
                inferred = source_inference.infer_source_mappings(
                    entity_name, raw_records,
                    canonical={k: v for k, v in source_inference.canonical_from_config(entity_name).items()
                               if k not in mapped_target_fields})
            except Exception:  # never let an inference problem break the catalog mapping
                inferred = []
            for proposal in inferred:
                column, target = proposal["source_field"], proposal["canonical_field"]
                if column not in unmapped_source_fields or target in mapped_target_fields:
                    continue
                unmapped_source_fields.discard(column)
                mapped_target_fields.add(target)
                for row_out, row_in in zip(mapped_records, raw_records):
                    row_out[target] = row_in.get(column)
                field_lineages.append({
                    "source_field": column,
                    "canonical_field": target,
                    "rule_applied": "DIRECT_COPY",
                    "confidence": round(min(proposal["confidence"], self.INFERENCE_CONFIDENCE_CAP), 3),
                    "mapping_basis": "NAME_VALUE_INFERENCE",
                    "evidence": proposal.get("evidence", []),
                    "provider": proposal.get("provider", ""),
                    "sample_value": "",
                })
                lineage_fields_seen.add(column)

        for raw_key in sorted(unmapped_source_fields):
            if raw_key not in lineage_fields_seen:
                field_lineages.append({
                    "source_field": raw_key,
                    "canonical_field": None,
                    "rule_applied": "UNRESOLVED_NO_CANONICAL_FIELD",
                })
                lineage_fields_seen.add(raw_key)

        for lineage in field_lineages:
            lineage["mapping_kind"] = self.mapping_kind(lineage.get("rule_applied"), lineage.get("canonical_field"))

        required_fields = self.required_fields(entity_name)
        missing_required = [req for req in required_fields if req not in mapped_target_fields]

        warnings = []
        if unmapped_source_fields:
            warnings.append(f"Unmapped source columns: {', '.join(sorted(unmapped_source_fields))}")
        if missing_required:
            warnings.append(f"CRITICAL: Missing required canonical fields: {', '.join(missing_required)}")
        if field_collisions:
            warnings.append("Multiple source columns map to the same canonical field: " + ", ".join(sorted(field_collisions)))

        for note, count in sorted(self._rule_warnings.items())[:10]:
            warnings.append(f"Transformation warning: {note} ({count} value{'s' if count != 1 else ''})")

        total_fields = len(source_fields_seen)
        mapped_count = len(source_fields_seen - unmapped_source_fields)
        base_confidence = (mapped_count / total_fields) if total_fields > 0 else 0.0

        if missing_required or field_collisions:
            confidence_score = round(max(0.0, base_confidence - 0.40), 2)
            status_val = "WARNING_INCOMPLETE"
        elif unmapped_source_fields:
            confidence_score = round(base_confidence, 2)
            status_val = "PARTIAL_MAPPED"
        else:
            confidence_score = 1.0
            status_val = "SUCCESS"

        return {
            "entity": entity_name,
            "status": status_val,
            "record_count": len(mapped_records),
            "mapped_records": mapped_records,
            "mapped_fields": sorted(list(mapped_target_fields)),
            "unmapped_fields": sorted(list(unmapped_source_fields)),
            "missing_required": missing_required,
            "field_collisions": sorted(field_collisions),
            "relationship_candidates": relationship_candidates,
            "confidence_score": confidence_score,
            "field_lineages": field_lineages,
            "warnings": warnings
        }


SchemaMapperAgent._ALIAS_DATA = SchemaMapperAgent._aliases()
SchemaMapperAgent.DEFAULT_CATALOG = dict(SchemaMapperAgent._ALIAS_DATA.get("catalog") or {})
SchemaMapperAgent.ENTITY_CATALOG = {e: dict(m) for e, m in (SchemaMapperAgent._ALIAS_DATA.get("entity_catalog") or {}).items()}
SchemaMapperAgent.RELATIONSHIP_COLUMNS = list(SchemaMapperAgent._ALIAS_DATA.get("relationships") or [])
