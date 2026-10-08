import logging
from typing import Dict, Any, List
from data_plane.core.sleeve_engine import SleeveMathEngine

logger = logging.getLogger("SleeveMathAgent")

class SleeveMathAgent:
    """Agent wrapper enforcing Rep-as-PM sleeve decomposition invariants."""

    def process_advisor_models(self, advisor_payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"SleeveMathAgent: Processing models for advisor {advisor_payload.get('rep_code')}")
        
        sleeves_by_account = {}
        for sleeve in advisor_payload.get("sleeves", []):
            acct = sleeve.get("account_id") or sleeve.get("ACCT_NUM")
            if acct:
                sleeves_by_account.setdefault(str(acct), []).append(sleeve)

        validated_accounts = []
        for acct_id, raw_sleeves in sleeves_by_account.items():
            result = SleeveMathEngine.normalize_and_validate(acct_id, raw_sleeves)
            validated_accounts.append(result)

        return {
            "rep_code": advisor_payload.get("rep_code"),
            "validated_accounts": validated_accounts,
            "status": "VALIDATED"
        }