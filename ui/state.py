import os
import json
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime
from control_plane.kernel.wealth_kernel import WealthManagementKnowledgeKernel

class WorkspaceStateStore:
    """Thread-safe persistent state engine managing firm context, entity scope choices, cohort summaries, and agent trace overrides."""

    def __init__(self):
        self.active_role: str = "Platform Admin"
        self.adls_connected: bool = True
        self.adls_container: str = "stg-corecap-data"
        
        # Discovery Questionnaire State
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
            "last_updated": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Entity Scopes Storage
        self.entity_scopes: Dict[str, str] = {}
        for category, entities in WealthManagementKnowledgeKernel.get_canonical_entities().items():
            for ent in entities:
                self.entity_scopes[ent["entity_name"]] = ent["default_scope"]

        self.firm_knowledge_docs: List[Dict[str, Any]] = [
            {
                "id": "DOC-001",
                "filename": "amplify_data_dictionary.json",
                "category": "data_dictionary",
                "uploaded_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "parsed_summary": "Amplify UMA Sleeve and Holding Schema Contract"
            }
        ]

        self.entity_sample_files: Dict[str, Dict[str, Any]] = {}
        self.pipeline_runs: List[Dict[str, Any]] = []
        self.audit_log: List[Dict[str, Any]] = []
        self.generated_dashboard: Dict[str, Any] = self._build_dashboard_from_form()

    def get_agent_detail(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Returns deep-dive transformation details for a specific agent from the latest pipeline run."""
        if not self.pipeline_runs:
            return None
        latest_run = self.pipeline_runs[-1]
        details = latest_run.get("agent_details", {})
        return details.get(agent_id, None)

    def apply_agent_override(self, agent_id: str, new_logic: str, override_notes: str) -> Dict[str, Any]:
        """Applies a human override to an agent's transformation logic."""
        if self.pipeline_runs:
            latest_run = self.pipeline_runs[-1]
            agent_data = latest_run.get("agent_details", {}).get(agent_id)
            if agent_data:
                agent_data["transformation_logic"] = new_logic
                agent_data["status"] = "HUMAN_OVERRIDDEN"
                agent_data.setdefault("warnings", []).append(f"Human Override Applied: {override_notes}")

        entry = {
            "audit_id": f"AUD-{len(self.audit_log)+1:04d}",
            "rep_code": "REP-9981",
            "approver": "Platform Admin (Human Intervention)",
            "decision": f"OVERRIDE_AGENT_{agent_id.upper()}: {override_notes}",
            "timestamp": datetime.utcnow().isoformat()
        }
        self.audit_log.append(entry)
        return {"status": "SUCCESS", "agent_id": agent_id, "updated_logic": new_logic}

    def get_cohort_sleeve_summary(self, rep_code: str) -> Dict[str, Any]:
        allocations = [
            {"sleeve_id": "SLV-01", "model_code": "MOD-GROWTH", "target_pct": 70.0},
            {"sleeve_id": "SLV-02", "model_code": "MOD-INCOME", "target_pct": 30.0}
        ]
        
        if self.pipeline_runs:
            for run in reversed(self.pipeline_runs):
                if run.get("rep_code") == rep_code:
                    break

        total_pct = sum(float(a.get("target_pct", 0.0)) for a in allocations)
        sum_check = f"{total_pct / 100.0:.4f} (PASS)" if abs(total_pct - 100.0) < 0.01 else f"{total_pct / 100.0:.4f} (FAIL)"

        return {
            "rep_code": rep_code,
            "allocations": allocations,
            "sum_check": sum_check
        }

    def update_questionnaire_from_form(self, form_payload: Dict[str, Any]) -> Dict[str, Any]:
        self.questionnaire_data.update(form_payload)
        self.questionnaire_data["last_updated"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        doc_record = {
            "id": f"DOC-{len(self.firm_knowledge_docs) + 1:03d}",
            "filename": filename,
            "category": category,
            "size_bytes": len(content_bytes),
            "uploaded_at": timestamp,
            "parsed_summary": f"Uploaded supplementary file '{filename}' ({len(content_bytes)} bytes)"
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
            "uploaded_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "size_bytes": len(content_bytes),
            "row_count": row_count,
            "status": "PARSED"
        }
        self.entity_sample_files[entity_name] = record
        return record

    def record_pipeline_run(self, pipeline_result: Dict[str, Any]):
        self.pipeline_runs.append(pipeline_result)
        self.generated_dashboard = self._build_dashboard_from_form()

    def record_approval(self, rep_code: str, approver: str, decision: str) -> Dict[str, Any]:
        entry = {
            "audit_id": f"AUD-{len(self.audit_log)+1:04d}",
            "rep_code": rep_code,
            "approver": approver,
            "decision": decision,
            "timestamp": datetime.utcnow().isoformat()
        }
        self.audit_log.append(entry)
        self.generated_dashboard = self._build_dashboard_from_form()
        return entry

    def _build_dashboard_from_form(self) -> Dict[str, Any]:
        advisor_count = int(self.questionnaire_data.get("advisor_count", 120))
        custodian = self.questionnaire_data.get("primary_custodian", "Charles Schwab (Retained)")
        aum_drift_gate = self.questionnaire_data.get("aum_drift_gate", "0.50%")
        firm_name = self.questionnaire_data.get("firm_name", "Core Cap Wealth Management")

        pilot_count = min(5, advisor_count)
        rem_count = max(0, advisor_count - pilot_count)
        w1_count = int(rem_count * 0.25)
        w2_count = int(rem_count * 0.35)
        w3_count = rem_count - w1_count - w2_count

        waves = [
            {"wave_id": "WAVE-00", "cohort_name": "Pilot Cohort", "advisors": pilot_count, "status": "PLANNED", "aum_drift": "N/A"},
            {"wave_id": "WAVE-01", "cohort_name": "Wave 1", "advisors": w1_count, "status": "PLANNED", "aum_drift": "N/A"},
            {"wave_id": "WAVE-02", "cohort_name": "Wave 2", "advisors": w2_count, "status": "PLANNED", "aum_drift": "N/A"},
            {"wave_id": "WAVE-03", "cohort_name": "Wave 3", "advisors": w3_count, "status": "PLANNED", "aum_drift": "N/A"}
        ]

        approved_reps = {entry["rep_code"] for entry in self.audit_log if entry.get("decision") == "APPROVED"}
        
        for run in self.pipeline_runs:
            run_wave_id = run.get("wave_id")
            run_rep_code = run.get("rep_code")
            drift_val = run.get("reconciliation_summary", {}).get("variance_pct", 0.0)

            for w in waves:
                if w["wave_id"] == run_wave_id:
                    if run_rep_code in approved_reps:
                        w["status"] = "COMPLETED"
                    else:
                        w["status"] = "IN_PROGRESS"
                    w["aum_drift"] = f"{drift_val:.4f}%"

        program_status = "PROGRAM_INSTANTIATED"
        if any(w["status"] == "COMPLETED" for w in waves):
            program_status = "CUTOVER_IN_PROGRESS"

        return {
            "firm_name": firm_name,
            "advisor_count": advisor_count,
            "primary_custodian": custodian,
            "aum_drift_gate": aum_drift_gate,
            "operating_model": self.questionnaire_data.get("operating_model", "Rep-as-PM"),
            "program_status": program_status,
            "waves": waves,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        }

state_store = WorkspaceStateStore()