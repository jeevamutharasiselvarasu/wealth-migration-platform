import logging
from typing import Dict, Any, List

logger = logging.getLogger("DataQualityAgent")


class DataQualityAgent:
    """Asserts cross-entity integrity across the identity spine: Client -> Household -> Account."""

    def audit_integrity(
        self,
        accounts: List[Dict[str, Any]],
        households: List[Dict[str, Any]],
        clients: List[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        valid_hh_ids = {str(h.get("HH_ID") or h.get("household_id")) for h in households if h}

        orphaned_accounts = []
        for acct in accounts:
            hh_id = str(acct.get("HH_ID") or acct.get("household_id", ""))
            if hh_id and hh_id not in valid_hh_ids:
                orphaned_accounts.append(acct.get("ACCT_NUM") or acct.get("account_id"))

        orphaned_clients = []
        if clients:
            for cl in clients:
                hh_id = str(cl.get("HH_ID") or cl.get("household_id", ""))
                if hh_id and hh_id not in valid_hh_ids:
                    orphaned_clients.append(cl.get("PARTY_ID") or cl.get("client_id"))

        has_failures = bool(orphaned_accounts or orphaned_clients)
        return {
            "total_households": len(households),
            "total_accounts": len(accounts),
            "total_clients": len(clients or []),
            "orphaned_account_count": len(orphaned_accounts),
            "orphaned_accounts": orphaned_accounts,
            "orphaned_client_count": len(orphaned_clients),
            "orphaned_clients": orphaned_clients,
            "status": "PASS" if not has_failures else "FAIL_ORPHAN"
        }