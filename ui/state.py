import os
import re
import uuid
from pathlib import Path
import json
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from control_plane.kernel.wealth_kernel import WealthManagementKnowledgeKernel
from control_plane.db import get_db_connection, init_db
from control_plane.services.source_file_adapter import read_source_records

SOURCE_UPLOAD_ROOT = Path(__file__).resolve().parents[1] / "App_Data" / "source_uploads"


class WorkspaceStateStore:
    """State engine managing firm context, in-scope filtering, and metadata table persistence."""

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
            "canonical_entity_count": 15,
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
        self.entity_source_files: Dict[str, Dict[str, Dict[str, Any]]] = {}
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

    def persist_source_profile(self, profile: Dict[str, Any], run_id: str) -> str:
        """Persist source profiling metrics; fail loudly if metadata cannot be stored."""
        if profile.get("status") != "PROFILED":
            raise ValueError(f"Cannot persist unsuccessful profile: {profile.get('status')}")
        conn = get_db_connection()
        try:
            profile_id = f"PROF-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO source_file_profiles
                (profile_id, run_id, entity_name, file_name, row_count, column_count, profile_summary_json, created_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    run_id,
                    profile["entity_name"],
                    profile["file_name"],
                    profile["row_count"],
                    profile["column_count"],
                    json.dumps(profile.get("column_profiles", [])),
                    profile.get("profiled_utc", datetime.now(timezone.utc).isoformat()),
                ),
            )
            conn.commit()
            return profile_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def persist_mapping_lineage(self, mapping_result: Dict[str, Any], run_id: str, actor: str = "Platform Admin"):
        """Persist mapping outcome and lineage atomically; never suppress persistence errors."""
        conn = get_db_connection()
        now = datetime.now(timezone.utc).isoformat()
        entity_name = mapping_result.get("entity", "Account")
        try:
            conn.execute(
                """
                INSERT INTO migration_runs
                (run_id, migration_id, wave_id, entity_name, source_record_count, mapped_record_count,
                 confidence_score, status, executed_by, executed_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    self.questionnaire_data.get("migration_id", "UNCONFIGURED"),
                    "NOT_ASSIGNED",
                    entity_name,
                    int(mapping_result.get("record_count", 0)),
                    0,  # Preview rows are not approved canonical output.
                    float(mapping_result.get("confidence_score", 0.0)),
                    ("NEEDS_REVIEW" if any(item.get("canonical_field") for item in mapping_result.get("field_lineages", [])) else mapping_result.get("status", "UNKNOWN")),
                    actor,
                    now,
                ),
            )
            for index, field_lineage in enumerate(mapping_result.get("field_lineages", [])):
                source_field = str(field_lineage.get("source_field", ""))
                if not source_field:
                    continue
                canonical_field = field_lineage.get("canonical_field")
                conn.execute(
                    """
                    INSERT INTO field_lineage_audit
                    (lineage_id, run_id, entity_name, source_column, canonical_column, applied_rule,
                     validation_status, actor, timestamp_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"LIN-{run_id}-{index}-{uuid.uuid4().hex[:8]}",
                        run_id,
                        entity_name,
                        source_field,
                        canonical_field,
                        field_lineage.get("rule_applied"),
                        ("RELATIONSHIP_PROPOSED" if str(field_lineage.get("rule_applied", "")).startswith("RELATIONSHIP_CANDIDATE") else ("PROPOSED_NEEDS_REVIEW" if canonical_field else "UNMAPPED")),
                        actor,
                        now,
                    ),
                )
            for index, field_lineage in enumerate(mapping_result.get("field_lineages", [])):
                source_field = str(field_lineage.get("source_field", "")).strip()
                canonical_field = field_lineage.get("canonical_field")
                if not source_field or not canonical_field:
                    continue
                conn.execute(
                    "INSERT INTO mapping_suggestions "
                    "(suggestion_id, run_id, migration_id, entity_name, source_field, canonical_field, "
                    "proposed_rule, confidence_score, status, created_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)",
                    (
                        f"MAPSUG-{run_id}-{index}-{uuid.uuid4().hex[:8]}",
                        run_id,
                        self.questionnaire_data.get("migration_id", "UNCONFIGURED"),
                        entity_name,
                        source_field,
                        str(canonical_field),
                        field_lineage.get("rule_applied"),
                        float(mapping_result.get("confidence_score", 0.0)),
                        now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def attach_entity_sample_file(self, entity_name: str, filename: str, content_bytes: bytes, source_role: str = "ENTITY_MASTER") -> Dict[str, Any]:
        """Validate and persist a supported sample file with source-role and lineage metadata."""
        if not content_bytes:
            raise ValueError("Uploaded file is empty.")
        try:
            records = read_source_records(filename, content_bytes)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        columns = list(records[0].keys()) if records else []
        if not columns:
            raise ValueError("Uploaded file has no records or headers.")

        safe_entity = re.sub(r"[^A-Za-z0-9_-]+", "_", entity_name).strip("_") or "entity"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)
        upload_dir = SOURCE_UPLOAD_ROOT / safe_entity
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_path = upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
        stored_path.write_bytes(content_bytes)

        record = {
            "filename": Path(filename).name,
            "source_role": str(source_role or "ENTITY_MASTER").strip().upper(),
            "entity_name": entity_name,
            "migration_id": self.questionnaire_data.get("migration_id", "UNCONFIGURED"),
            "stored_path": str(stored_path.resolve()),
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": len(content_bytes),
            "row_count": len(records),
            "column_count": len(columns),
            "columns": columns,
            "status": "UPLOADED",
        }
        role_files = self.entity_source_files.setdefault(entity_name, {})
        role_files[record["source_role"]] = record
        # Keep the existing entity-level mapping path tied to the master file only.
        if record["source_role"] in {"ENTITY_MASTER", "ACCOUNT_MASTER"}:
            self.entity_sample_files[entity_name] = record
        return record

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

    P0_ENTITIES = {"Account", "Client", "Household"}

    def save_entity_scopes(self, scopes_map: Dict[str, str]):
        """Persist only the three entities enabled in the P0 source-to-canonical workflow."""
        allowed_scopes = {"IN_SCOPE", "PHASE_2", "ARCHIVE_ONLY", "OUT_OF_SCOPE"}
        for entity_name, scope in scopes_map.items():
            if entity_name in self.P0_ENTITIES and scope in allowed_scopes:
                self.entity_scopes[entity_name] = scope

    def get_full_menu_card(self) -> Dict[str, List[Dict[str, Any]]]:
        """Expose only Account, Client, and Household in the P0 scope selector."""
        catalog = WealthManagementKnowledgeKernel.get_canonical_entities()
        result = {}
        for class_name, entities in catalog.items():
            scoped_entities = []
            for ent in entities:
                if ent["entity_name"] not in self.P0_ENTITIES:
                    continue
                ent_copy = dict(ent)
                ent_copy["current_scope"] = self.entity_scopes.get(ent["entity_name"], ent["default_scope"])
                scoped_entities.append(ent_copy)
            if scoped_entities:
                result[class_name] = scoped_entities
        return result

    def get_in_scope_entities(self) -> List[Dict[str, Any]]:
        """Return only explicitly in-scope P0 entities; exclude all other canonical entities."""
        in_scope_list = []
        catalog = WealthManagementKnowledgeKernel.get_canonical_entities()
        for entities in catalog.values():
            for ent in entities:
                if ent["entity_name"] not in self.P0_ENTITIES:
                    continue
                scope = self.entity_scopes.get(ent["entity_name"], ent["default_scope"])
                if scope == "IN_SCOPE":
                    ent_copy = dict(ent)
                    ent_copy["current_scope"] = scope
                    ent_copy["sample_file_info"] = self.entity_sample_files.get(ent["entity_name"], None)
                    in_scope_list.append(ent_copy)
        return in_scope_list

    def persist_mcl_execution_summary(
        self,
        mcl_run_id: str,
        status: str,
        entity_results: Dict[str, Dict[str, Any]],
        relationship_validation: Dict[str, Any],
        execution_boundary: Dict[str, Any],
    ) -> None:
        """Persist metadata-only MCL batch evidence; never store mapped source records or identifier values."""
        entity_summary = {}
        for entity_name, result in entity_results.items():
            mapping = result.get("mapping", {})
            profile = result.get("profile", {})
            entity_summary[entity_name] = {
                "run_id": result.get("run_id"),
                "canonical_entity": result.get("canonical_entity"),
                "source_file": result.get("source_file"),
                "row_count": profile.get("row_count", 0),
                "column_count": profile.get("column_count", 0),
                "status": mapping.get("status", "UNKNOWN"),
                "mapped_fields": mapping.get("mapped_fields", []),
                "unmapped_fields": mapping.get("unmapped_fields", []),
                "missing_required": mapping.get("missing_required", []),
                "confidence_score": mapping.get("confidence_score", 0.0),
            }
        relationship_summary = {
            "status": relationship_validation.get("status", "UNKNOWN"),
            "summary": relationship_validation.get("summary", {}),
            "key_checks": [
                {key: value for key, value in item.items() if key not in {"duplicate_values"}}
                for item in relationship_validation.get("key_checks", [])
            ],
            "relationship_checks": [
                {key: value for key, value in item.items() if key not in {"orphan_values"}}
                for item in relationship_validation.get("relationship_checks", [])
            ],
        }
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO mcl_execution_runs
                (mcl_run_id, migration_id, status, entity_summary_json, relationship_summary_json,
                 execution_boundary_json, executed_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mcl_run_id,
                    self.questionnaire_data.get("migration_id", "UNCONFIGURED"),
                    status,
                    json.dumps(entity_summary),
                    json.dumps(relationship_summary),
                    json.dumps(execution_boundary),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_source_upload_entities(self) -> List[Dict[str, Any]]:
        """Return in-scope source upload cards, presenting source Client as canonical Client."""
        result = []
        for entity in self.get_in_scope_entities():
            if entity["entity_name"] == "Client":
                alias = dict(entity)
                alias["entity_name"] = "Client"
                alias["canonical_entity_name"] = "Client"
                alias["expected_file"] = "clients.csv"
                alias["required_fields"] = ["CLIENT_ID", "LEGAL_NAME", "TAX_ID", "CLIENT_TYPE"]
                alias["sample_file_info"] = self.entity_sample_files.get("Client")
                alias["source_files"] = self.entity_source_files.get("Client", {})
                result.append(alias)
            else:
                item = dict(entity)
                item["source_files"] = self.entity_source_files.get(entity["entity_name"], {})
                result.append(item)
        return result

    def add_firm_file(self, filename: str, category: str, content_bytes: bytes) -> Dict[str, Any]:
        """Register a knowledge asset and extract lightweight schema/rule signals for review."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        suffix = Path(filename).suffix.lower()
        extracted_text = ""
        columns = []
        try:
            if suffix in {".md", ".txt"}:
                extracted_text = content_bytes.decode("utf-8-sig")
            elif suffix == ".json":
                payload = json.loads(content_bytes.decode("utf-8-sig"))
                extracted_text = json.dumps(payload, ensure_ascii=False, indent=2)
                def discover_columns(value):
                    # Support records, field-name lists, nested dictionary schemas,
                    # and JSON Schema "properties" without assuming a single vendor shape.
                    if isinstance(value, list):
                        if value and all(isinstance(item, str) for item in value):
                            return value
                        if value and all(isinstance(item, dict) for item in value):
                            named = []
                            for item in value:
                                name = (
                                    item.get("canonical_field") or item.get("column_name")
                                    or item.get("field_name") or item.get("source_field")
                                    or item.get("name")
                                )
                                if isinstance(name, str) and name.strip():
                                    named.append(name.strip())
                            return named or list(value[0].keys())
                        for item in value:
                            found = discover_columns(item)
                            if found:
                                return found
                    elif isinstance(value, dict):
                        for key in ("columns", "fields", "schema", "properties", "record",
                                    "records", "data", "tables", "entities", "items", "definitions", "$defs", "components"):
                            if key in value:
                                found = discover_columns(value[key])
                                if found:
                                    return found
                        # Some dictionaries map each field name to its metadata.
                        if value and all(isinstance(item, dict) for item in value.values()):
                            return list(value.keys())
                    return []

                columns = discover_columns(payload)
            elif suffix in {".csv", ".tsv"}:
                import csv
                reader = csv.reader(content_bytes.decode("utf-8-sig").splitlines(), delimiter="\t" if suffix == ".tsv" else ",")
                columns = next(reader, [])
                extracted_text = "Columns: " + ", ".join(columns)
            elif suffix in {".xlsx", ".xlsm", ".xls"}:
                records = read_source_records(filename, content_bytes)
                columns = list(records[0].keys()) if records else []
                extracted_text = "Columns: " + ", ".join(columns)
        except Exception as exc:
            raise ValueError(f"Could not parse knowledge asset {filename}: {exc}") from exc
        if not extracted_text.strip() and not columns:
            raise ValueError("Knowledge asset is empty or contains no readable schema.")
        if category not in {"data_dictionary", "transformation_rule", "source_analysis", "mapping_reference"}:
            raise ValueError("Unsupported knowledge category.")
        lines = [line.strip() for line in extracted_text.splitlines() if line.strip()]
        summary = ("Extracted " + str(len(columns)) + " column names: " + ", ".join(map(str, columns[:20])) if columns else "Extracted " + str(len(lines)) + " non-empty knowledge lines.")
        rules = [line for line in lines if any(token in line.lower() for token in ("mapping", "canonical", "transform", "rule:", "source field", "target field"))][:12]
        doc_record = {
            "id": f"DOC-{len(self.firm_knowledge_docs) + 1:03d}",
            "filename": Path(filename).name,
            "category": category,
            "size_bytes": len(content_bytes),
            "uploaded_at": timestamp,
            "parsed_summary": summary,
            "extracted_columns": list(map(str, columns)),
            "transferable_knowledge": rules,
            "content_preview": "\n".join(lines[:30])[:5000],
            "status": "REGISTERED_REVIEW_REQUIRED",
        }
        self.firm_knowledge_docs.append(doc_record)
        self.generated_dashboard = self._build_dashboard_from_form()
        return doc_record

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
        knowledge_assets = self.firm_knowledge_docs
        transferable_rules = [rule for doc in knowledge_assets for rule in doc.get("transferable_knowledge", [])]

        return {
            "firm_name": firm_name,
            "advisor_count": advisor_count,
            "primary_custodian": custodian,
            "aum_drift_gate": aum_drift_gate,
            "operating_model": self.questionnaire_data.get("operating_model", "Rep-as-PM"),
            "program_status": "CONDITION_FRAMEWORK_ACTIVE",
            "cohort_conditions": cohort_conditions,
            "knowledge_asset_count": len(knowledge_assets),
            "knowledge_assets": [{"filename": doc.get("filename"), "category": doc.get("category"), "summary": doc.get("parsed_summary"), "columns": doc.get("extracted_columns", []), "status": doc.get("status", "REGISTERED_REVIEW_REQUIRED")} for doc in knowledge_assets],
            "knowledge_column_count": sum(len(doc.get("extracted_columns", [])) for doc in knowledge_assets),
            "transferable_knowledge": transferable_rules[:20],
            "segregation_conditions": [
                {"name": "AUM band", "rule": "Use only when a reliable account AUM field is available; thresholds require business-owner confirmation."},
                {"name": "Data quality readiness", "rule": "Hold records with missing identifiers or unresolved critical mappings for remediation."},
                {"name": "Custodian / source system", "rule": "Group by verified source only when operational handling differs."}
            ],
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }


state_store = WorkspaceStateStore()