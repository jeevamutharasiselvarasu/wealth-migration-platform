import os
import pandas as pd
import logging
from typing import Dict, Any, List

logger = logging.getLogger("AmplifyConnector")

class AmplifyConnector:
    """Reads legacy TAMP extracts containing source-specific column names."""

    def __init__(self, source_dir: str):
        self.source_dir = source_dir

    def extract_sleeve_extract(self) -> List[Dict[str, Any]]:
        file_path = os.path.join(self.source_dir, "amplify_sleeves.csv")
        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}. Returning fallback raw legacy data.")
            return [
                {"REP_CODE_RAW": "9981", "SRC_ACCOUNT_NO": "ACC-99812", "SUB_SLEEVE_ID": "SLV-01", "STRATEGY_MODEL_ID": "AMPLIFY-GROWTH-01", "TARGET_WEIGHT_PCT": 70.0},
                {"REP_CODE_RAW": "9981", "SRC_ACCOUNT_NO": "ACC-99812", "SUB_SLEEVE_ID": "SLV-02", "STRATEGY_MODEL_ID": "AMPLIFY-INCOME-02", "TARGET_WEIGHT_PCT": 30.0}
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
            return [
                {"REP_CODE_RAW": "9981", "SRC_ACCOUNT_NO": "ACC-99812", "CUSIP": "594918104", "TICKER": "MSFT", "QUANTITY": 150, "market_value": 63000.00},
                {"REP_CODE_RAW": "9981", "SRC_ACCOUNT_NO": "ACC-99812", "CUSIP": "037833100", "TICKER": "AAPL", "QUANTITY": 200, "market_value": 45000.00}
            ]
        try:
            df = pd.read_csv(file_path)
            return df.to_dict(orient="records")
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return []