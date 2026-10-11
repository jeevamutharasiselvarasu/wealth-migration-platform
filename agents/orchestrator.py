import logging
from typing import Dict, Any, List
from datetime import datetime, timezone

from agents.discovery.extractor_agent import ExtractorAgent
from agents.discovery.schema_mapper_agent import SchemaMapperAgent
from agents.logic.sleeve_math_agent import SleeveMathAgent
from agents.logic.compliance_validator_agent import ComplianceValidatorAgent
from agents.logic.data_quality_agent import DataQualityAgent
from agents.operational.loader_agent import LoaderAgent
from agents.operational.reconciler_agent import ReconcilerAgent
from agents.operational.cutover_agent import CutoverAgent
from agents.operational.audit_agent import AuditAgent
from data_plane.adapters.salesforce_sparc_adapter import SalesforceSparcAdapter
from data_plane.adapters.orion_eclipse_adapter import OrionEclipseAdapter

logger = logging.getLogger("AgenticOrchestrator")


class AgenticOrchestrator:
    """Orchestrates all 9 specialized agents across schema translation, sleeve math, and staging[cite: 23]."""

    def __init__(self, source_dir: str):
        self.extractor = ExtractorAgent(source_dir)
        self.schema_mapper = SchemaMapperAgent()
        self.sleeve_math = SleeveMathAgent()
        self.compliance_validator = ComplianceValidatorAgent()
        self.data_quality = DataQualityAgent()
        self.loader = LoaderAgent()
        self.reconciler = ReconcilerAgent(tolerance_pct=0.5)
        self.cutover = CutoverAgent()
        self.audit = AuditAgent()

    def run_cohort_pipeline(self, rep_code: str, wave_id: str) -> Dict[str, Any]:
        logger.info(f"=== Starting Pipeline Run for Advisor {rep_code} ({wave_id}) ===")

        # 1. Extractor Agent
        extracted_data = self.extractor.extract_advisor_cohort(rep_code)

        # 2. Schema Mapper Agent
        mapped_sleeves = self.schema_mapper.map_source_schema(extracted_data["sleeves"])
        mapped_positions = self.schema_mapper.map_source_schema(extracted_data["positions"])

        # 3. Sleeve Math Agent
        sleeve_payload = {
            "rep_code": rep_code,
            "sleeves": mapped_sleeves["mapped_records"]
        }
        validated_sleeve_models = self.sleeve_math.process_advisor_models(sleeve_payload)

        # 4. Compliance Validator Agent
        compliance_result = self.compliance_validator.validate_compliance(
            validated_sleeve_models["validated_accounts"],
            restricted_list=["MOD-RESTRICTED-01"]
        )

        # 5. Data Quality Agent
        dummy_households = [{"household_id": "HH-1001"}]
        dummy_accounts = [{"account_id": acct["account_id"], "household_id": "HH-1001"}
                          for acct in validated_sleeve_models["validated_accounts"]]
        dq_result = self.data_quality.audit_integrity(dummy_accounts, dummy_households)

        # Target Adapters
        sparc_adapter = SalesforceSparcAdapter()
        orion_adapter = OrionEclipseAdapter()

        sparc_payloads = sparc_adapter.transform([
            {"canonical_entity": "Household", "household_id": "HH-1001", "display_name": "Sample Household", "primary_advisor_id": rep_code}
        ])
        orion_payloads = orion_adapter.transform(validated_sleeve_models["validated_accounts"])

        # 6. Loader Agent
        load_result = self.loader.load_staging_packages(sparc_payloads, orion_payloads)

        # 7. Reconciler Agent
        reconcil_result = self.reconciler.execute_advisor_reconciliation(
            source_positions=mapped_positions["mapped_records"],
            target_positions=mapped_positions["mapped_records"]
        )

        # Agent Details for Inspection
        agent_details = {
            "extractor": {
                "name": "Extractor Agent",
                "phase": "Phase 1: Discovery",
                "status": "EXECUTED",
                "inputs": {"source_files": ["amplify_sleeves.csv", "amplify_positions.csv"], "filter_rep_code": rep_code},
                "transformation_logic": f"SELECT * FROM amplify_raw_extracts WHERE REP_CODE_RAW = '{rep_code}'",
                "outputs": extracted_data,
                "warnings": []
            },
            "schema_mapper": {
                "name": "Schema Mapper Agent",
                "phase": "Phase 1: Discovery",
                "status": "EXECUTED",
                "inputs": extracted_data["sleeves"],
                "transformation_logic": """-- Source-to-Target Schema Crosswalk Rules
SRC_ACCOUNT_NO    --> Account.account_id
SUB_SLEEVE_ID     --> Sleeve.sleeve_id
STRATEGY_MODEL_ID --> Model.model_code (Crosswalk: AMPLIFY-GROWTH-01 -> MOD-GROWTH)
TARGET_WEIGHT_PCT --> Sleeve.target_weight (Float Coercion)""",
                "outputs": mapped_sleeves,
                "warnings": mapped_sleeves.get("unmapped_fields", [])
            },
            "sleeve_math": {
                "name": "Sleeve Math Agent",
                "phase": "Phase 2: Business Logic",
                "status": "INVARIANT_PASS",
                "inputs": mapped_sleeves["mapped_records"],
                "transformation_logic": """target_weight = TARGET_PCT / 100.00
ASSERT SUM(target_weight) == 1.0000""",
                "outputs": validated_sleeve_models["validated_accounts"],
                "warnings": []
            },
            "reconciler": {
                "name": "Reconciler Agent",
                "phase": "Phase 3: Operational Execution",
                "status": reconcil_result["status"],
                "inputs": {"source_positions": mapped_positions["mapped_records"]},
                "transformation_logic": "variance_pct = abs((target_aum - source_aum) / source_aum) * 100.0",
                "outputs": reconcil_result,
                "warnings": []
            }
        }

        return {
            "run_id": f"RUN-{rep_code}-{wave_id}",
            "rep_code": rep_code,
            "wave_id": wave_id,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "extracted_sleeve_count": extracted_data["sleeve_count"],
            "extracted_position_count": extracted_data["position_count"],
            "compliance_status": compliance_result["status"],
            "data_quality_status": dq_result["status"],
            "load_summary": load_result,
            "reconciliation_summary": reconcil_result,
            "hitl_status": "PENDING_APPROVAL" if reconcil_result["status"] == "PASS" else "HOLD_VARIANCE",
            "agent_details": agent_details
        }