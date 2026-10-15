import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agents.discovery.extractor_agent import ExtractorAgent
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from control_plane.db import get_db_connection

logger = logging.getLogger("AgenticOrchestrator")


class AgenticOrchestrator:
    """Run source-to-canonical mapping only for explicitly in-scope entities."""

    ENTITY_SOURCE_KEYS = {
        "account": ("Account", "accounts"),
        "sleeve": ("Sleeve", "sleeves"),
        "position": ("Position", "positions"),
        "transaction": ("Transaction", "transactions"),
    }

    ENTITY_ALIASES = {
        "account": "account",
        "accounts": "account",
        "sleeve": "sleeve",
        "sleeves": "sleeve",
        "sleeveallocation": "sleeve",
        "allocation": "sleeve",
        "position": "position",
        "positions": "position",
        "positionsnapshot": "position",
        "transaction": "transaction",
        "transactions": "transaction",
    }

    def __init__(self, source_dir: str):
        self.extractor = ExtractorAgent(source_dir)
        self.schema_mapper = SchemaMapperAgent()

    @classmethod
    def _normalize_scope(cls, value: str) -> Optional[str]:
        key = "".join(ch.lower() for ch in str(value) if ch.isalnum())
        return cls.ENTITY_ALIASES.get(key)

    def run_cohort_pipeline(
        self,
        rep_code: str,
        wave_id: Optional[str] = None,
        in_scope_entity_names: Optional[List[str]] = None,
        migration_id: str = "UNCONFIGURED",
    ) -> Dict[str, Any]:
        """Profile/map only real source records; no fabricated data, wave assignment, or target loading."""
        run_id = f"RUN-{uuid.uuid4().hex}"
        now_utc = datetime.now(timezone.utc).isoformat()
        requested_scopes = list(in_scope_entity_names or [])
        selected_entities: List[str] = []
        for requested in requested_scopes:
            normalized = self._normalize_scope(requested)
            if normalized and normalized not in selected_entities:
                selected_entities.append(normalized)

        if not selected_entities:
            return {
                "status": "BLOCKED_NO_SCOPE",
                "run_id": run_id,
                "migration_id": migration_id,
                "rep_code": rep_code,
                "wave_assignment": "DISABLED",
                "message": "Select at least one explicitly in-scope entity before running mapping.",
                "mapping_reports": {},
                "migration_readiness": {
                    "readiness_status": "BLOCKED",
                    "in_scope_entities": [],
                    "excluded_entities": requested_scopes,
                    "total_entities_evaluated": 0,
                },
            }

        extracted_data = self.extractor.extract_advisor_cohort(rep_code)
        mapping_reports: Dict[str, Any] = {}
        excluded_entities: List[str] = []
        warnings: List[str] = []

        for entity_key, (entity_label, source_key) in self.ENTITY_SOURCE_KEYS.items():
            if entity_key not in selected_entities:
                continue
            records = extracted_data.get(source_key)
            if not isinstance(records, list) or not records:
                mapping_reports[entity_label] = {
                    "status": "NO_SOURCE_DATA",
                    "record_count": 0,
                    "mapped_records": [],
                    "mapped_fields": [],
                    "unmapped_fields": [],
                    "field_lineages": [],
                    "confidence_score": 0.0,
                    "warnings": [
                        f"No records were found in the real source extract for {entity_label}; no fallback records were generated."
                    ],
                }
                warnings.extend(mapping_reports[entity_label]["warnings"])
                continue

            report = self.schema_mapper.map_source_to_canonical(entity_label, records)
            report.setdefault("record_count", len(records))
            report.setdefault("mapped_records", [])
            report.setdefault("mapped_fields", [])
            report.setdefault("unmapped_fields", [])
            report.setdefault("field_lineages", [])
            report.setdefault("warnings", [])
            report["status"] = (
                "NEEDS_REVIEW"
                if report.get("unmapped_fields") or report.get("warnings")
                else "MAPPED"
            )
            mapping_reports[entity_label] = report
            warnings.extend(report.get("warnings", []))

        excluded_entities = [
            label
            for key, (label, _source_key) in self.ENTITY_SOURCE_KEYS.items()
            if key not in selected_entities
        ]

        confidence_values = [
            float(report.get("confidence_score", 0.0))
            for report in mapping_reports.values()
            if report.get("status") != "NO_SOURCE_DATA"
        ]
        migration_confidence = (
            round(sum(confidence_values) / len(confidence_values), 4)
            if confidence_values
            else 0.0
        )
        overall_status = (
            "NEEDS_REMEDIATION"
            if warnings or any(r.get("status") != "MAPPED" for r in mapping_reports.values())
            else "MAPPING_COMPLETE"
        )

        # Persist each entity run and its lineage. Persistence failures must fail visibly.
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            for entity_name, report in mapping_reports.items():
                entity_run_id = f"{run_id}:{entity_name}"
                cursor.execute(
                    """
                    INSERT INTO migration_runs
                    (run_id, migration_id, wave_id, entity_name, source_record_count,
                     mapped_record_count, confidence_score, status, executed_by, executed_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_run_id,
                        migration_id or "UNCONFIGURED",
                        "NOT_ASSIGNED",
                        entity_name,
                        int(report.get("record_count", 0)),
                        len(report.get("mapped_records", [])),
                        float(report.get("confidence_score", 0.0)),
                        report.get("status", "UNKNOWN"),
                        "AgenticOrchestrator",
                        now_utc,
                    ),
                )
                for index, lineage in enumerate(report.get("field_lineages", [])):
                    source_field = str(lineage.get("source_field", ""))
                    canonical_field = lineage.get("canonical_field")
                    cursor.execute(
                        """
                        INSERT INTO field_lineage_audit
                        (lineage_id, run_id, entity_name, source_column, canonical_column,
                         applied_rule, validation_status, actor, timestamp_utc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"LIN-{run_id}-{entity_name}-{index}",
                            entity_run_id,
                            entity_name,
                            source_field,
                            canonical_field,
                            lineage.get("rule_applied"),
                            "REVIEW_REQUIRED" if not canonical_field else "MAPPED",
                            "SchemaMapperAgent",
                            now_utc,
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Migration metadata persistence failed.")
            raise
        finally:
            conn.close()

        return {
            "status": overall_status,
            "run_id": run_id,
            "migration_id": migration_id or "UNCONFIGURED",
            "rep_code": rep_code,
            "wave_assignment": "DISABLED",
            "timestamp": now_utc,
            "migration_readiness": {
                "overall_confidence_score": migration_confidence,
                "readiness_status": overall_status,
                "in_scope_entities": [name for name in mapping_reports],
                "excluded_entities": excluded_entities,
                "total_entities_evaluated": len(mapping_reports),
            },
            "mapping_reports": mapping_reports,
            "warnings": warnings,
            "execution_boundary": {
                "source_to_canonical_only": True,
                "target_loading": "NOT_RUN",
                "cutover": "NOT_RUN",
                "reconciliation": "NOT_RUN",
                "wave_assignment": "DISABLED",
            },
        }
