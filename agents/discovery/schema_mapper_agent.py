import logging
from typing import Dict, Any, List

logger = logging.getLogger("SchemaMapperAgent")

class SchemaMapperAgent:
    """Applies canonical field mappings and detects unmapped source fields."""

    def __init__(self, mapping_catalog: Dict[str, str] = None):
        self.mapping_catalog = mapping_catalog or {
            "ACCT_NUM": "account_id",
            "HH_ID": "household_id",
            "REP_CODE": "primary_advisor_id"
        }

    def map_source_schema(self, raw_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        mapped_records = []
        unmapped_fields = set()

        for record in raw_records:
            mapped = {}
            for k, v in record.items():
                if k in self.mapping_catalog:
                    mapped[self.mapping_catalog[k]] = v
                else:
                    unmapped_fields.add(k)
                    mapped[k] = v
            mapped_records.append(mapped)

        return {
            "mapped_records": mapped_records,
            "unmapped_fields": list(unmapped_fields),
            "confidence_score": 1.0 if not unmapped_fields else 0.85
        }