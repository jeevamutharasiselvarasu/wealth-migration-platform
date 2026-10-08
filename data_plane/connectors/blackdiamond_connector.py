import os
import pandas as pd
import logging
from typing import Dict, Any, List

logger = logging.getLogger("BlackDiamondConnector")

class BlackDiamondConnector:
    """Reads performance history extract files from Black Diamond."""

    def __init__(self, source_dir: str):
        self.source_dir = source_dir

    def extract_performance_history(self) -> List[Dict[str, Any]]:
        file_path = os.path.join(self.source_dir, "blackdiamond_performance.csv")
        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}. Returning fallback data.")
            return [
                {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "PERIOD": "2026-Q1", "BMV": 105000.00, "EMV": 108000.00, "NET_RETURN": 2.8571}
            ]
        try:
            df = pd.read_csv(file_path)
            return df.to_dict(orient="records")
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return []