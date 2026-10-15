import logging
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("CohortConditionEngine")


class CohortConditionCriterion(BaseModel):
    condition_id: str
    category: str  # AUM Value, Custodian, Entity Structure, Data Quality, Operating Model
    condition_name: str
    parameter: str
    operator: str  # >=, <=, ==, IN, BETWEEN
    threshold_value: str
    business_rationale: str
    telemetry_source: str  # ADLS Account Snapshot, Client Extract, Mapping Engine
    status: str = "CONFIGURED_FOR_ADLS_TELEMETRY"


class CohortConditionRule(BaseModel):
    rule_id: str
    target_wave: str
    condition_name: str
    priority: int = 1
    criteria: List[Dict[str, Any]]


class CohortConditionEngine:
    """Manages declarative conditions used to actuate migration cohorts based on ADLS telemetry."""

    DEFAULT_CONDITIONS: List[CohortConditionCriterion] = [
        CohortConditionCriterion(
            condition_id="COND-AUM-01",
            category="AUM Value Tiering",
            condition_name="High-Net-Worth & Key Accounts",
            parameter="account_market_value",
            operator=">=",
            threshold_value="$10,000,000.00",
            business_rationale="Prioritizes high-value books for high-touch pilot validation or hypercare monitoring",
            telemetry_source="ADLS Position & Account Snapshots",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        ),
        CohortConditionCriterion(
            condition_id="COND-AUM-02",
            category="AUM Value Tiering",
            condition_name="Core Wealth Tier",
            parameter="account_market_value",
            operator="BETWEEN",
            threshold_value="$1,000,000.00 - $10,000,000.00",
            business_rationale="Standard private wealth volume cohort for bulk automated transformation",
            telemetry_source="ADLS Position & Account Snapshots",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        ),
        CohortConditionCriterion(
            condition_id="COND-CUST-01",
            category="Custodian Clearing Rails",
            condition_name="Retained Direct-Custody Accounts",
            parameter="custodian_code",
            operator="==",
            threshold_value="SCHWAB",
            business_rationale="Direct API integration with zero custodial asset transfers required",
            telemetry_source="ADLS Schwab PAS Raw Extract",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        ),
        CohortConditionCriterion(
            condition_id="COND-CUST-02",
            category="Custodian Clearing Rails",
            condition_name="Secondary & Multi-Custodian Accounts",
            parameter="custodian_code",
            operator="IN",
            threshold_value="['FIDELITY', 'PERSHING']",
            business_rationale="Requires external custodian crosswalks and clearing reconciliation",
            telemetry_source="ADLS Multi-Custodian Extract Feeds",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        ),
        CohortConditionCriterion(
            condition_id="COND-LEGAL-01",
            category="Client Entity Structure",
            condition_name="Standard Retail Registrations",
            parameter="registration_type",
            operator="IN",
            threshold_value="['INDIVIDUAL', 'IRA_ROLLOVER', 'JOINT_TIC']",
            business_rationale="Direct one-to-one individual ownership with standard KYC contracts",
            telemetry_source="ADLS Client & Registration Catalog",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        ),
        CohortConditionCriterion(
            condition_id="COND-LEGAL-02",
            category="Client Entity Structure",
            condition_name="Complex Fiduciary Entities",
            parameter="party_type",
            operator="IN",
            threshold_value="['TRUST', 'CORPORATION', 'ESTATE', 'LLC']",
            business_rationale="Entity agreements and trust documentation require legal sign-off",
            telemetry_source="ADLS Client Entity Taxonomy",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        ),
        CohortConditionCriterion(
            condition_id="COND-QUAL-01",
            category="Data Quality Gate",
            condition_name="Clean Mapping & Zero Lineage Gaps",
            parameter="mapping_confidence_score",
            operator=">=",
            threshold_value="95.0% AND 0 Missing Mandatory Fields",
            business_rationale="Verified parent-child referential integrity (Client -> Household -> Account)",
            telemetry_source="Control Plane Mapping Telemetry Engine",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        ),
        CohortConditionCriterion(
            condition_id="COND-QUAL-02",
            category="Data Quality Gate",
            condition_name="Remediation & Exception Handling",
            parameter="unmapped_column_count",
            operator=">",
            threshold_value="0 OR Orphaned Parent References",
            business_rationale="Quarantines records with missing mandatory keys for manual analyst remediation",
            telemetry_source="Control Plane Validation Auditor",
            status="CONFIGURED_FOR_ADLS_TELEMETRY"
        )
    ]

    @classmethod
    def get_condition_catalog(cls) -> List[Dict[str, Any]]:
        """Returns the full catalog of declarative cohort creation conditions."""
        return [c.dict() for c in cls.DEFAULT_CONDITIONS]


class WaveConditionEvaluator:
    """Evaluates condition rules against source entity telemetry to assign cohorts (Backwards-compatible)."""

    DEFAULT_RULES = [
        CohortConditionRule(
            rule_id="RULE-W0",
            target_wave="WAVE-00 (Pilot)",
            condition_name="High AUM & Clean Mapping",
            priority=1,
            criteria=[
                {"field": "confidence_score", "op": ">=", "val": 0.95},
                {"field": "missing_required_count", "op": "==", "val": 0}
            ]
        ),
        CohortConditionRule(
            rule_id="RULE-W1",
            target_wave="WAVE-01",
            condition_name="Schwab Retained Accounts",
            priority=2,
            criteria=[
                {"field": "CUSTODIAN_CODE", "op": "==", "val": "SCHWAB"}
            ]
        ),
        CohortConditionRule(
            rule_id="RULE-W2",
            target_wave="WAVE-02",
            condition_name="Fidelity & Pershing Accounts",
            priority=3,
            criteria=[
                {"field": "CUSTODIAN_CODE", "op": "IN", "val": ["FIDELITY", "PERSHING"]}
            ]
        )
    ]

    @classmethod
    def assign_wave_for_record(cls, record: Dict[str, Any], rules: Optional[List[CohortConditionRule]] = None) -> str:
        active_rules = sorted(rules or cls.DEFAULT_RULES, key=lambda r: r.priority)

        for rule in active_rules:
            matches = True
            for crit in rule.criteria:
                field_name = crit["field"]
                op = crit["op"]
                target_val = crit["val"]
                actual_val = record.get(field_name)

                if op == ">=":
                    if float(actual_val or 0) < float(target_val):
                        matches = False
                elif op == "==":
                    if str(actual_val).upper() != str(target_val).upper():
                        matches = False
                elif op == "IN":
                    if str(actual_val).upper() not in [str(v).upper() for v in target_val]:
                        matches = False

            if matches:
                return rule.target_wave

        return "WAVE-03 (Fallback / Remediation)"