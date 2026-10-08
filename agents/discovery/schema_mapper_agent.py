import logging
from typing import Dict, Any, List

logger = logging.getLogger("SchemaMapperAgent")

class SchemaMapperAgent:
    """Applies crosswalk mappings and value coercions to raw source extracts."""

    # Explicit Source-to-Target Attribute Mapping Catalog
    CROSSWALK_CATALOG = {
        # Account Field Mappings
        "LEGACY_ACCT_NO": "ACCT_NUM",
        "CUST_CODE_SRC": "CUSTODIAN_CODE",
        "ADVISOR_REP_NUM": "REP_CODE",
        "REGISTRATION_SRC_TYPE": "REG_TYPE",
        "ACCOUNT_OPEN_DT": "OPEN_DATE",
        
        # Transaction Field Mappings
        "TRANSACTION_REF_ID": "TRANS_ID",
        "TXN_ACTION_TYPE": "TRANS_CODE",
        "SETTLEMENT_AMT": "AMOUNT",
        "TRADE_EVENT_DATE": "TRADE_DATE",
        
        # Sleeve Field Mappings
        "REP_CODE_RAW": "REP_CODE",
        "SRC_ACCOUNT_NO": "ACCT_NUM",
        "SUB_SLEEVE_ID": "SLEEVE_ID",
        "STRATEGY_MODEL_ID": "MODEL_CODE",
        "TARGET_WEIGHT_PCT": "TARGET_PCT",

        # Terminated Account Mappings
        "TERMINATION_EFFECTIVE_DT": "CLOSE_DATE",
        "CLOSURE_CATEGORY": "ARCHIVE_REASON"
    }

    # Value Transformation Lookup Tables
    VALUE_TRANSFORMS = {
        "CUST_CODE_SRC": {"SCHW": "SCHWAB", "FID": "FIDELITY", "PERSH": "PERSHING"},
        "REGISTRATION_SRC_TYPE": {"INDV_TAXABLE": "INDIVIDUAL", "IRA_ROLL": "IRA_ROLLOVER", "JT_TEN": "JOINT_TIC"},
        "TXN_ACTION_TYPE": {"BUY_EQUITY": "BUY", "DIV_CASH": "DIV", "CLIENT_WITHDRAWAL": "WITHDRAWAL"},
        "STRATEGY_MODEL_ID": {"AMPLIFY-GROWTH-01": "MOD-GROWTH", "AMPLIFY-INCOME-02": "MOD-INCOME"}
    }

    def __init__(self, custom_catalog: Dict[str, str] = None):
        self.catalog = custom_catalog or self.CROSSWALK_CATALOG

    def map_source_schema(self, raw_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Translates raw legacy dictionaries into canonical target schemas."""
        mapped_records = []
        unmapped_fields = set()

        for raw_row in raw_records:
            canonical_row = {}
            for raw_key, raw_val in raw_row.items():
                target_key = self.catalog.get(raw_key)
                if target_key:
                    # Apply value coercions if defined
                    transformed_val = raw_val
                    if raw_key in self.VALUE_TRANSFORMS and str(raw_val) in self.VALUE_TRANSFORMS[raw_key]:
                        transformed_val = self.VALUE_TRANSFORMS[raw_key][str(raw_val)]
                    
                    # Normalize Rep Code prefixes if missing
                    if target_key == "REP_CODE" and not str(transformed_val).startswith("REP-"):
                        transformed_val = f"REP-{transformed_val}"

                    canonical_row[target_key] = transformed_val
                else:
                    unmapped_fields.add(raw_key)

            mapped_records.append(canonical_row)

        return {
            "mapped_records": mapped_records,
            "unmapped_fields": list(unmapped_fields),
            "record_count": len(mapped_records)
        }