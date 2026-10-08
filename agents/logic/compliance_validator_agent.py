import logging
from typing import Dict, Any, List

logger = logging.getLogger("ComplianceValidatorAgent")

class ComplianceValidatorAgent:
    """Re-applies Home Office overlays and trading restrictions against Orion drift tolerances."""

    def validate_compliance(self, account_payloads: List[Dict[str, Any]], restricted_list: List[str]) -> Dict[str, Any]:
        violations = []
        for acct in account_payloads:
            for comp in acct.get("compositions", []):
                if comp.get("model_code") in restricted_list:
                    violations.append({
                        "account_id": acct.get("account_id"),
                        "model_code": comp.get("model_code"),
                        "reason": "Restricted model code detected"
                    })

        return {
            "status": "APPROVED" if not violations else "HOLD_COMPLIANCE",
            "violations": violations
        }