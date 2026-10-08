import logging
from typing import Dict, Any, List
from data_plane.validation.reconciler import ReconciliationEngine

logger = logging.getLogger("ReconcilerAgent")

class ReconcilerAgent:
    """Compares Orion post-load snapshots against Amplify source data."""

    def __init__(self, tolerance_pct: float = 0.5):
        self.engine = ReconciliationEngine(tolerance_pct=tolerance_pct)

    def execute_advisor_reconciliation(self, source_positions: List[Dict[str, Any]], target_positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        logger.info("ReconcilerAgent: Comparing source vs target AUM...")
        return self.engine.reconcile_aum(source_positions, target_positions)