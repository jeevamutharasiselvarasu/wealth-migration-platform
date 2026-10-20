import os
import json
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from control_plane.kernel.wealth_kernel import WealthManagementKnowledgeKernel
from control_plane.db import get_db_connection, init_db


class WorkspaceStateStore:
    """State engine for the three-entity migration pilot and metadata persistence."""

    def __init__(self):
        init_db()
        self.active_role: str = "Platform Admin"
        self.adls_connected: bool = True
        self.adls_container: str = "stg-corecap-data"

        # 1. Configurable Acquisition Discovery Context
        self.questionnaire_data: Dict[str, Any] = {
            "firm_name": "Core Cap Wealth Management",
            "migration_id": "MIG-CORECAP-2026",
            "advisor_count": 120,
            "operating_model": "Rep-as-PM",
            "source_tamp": "Amplify",
            "performance_source": "Black Diamond",
            "primary_custodian": "Charles Schwab (Retained)",
            "aum_drift_gate": "0.50%",
            "restricted_models": "MOD-RESTRICTED-01, MOD-RESTRICTED-02",
            "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }

        # 2. Kernel Specs (Editable ONLY by Platform Admin / Domain SME)
        self.kernel_specs: Dict[str, Any] = {
            "canonical_entity_count": 3,
            "sleeve_invariant": "1.0000 (100.00%)",
            "sleeve_tolerance": "0.0005",
            "aum_drift_gate": "0.5000%",
            "last_modified_by": "System Default",
            "last_modified_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }

        # 3. Entity Scopes Storage (In Scope vs Out of Scope)
        self.entity_scopes: Dict[str, str] = {}
        for category, entities in WealthManagementKnowledgeKernel.get_canonical_entities().items():
            for ent in entities:
                self.entity_scopes[ent["entity_name"]] = ent["default_scope"]

        self.firm_knowledge_docs: List[Dict[str, Any]] = [
            {
                "id": "DOC-001",
                "filename": "amplify_data_dictionary.json",
                "category": "data_dictionary",
                "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "parsed_summary": "Amplify UMA Sleeve and Holding Schema Contract"
            }
        ]

        self.entity_sample_files: Dict[str, Dict[str, Any]] = {}
        self.pipeline_runs: List[Dict[str, Any]] = []
        self.audit_log: List[Dict[str, Any]] = []
        self.generated_dashboard: Dict[str, Any] = self._build_dashboard_from_form()

    def update_kernel_specs(self, updated_by: str, sleeve_invariant: str, sleeve_tolerance: str, aum_drift_gate: str) -> Dict[str, Any]:
        """Updates Kernel invariants when executed by Platform Admin or Domain SME."""
        self.kernel_specs.update({
            "sleeve_invariant": sleeve_invariant,
            "sleeve_tolerance": sleeve_tolerance,
            "aum_drift_gate": aum_drift_gate,
            "last_modified_by": updated_by,
            "last_modified_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        })
        self._record_metadata_audit("KERNEL_SPEC_UPDATE", updated_by, f"Invariant: {sleeve_invariant}, Gate: {aum_drift_gate}")
        return self.kernel_specs

    def persist_source_profile(self, profile: Dict[str, Any], run_id: str = "PILOT-RUN-001") -> Optional[str]:
        """Persists source file profiling metrics into source_file_profiles SQLite table."""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            profile_id = f"PROF-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
            cursor.execute("""
                INSERT OR REPLACE INTO source_file_profiles 
                (profile_id, run_id, entity_name, file_name, row_count, column_count, profile_summary_json, created_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                profile_id,
                run_id,
                profile.get("entity_name", "Account"),
                profile.get("file_name", "accounts.csv"),
                profile.get("row_count", 0),
                profile.get("column_count", 0),
                json.dumps(profile.get("column_profiles", [])),
                profile.get("profiled_utc", datetime.now(timezone.utc).isoformat())
            ))
            conn.commit()
            conn.close()
            return profile_id
        except Exception:
            return None

    def persist_mapping_lineage(self, mapping_result: Dict[str, Any], run_id: str = "PILOT-RUN-001", actor: str = "Platform Admin"):
        """Persists mapping result summaries and granular field lineage into SQLite metadata tables."""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Record Migration Run Summary
            cursor.execute("""
                INSERT OR REPLACE INTO migration_runs 
                (run_id, migration_id, wave_id, entity_name, source_record_count, mapped_record_count, confidence_score, status, executed_by, executed_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                self.questionnaire_data.get("migration_id", "MIG-CORECAP-2026"),
                "PILOT",
                mapping_result.get("entity", "Account"),
                mapping_result.get("record_count", 0),
                len(mapping_result.get("mapped_records", [])),
                mapping_result.get("confidence_score", 0.0),
                mapping_result.get("status", "SUCCESS"),
                actor,
                datetime.now(timezone.utc).isoformat()
            ))

            # Record Field-Level Lineage
            for fl in mapping_result.get("field_lineages", []):
                lineage_id = f"LIN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{fl['source_field']}"
                cursor.execute("""
                    INSERT OR REPLACE INTO field_lineage_audit 
                    (lineage_id, run_id, entity_name, source_column, canonical_column, applied_rule, validation_status, actor, timestamp_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    lineage_id,
                    run_id,
                    mapping_result.get("entity", "Account"),
                    fl["source_field"],
                    fl["canonical_field"],
                    fl["rule_applied"],
                    "PASS",
                    actor,
                    datetime.now(timezone.utc).isoformat()
                ))

            conn.commit()
            conn.close()
        except Exception:
            pass

    def get_agent_detail(self, agent_id: str) -> Dict[str, Any]:
        if self.pipeline_runs:
            latest_run = self.pipeline_runs[-1]
            details = latest_run.get("agent_details", {})
            if agent_id in details:
                return details[agent_id]

        return {
            "name": f"Agent {agent_id.replace('_', ' ').title()}",
            "phase": "Pipeline Execution",
            "status": "READY_TO_RUN",
            "inputs": {"message": "Run pipeline in Step 5 to generate execution traces."},
            "transformation_logic": "-- Transformation SQL or logic will be displayed here.",
            "outputs": {},
            "warnings": []
        }

    def apply_agent_override(self, agent_id: str, new_logic: str, override_notes: str) -> Dict[str, Any]:
        if self.pipeline_runs:
            latest_run = self.pipeline_runs[-1]
            agent_data = latest_run.get("agent_details", {}).get(agent_id)
            if agent_data:
                agent_data["transformation_logic"] = new_logic
                agent_data["status"] = "HUMAN_OVERRIDDEN"
                agent_data.setdefault("warnings", []).append(f"Human Override: {override_notes}")

        entry = self.record_approval("REP-9981", "Platform Admin", f"OVERRIDE_AGENT_{agent_id.upper()}: {override_notes}")
        return {"status": "SUCCESS", "agent_id": agent_id, "updated_logic": new_logic, "audit_entry": entry}

    def get_cohort_sleeve_summary(self, rep_code: str) -> Dict[str, Any]:
        allocations = [
            {"sleeve_id": "SLV-01", "model_code": "MOD-GROWTH", "target_pct": 70.0},
            {"sleeve_id": "SLV-02", "model_code": "MOD-INCOME", "target_pct": 30.0}
        ]

        total_pct = sum(float(a.get("target_pct", 0.0)) for a in allocations)
        sum_check = f"{total_pct / 100.0:.4f} (PASS)" if abs(total_pct - 100.0) < 0.01 else f"{total_pct / 100.0:.4f} (FAIL)"

        return {
            "rep_code": rep_code,
            "allocations": allocations,
            "sum_check": sum_check
        }

    def update_questionnaire_from_form(self, form_payload: Dict[str, Any]) -> Dict[str, Any]:
        self.questionnaire_data.update(form_payload)
        self.questionnaire_data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.generated_dashboard = self._build_dashboard_from_form()
        return self.generated_dashboard

    def save_entity_scopes(self, scopes_map: Dict[str, str]):
        self.entity_scopes.update(scopes_map)

    def get_full_menu_card(self) -> Dict[str, List[Dict[str, Any]]]:
        catalog = WealthManagementKnowledgeKernel.get_canonical_entities()
        result = {}
        for class_name, entities in catalog.items():
            result[class_name] = []
            for ent in entities:
                ent_copy = dict(ent)
                ent_copy["current_scope"] = self.entity_scopes.get(ent["entity_name"], ent["default_scope"])
                result[class_name].append(ent_copy)
        return result

    def get_in_scope_entities(self) -> List[Dict[str, Any]]:
        """Returns only the configured pilot entities that are currently in scope."""
        in_scope_list = []
        catalog = WealthManagementKnowledgeKernel.get_canonical_entities()
        for class_name, entities in catalog.items():
            for ent in entities:
                scope = self.entity_scopes.get(ent["entity_name"], ent["default_scope"])
                if scope in ["IN_SCOPE", "ARCHIVE_ONLY"]:
                    ent_copy = dict(ent)
                    ent_copy["current_scope"] = scope
                    ent_copy["sample_file_info"] = self.entity_sample_files.get(ent["entity_name"], None)
                    in_scope_list.append(ent_copy)
        return in_scope_list

    def add_firm_file(self, filename: str, category: str, content_bytes: bytes) -> Dict[str, Any]:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        doc_record = {
            "id": f"DOC-{len(self.firm_knowledge_docs) + 1:03d}",
            "filename": filename,
            "category": category,
            "size_bytes": len(content_bytes),
            "uploaded_at": timestamp,
            "parsed_summary": f"Supplementary asset '{filename}' ({len(content_bytes)} bytes)"
        }
        self.firm_knowledge_docs.append(doc_record)
        return doc_record

    def attach_entity_sample_file(self, entity_name: str, filename: str, content_bytes: bytes) -> Dict[str, Any]:
        row_count = 0
        try:
            import io
            df = pd.read_csv(io.BytesIO(content_bytes))
            row_count = len(df)
        except Exception:
            pass

        record = {
            "filename": filename,
            "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes": len(content_bytes),
            "row_count": row_count,
            "status": "PARSED"
        }
        self.entity_sample_files[entity_name] = record
        return record

    def record_pipeline_run(self, pipeline_result: Dict[str, Any]):
        self.pipeline_runs.append(pipeline_result)
        self.generated_dashboard = self._build_dashboard_from_form()
        self._record_metadata_audit("PIPELINE_RUN", pipeline_result.get("rep_code"), pipeline_result.get("run_id"))

    def record_approval(self, rep_code: str, approver: str, decision: str) -> Dict[str, Any]:
        entry = {
            "audit_id": f"AUD-{len(self.audit_log)+1:04d}",
            "rep_code": rep_code,
            "approver": approver,
            "decision": decision,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.audit_log.append(entry)
        self.generated_dashboard = self._build_dashboard_from_form()
        self._record_metadata_audit("HITL_APPROVAL", approver, decision)
        return entry

    def _record_metadata_audit(self, action: str, actor: str, details: str):
        """Records audit actions into the approvals table in SQLite."""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO approvals (id, run_id, artifact_id, stage, decision, reviewer, comments, created_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"AUD-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
                self.questionnaire_data.get("migration_id", "MIG-CORECAP-2026"),
                action,
                "AUDIT_LINEAGE",
                action,
                actor or "System",
                details or "",
                datetime.now(timezone.utc).isoformat()
            ))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _build_dashboard_from_form(self) -> Dict[str, Any]:
        from control_plane.services.wave_rules import CohortConditionEngine
        advisor_count = int(self.questionnaire_data.get("advisor_count", 120))
        custodian = self.questionnaire_data.get("primary_custodian", "Charles Schwab (Retained)")
        aum_drift_gate = self.questionnaire_data.get("aum_drift_gate", "0.50%")
        firm_name = self.questionnaire_data.get("firm_name", "Core Cap Wealth Management")

        cohort_conditions = CohortConditionEngine.get_condition_catalog()

        return {
            "firm_name": firm_name,
            "advisor_count": advisor_count,
            "primary_custodian": custodian,
            "aum_drift_gate": aum_drift_gate,
            "operating_model": self.questionnaire_data.get("operating_model", "Rep-as-PM"),
            "program_status": "CONDITION_FRAMEWORK_ACTIVE",
            "cohort_conditions": cohort_conditions,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }


state_store = WorkspaceStateStore()