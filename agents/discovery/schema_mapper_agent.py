import logging
from typing import Dict, Any, List, Set

logger = logging.getLogger("SchemaMapperAgent")


class SchemaMapperAgent:
    """Source-to-canonical schema mapping engine with telemetry scoring and validation."""

    COMMON_CUSTODIAN_CROSSWALK = {
        "SCHW": "SCHWAB",
        "SWB": "SCHWAB",
        "SCHWAB_PAS": "SCHWAB",
        "FID": "FIDELITY",
        "FIDC": "FIDELITY",
        "NFS": "FIDELITY",
        "PERSH": "PERSHING",
        "PER": "PERSHING",
        "BNY_PERSHING": "PERSHING"
    }

    REGISTRATION_TYPE_CROSSWALK = {
        "INDV_TAXABLE": "INDIVIDUAL",
        "TAX": "INDIVIDUAL",
        "INDV": "INDIVIDUAL",
        "IRA_ROLL": "IRA_ROLLOVER",
        "ROLLOVER_IRA": "IRA_ROLLOVER",
        "IRA_TRAD": "IRA_TRADITIONAL",
        "JT_TEN": "JOINT_TIC",
        "JTWROS": "JOINT_TIC"
    }

    CLIENT_TYPE_CROSSWALK = {
        "INDV": "INDIVIDUAL",
        "INDIVIDUAL": "INDIVIDUAL",
        "TRUST": "TRUST",
        "CORP": "CORPORATION",
        "CORPORATION": "CORPORATION",
        "LLC": "ENTITY",
        "JOINT": "JOINT"
    }

    TRANSACTION_ACTION_CROSSWALK = {
        "BUY_EQUITY": "BUY",
        "SELL_EQUITY": "SELL",
        "DIV_CASH": "DIV",
        "CLIENT_WITHDRAWAL": "WITHDRAWAL",
        "DEP": "DEPOSIT"
    }

    DEFAULT_CATALOG = {
        # Account Attributes
        "LEGACY_ACCT_NO": "ACCT_NUM",
        "ACCT_NUM": "ACCT_NUM",
        "ACCOUNT_ID": "ACCT_NUM",
        "CUST_CODE_SRC": "CUSTODIAN_CODE",
        "ADVISOR_REP_NUM": "REP_CODE",
        "PRIMARY_REP_NUM": "REP_CODE",
        "REGISTRATION_SRC_TYPE": "REG_TYPE",
        "ACCOUNT_OPEN_DT": "OPEN_DATE",
        "LEGACY_HH_ID": "HH_ID",
        "HOUSEHOLD_ID": "HH_ID",
        "HHOLD_ID": "HH_ID",
        "HOUSEHOLD_LINK_ID": "HH_ID",
        "HH_ID": "HH_ID",
        "ACCOUNT_HOUSEHOLD_ID": "HH_ID",

        # Household Attributes
        "SRC_HH_ID": "HH_ID",
        "HH_NAME": "HH_NAME",
        "HOUSEHOLD_NAME": "HH_NAME",
        "HH_TITLE": "HH_NAME",

        # Client / Client Attributes
        "LEGACY_CLIENT_ID": "CLIENT_ID",
        "CLIENT_ID": "CLIENT_ID",
        "PARTY_SRC_ID": "CLIENT_ID",
        "FULL_LEGAL_NAME": "LEGAL_NAME",
        "PARTY_NAME": "LEGAL_NAME",
        "CLIENT_NAME": "LEGAL_NAME",
        "LEGAL_NAME": "LEGAL_NAME",
        "CLIENT_FIRST_NAME": "FIRST_NAME",
        "FIRST_NAME": "FIRST_NAME",
        "CLIENT_LAST_NAME": "LAST_NAME",
        "LAST_NAME": "LAST_NAME",
        "TAX_IDENTIFIER": "TAX_ID",
        "TAX_ID": "TAX_ID",
        "SSN_TIN": "TAX_ID",
        "CLIENT_TYPE_RAW": "CLIENT_TYPE",
        "CLIENT_TYPE": "CLIENT_TYPE",
        "CLIENT_TYPE_SRC": "CLIENT_TYPE",
        "EMAIL_ADDR": "EMAIL",
        "PRIMARY_EMAIL": "EMAIL",
        "PHONE_NUM": "PHONE",
        "MOBILE_PHONE": "PHONE",

        # Sleeve Attributes
        "REP_CODE_RAW": "REP_CODE",
        "SRC_ACCOUNT_NO": "ACCT_NUM",
        "SUB_SLEEVE_ID": "SLEEVE_ID",
        "STRATEGY_MODEL_ID": "MODEL_CODE",
        "TARGET_WEIGHT_PCT": "TARGET_PCT",

        # Transaction Attributes
        "TRANSACTION_REF_ID": "TRANS_ID",
        "TXN_ACTION_TYPE": "TRANS_CODE",
        "SETTLEMENT_AMT": "AMOUNT",
        "TRADE_EVENT_DATE": "TRADE_DATE",

        # Position Attributes
        "CUSIP": "CUSIP",
        "TICKER": "TICKER",
        "QUANTITY": "QUANTITY",
        "market_value": "MARKET_VALUE",

        # Terminated / Archival Attributes
        "TERMINATION_EFFECTIVE_DT": "CLOSE_DATE",
        "CLOSURE_CATEGORY": "ARCHIVE_REASON"
    }

    MANDATORY_CANONICAL_FIELDS = {
        "Account": ["ACCT_NUM", "CUSTODIAN_CODE", "REP_CODE", "OPEN_DATE"],
        "Household": ["HH_ID", "HH_NAME", "REP_CODE"],
        "Client": ["CLIENT_ID", "LEGAL_NAME", "CLIENT_TYPE"],
        "Sleeve": ["ACCT_NUM", "SLEEVE_ID", "TARGET_PCT"],
        "Position": ["ACCT_NUM", "CUSIP", "MARKET_VALUE"],
        "Transaction": ["TRANS_ID", "ACCT_NUM", "AMOUNT"]
    }

    def __init__(self, custom_catalog: Dict[str, str] = None):
        self.catalog = custom_catalog or self.DEFAULT_CATALOG

    def map_source_schema(self, raw_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Backward-compatible mapping function for 9-agent pipeline."""
        res = self.map_source_to_canonical("Account", raw_records)
        return {
            "mapped_records": res["mapped_records"],
            "unmapped_fields": res["unmapped_fields"],
            "record_count": res["record_count"]
        }

    def map_source_to_canonical(self, entity_name: str, raw_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Translates raw records to canonical format, computes lineage, and calculates confidence."""
        if not raw_records:
            return {
                "entity": entity_name,
                "status": "NO_SOURCE_RECORDS",
                "record_count": 0,
                "mapped_records": [],
                "mapped_fields": [],
                "unmapped_fields": [],
                "missing_required": self.MANDATORY_CANONICAL_FIELDS.get(entity_name, []),
                "confidence_score": 0.0,
                "relationship_candidates": [],
                "field_lineages": [],
                "warnings": [f"No source records provided for entity {entity_name}."]
            }

        mapped_records = []
        source_fields_seen: Set[str] = set()
        mapped_target_fields: Set[str] = set()
        unmapped_source_fields: Set[str] = set()
        field_lineages = []
        lineage_fields_seen: Set[str] = set()
        field_collisions: Set[str] = set()
        relationship_candidates = []

        for raw_row in raw_records:
            canonical_row = {}
            for raw_key, raw_val in raw_row.items():
                source_fields_seen.add(raw_key)
                if entity_name == "Client" and raw_key == "HOUSEHOLD_LINK_ID":
                    source_key = next((raw_row.get(key) for key in ("LEGACY_CLIENT_ID", "CLIENT_ID", "PARTY_SRC_ID", "CLIENT_ID") if raw_row.get(key) not in (None, "")), None)
                    relationship_candidates.append({
                        "relationship": "CLIENT_HOUSEHOLD_MEMBERSHIP",
                        "source_entity": "Client",
                        "source_key": str(source_key) if source_key is not None else None,
                        "source_field": raw_key,
                        "target_entity": "Household",
                        "target_field": "HH_ID",
                        "target_key_value": str(raw_val).strip() if raw_val is not None else "",
                        "status": "PROPOSED_REQUIRES_SOURCE_CONFIRMATION",
                    })
                    if raw_key not in lineage_fields_seen:
                        field_lineages.append({
                            "source_field": raw_key,
                            "canonical_field": None,
                            "rule_applied": "RELATIONSHIP_CANDIDATE_PROPOSED",
                        })
                        lineage_fields_seen.add(raw_key)
                    continue
                target_key = self.catalog.get(raw_key)

                if target_key:
                    mapped_target_fields.add(target_key)
                    transformed_val = raw_val
                    applied_rule = "DIRECT_COPY"

                    # Custodian Crosswalk
                    if target_key == "CUSTODIAN_CODE":
                        norm_cust = str(raw_val).strip().upper()
                        if norm_cust in self.COMMON_CUSTODIAN_CROSSWALK:
                            transformed_val = self.COMMON_CUSTODIAN_CROSSWALK[norm_cust]
                            applied_rule = f"CUSTODIAN_LOOKUP ({raw_val} -> {transformed_val})"

                    # Registration Type Crosswalk
                    elif target_key == "REG_TYPE":
                        norm_reg = str(raw_val).strip().upper()
                        if norm_reg in self.REGISTRATION_TYPE_CROSSWALK:
                            transformed_val = self.REGISTRATION_TYPE_CROSSWALK[norm_reg]
                            applied_rule = f"REG_TYPE_LOOKUP ({raw_val} -> {transformed_val})"

                    # Client / Client Type Crosswalk
                    elif target_key == "CLIENT_TYPE":
                        norm_party = str(raw_val).strip().upper()
                        if norm_party in self.CLIENT_TYPE_CROSSWALK:
                            transformed_val = self.CLIENT_TYPE_CROSSWALK[norm_party]
                            applied_rule = f"CLIENT_TYPE_LOOKUP ({raw_val} -> {transformed_val})"

                    # Rep Code prefix normalization
                    elif target_key == "REP_CODE":
                        if not str(raw_val).startswith("REP-"):
                            transformed_val = f"REP-{raw_val}"
                            applied_rule = f"PREFIX_FORMAT (REP-{raw_val})"

                    # Date normalization (MM/DD/YYYY -> YYYY-MM-DD)
                    elif target_key in ["OPEN_DATE", "DOB"]:
                        date_str = str(raw_val).strip()
                        if "/" in date_str:
                            parts = date_str.split("/")
                            if len(parts) == 3:
                                transformed_val = f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
                                applied_rule = "DATE_FORMAT (MM/DD/YYYY -> YYYY-MM-DD)"

                    if target_key in canonical_row:
                        field_collisions.add(f"{target_key} <- {raw_key}")
                    canonical_row[target_key] = transformed_val
                    if raw_key not in lineage_fields_seen:
                        field_lineages.append({
                            "source_field": raw_key,
                            "canonical_field": target_key,
                            "rule_applied": applied_rule,
                            "sample_value": str(transformed_val)
                        })
                        lineage_fields_seen.add(raw_key)
                else:
                    unmapped_source_fields.add(raw_key)

            mapped_records.append(canonical_row)

        for raw_key in sorted(unmapped_source_fields):
            if raw_key not in lineage_fields_seen:
                field_lineages.append({
                    "source_field": raw_key,
                    "canonical_field": None,
                    "rule_applied": "UNRESOLVED_NO_CANONICAL_FIELD",
                })
                lineage_fields_seen.add(raw_key)

        required_fields = self.MANDATORY_CANONICAL_FIELDS.get(entity_name, [])
        missing_required = [req for req in required_fields if req not in mapped_target_fields]

        warnings = []
        if unmapped_source_fields:
            warnings.append(f"Unmapped source columns: {', '.join(sorted(unmapped_source_fields))}")
        if missing_required:
            warnings.append(f"CRITICAL: Missing required canonical fields: {', '.join(missing_required)}")
        if field_collisions:
            warnings.append("Multiple source columns map to the same canonical field: " + ", ".join(sorted(field_collisions)))

        total_fields = len(source_fields_seen)
        mapped_count = len(source_fields_seen - unmapped_source_fields)
        base_confidence = (mapped_count / total_fields) if total_fields > 0 else 0.0

        if missing_required or field_collisions:
            confidence_score = round(max(0.0, base_confidence - 0.40), 2)
            status_val = "WARNING_INCOMPLETE"
        elif unmapped_source_fields:
            confidence_score = round(base_confidence, 2)
            status_val = "PARTIAL_MAPPED"
        else:
            confidence_score = 1.0
            status_val = "SUCCESS"

        return {
            "entity": entity_name,
            "status": status_val,
            "record_count": len(mapped_records),
            "mapped_records": mapped_records,
            "mapped_fields": sorted(list(mapped_target_fields)),
            "unmapped_fields": sorted(list(unmapped_source_fields)),
            "missing_required": missing_required,
            "field_collisions": sorted(field_collisions),
            "relationship_candidates": relationship_candidates,
            "confidence_score": confidence_score,
            "field_lineages": field_lineages,
            "warnings": warnings
        }