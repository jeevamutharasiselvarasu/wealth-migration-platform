import logging
from typing import Dict, Any, List
from data_plane.connectors.amplify_connector import AmplifyConnector
from data_plane.connectors.blackdiamond_connector import BlackDiamondConnector

logger = logging.getLogger("ExtractorAgent")

class ExtractorAgent:
    """Pulls per-advisor snapshots and metadata from source connectors."""

    def __init__(self, source_dir: str):
        self.amplify = AmplifyConnector(source_dir)
        self.blackdiamond = BlackDiamondConnector(source_dir)

    def extract_advisor_cohort(self, rep_code: str) -> Dict[str, Any]:
        logger.info(f"ExtractorAgent: Pulling snapshot for Advisor Rep Code {rep_code}")
        raw_sleeves = self.amplify.extract_sleeve_extract()
        raw_positions = self.amplify.extract_positions()
        raw_perf = self.blackdiamond.extract_performance_history()

        # Filter by advisor rep code
        advisor_sleeves = [s for s in raw_sleeves if str(s.get("REP_CODE")).strip() == rep_code]
        advisor_positions = [p for p in raw_positions if str(p.get("REP_CODE")).strip() == rep_code]
        advisor_perf = [f for f in raw_perf if str(f.get("REP_CODE")).strip() == rep_code]

        return {
            "rep_code": rep_code,
            "sleeve_count": len(advisor_sleeves),
            "position_count": len(advisor_positions),
            "performance_count": len(advisor_perf),
            "sleeves": advisor_sleeves,
            "positions": advisor_positions,
            "performance": advisor_perf
        }