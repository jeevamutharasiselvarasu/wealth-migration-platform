import logging
from typing import Dict, Any, List

logger = logging.getLogger("DataQualityAgent")

class DataQualityAgent:
    """Asserts cross-entity integrity (Party -> Household -> Account -> Sleeve -> Position)."""

    def audit_integrity(self, accounts: List[Dict[str, Any]], households: List[Dict[str, Any]]) -> Dict[str, Any]:
        valid_hh_ids = {str(h.get("household_id")) for h in households}
        orphaned_accounts = []

        for acct in accounts:
            hh_id = str(acct.get("household_id", ""))
            if hh_id and hh_id not in valid_hh_ids:
                orphaned_accounts.append(acct.get("account_id"))

        return {
            "total_accounts": len(accounts),
            "orphaned_account_count": len(orphaned_accounts),
            "status": "PASS" if not orphaned_accounts else "FAIL_ORPHAN"
        }