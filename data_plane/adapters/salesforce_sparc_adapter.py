import logging
from typing import Dict, Any, List

logger = logging.getLogger("SalesforceSparcAdapter")

class SalesforceSparcAdapter:
    """Transforms canonical entity records into Salesforce SPARC CRM structures."""

    def transform(self, canonical_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sparc_payloads = []
        for rec in canonical_records:
            sparc_payloads.append({
                "sparc_object": "Account",
                "household_id": rec.get("household_id"),
                "account_name": rec.get("display_name", "SPARC Managed Account"),
                "advisor_id": rec.get("primary_advisor_id"),
                "sync_status": "STAGED"
            })
        return sparc_payloads