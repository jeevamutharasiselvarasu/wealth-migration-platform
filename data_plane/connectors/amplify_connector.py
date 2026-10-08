import os
import pandas as pd
import logging
from typing import Dict, Any, List

logger = logging.getLogger("AmplifyConnector")

class AmplifyConnector:
    """Reads raw extract files from Amplify TAMP."""

    def __init__(self, source_dir: str):
        self.source_dir = source_dir

    def extract_sleeve_extract(self) -> List[Dict[str, Any]]:
        file_path = os.path.join(self.source_dir, "amplify_sleeves.csv")
        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}. Returning fallback data.")
            return [
                {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "sleeve_id": "SLV-01", "model_code": "MOD-GROWTH", "target_pct": 70.0},
                {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "sleeve_id": "SLV-02", "model_code": "MOD-INCOME", "target_pct": 30.0}
            ]
        try:
            df = pd.read_csv(file_path)
            return df.to_dict(orient="records")
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return []

    def extract_positions(self) -> List[Dict[str, Any]]:
        file_path = os.path.join(self.source_dir, "amplify_positions.csv")
        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}. Returning fallback data.")
            return [
                {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "CUSIP": "594918104", "TICKER": "MSFT", "QUANTITY": 150, "market_value": 63000.00},
                {"REP_CODE": "REP-9981", "ACCT_NUM": "ACC-99812", "CUSIP": "037833100", "TICKER": "AAPL", "QUANTITY": 200, "market_value": 45000.00}
            ]
        try:
            df = pd.read_csv(file_path)
            return df.to_dict(orient="records")
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return []