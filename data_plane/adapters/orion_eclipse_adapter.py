import logging
from typing import Dict, Any, List

logger = logging.getLogger("OrionEclipseAdapter")

class OrionEclipseAdapter:
    """Transforms canonical entity records into Orion Eclipse accounting structures."""

    def transform(self, account_models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        orion_payloads = []
        for acct in account_models:
            for comp in acct.get("compositions", []):
                orion_payloads.append({
                    "orion_portfolio_id": acct.get("account_id"),
                    "model_code": comp.get("model_code"),
                    "target_weight": float(comp.get("target_weight", 0.0)),
                    "rebalancer_status": "STAGED"
                })
        return orion_payloads