import logging
from typing import Any, Dict, List
from pydantic import BaseModel

logger = logging.getLogger("CohortConditionEngine")


class CohortConditionCriterion(BaseModel):
    condition_id: str
    category: str
    condition_name: str
    parameter: str
    operator: str
    threshold_value: str
    business_rationale: str
    telemetry_source: str
    status: str = "CONFIGURED_FOR_ADLS_TELEMETRY"


class CohortConditionEngine:
    """Catalogues condition definitions only; it does not evaluate or assign migration waves."""

    DEFAULT_CONDITIONS: List[CohortConditionCriterion] = [
        CohortConditionCriterion(
            condition_id="COND-AUM-01",
            category="AUM Value Tiering",
            condition_name="High-Net-Worth & Key Accounts",
            parameter="account_market_value",
            operator=">=",
            threshold_value="$10,000,000.00",
            business_rationale="Potential high-touch cohort criterion; evaluate only when source telemetry is available and approved.",
            telemetry_source="ADLS Position & Account Snapshots",
        ),
        CohortConditionCriterion(
            condition_id="COND-AUM-02",
            category="AUM Value Tiering",
            condition_name="Core Wealth Tier",
            parameter="account_market_value",
            operator="BETWEEN",
            threshold_value="$1,000,000.00 - $10,000,000.00",
            business_rationale="Potential standard-volume cohort criterion; no cohort is assigned in this phase.",
            telemetry_source="ADLS Position & Account Snapshots",
        ),
        CohortConditionCriterion(
            condition_id="COND-CUST-01",
            category="Custodian Clearing Rails",
            condition_name="Retained Direct-Custody Accounts",
            parameter="custodian_code",
            operator="==",
            threshold_value="SCHWAB",
            business_rationale="Capture custodian condition for later telemetry-based cohort planning.",
            telemetry_source="ADLS Schwab PAS Raw Extract",
        ),
        CohortConditionCriterion(
            condition_id="COND-CUST-02",
            category="Custodian Clearing Rails",
            condition_name="Secondary & Multi-Custodian Accounts",
            parameter="custodian_code",
            operator="IN",
            threshold_value="['FIDELITY', 'PERSHING']",
            business_rationale="Capture multi-custodian condition for later telemetry-based cohort planning.",
            telemetry_source="ADLS Multi-Custodian Extract Feeds",
        ),
        CohortConditionCriterion(
            condition_id="COND-LEGAL-01",
            category="Client Entity Structure",
            condition_name="Standard Retail Registrations",
            parameter="registration_type",
            operator="IN",
            threshold_value="['INDIVIDUAL', 'IRA_ROLLOVER', 'JOINT_TIC']",
            business_rationale="Potential registration condition; requires validated source values before evaluation.",
            telemetry_source="ADLS Client & Registration Catalog",
        ),
        CohortConditionCriterion(
            condition_id="COND-LEGAL-02",
            category="Client Entity Structure",
            condition_name="Complex Fiduciary Entities",
            parameter="party_type",
            operator="IN",
            threshold_value="['TRUST', 'CORPORATION', 'ESTATE', 'LLC']",
            business_rationale="Potential fiduciary condition; no legal or wave decision is made here.",
            telemetry_source="ADLS Client Entity Taxonomy",
        ),
        CohortConditionCriterion(
            condition_id="COND-QUAL-01",
            category="Data Quality Gate",
            condition_name="Clean Mapping & Zero Lineage Gaps",
            parameter="mapping_confidence_score",
            operator=">=",
            threshold_value="95.0% AND 0 Missing Mandatory Fields",
            business_rationale="Candidate readiness criterion to validate after mapping telemetry is trustworthy.",
            telemetry_source="Control Plane Mapping Telemetry Engine",
        ),
        CohortConditionCriterion(
            condition_id="COND-QUAL-02",
            category="Data Quality Gate",
            condition_name="Remediation & Exception Handling",
            parameter="unmapped_column_count",
            operator=">",
            threshold_value="0 OR Orphaned Parent References",
            business_rationale="Candidate remediation criterion; does not assign a remediation wave.",
            telemetry_source="Control Plane Validation Auditor",
        ),
    ]

    @classmethod
    def get_condition_catalog(cls) -> List[Dict[str, Any]]:
        """Return configured conditions with explicit non-evaluated status."""
        return [
            {**condition.dict(), "status": "CONFIGURED_NOT_EVALUATED"}
            for condition in cls.DEFAULT_CONDITIONS
        ]


class WaveConditionEvaluator:
    """Compatibility guard: wave assignment is intentionally disabled in the foundation phase."""

    @classmethod
    def assign_wave_for_record(cls, record: Dict[str, Any], rules=None) -> str:
        raise RuntimeError(
            "Wave assignment is disabled. Configure conditions now; evaluate cohorts only "
            "after approved source telemetry is available."
        )
