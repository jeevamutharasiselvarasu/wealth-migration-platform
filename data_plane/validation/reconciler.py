import logging
from typing import Dict, Any, List

logger = logging.getLogger("ReconciliationEngine")


class ReconciliationEngine:
    """Calculates AUM market value variance and checks against tolerance thresholds[cite: 40]."""

    def __init__(self, tolerance_pct: float = 0.5):
        self.tolerance_pct = tolerance_pct

    def reconcile_aum(self, source_positions: List[Dict[str, Any]], target_positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        source_aum = sum(float(p.get("market_value", p.get("mkt_val", 0))) for p in source_positions)
        target_aum = sum(float(p.get("market_value", p.get("mkt_val", 0))) for p in target_positions)

        if source_aum == 0:
            variance_pct = 0.0
        else:
            variance_pct = abs((target_aum - source_aum) / source_aum) * 100.0

        status = "PASS" if variance_pct <= self.tolerance_pct else "FAIL_HOLD"

        return {
            "source_aum": round(source_aum, 2),
            "target_aum": round(target_aum, 2),
            "variance_pct": round(variance_pct, 4),
            "tolerance_threshold": self.tolerance_pct,
            "status": status
        }