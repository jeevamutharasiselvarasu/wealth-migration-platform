import io
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import pandas as pd

logger = logging.getLogger("SourceDataProfiler")


class SourceDataProfiler:
    """Inspects source files, infers data types, calculates null rates, and discovers natural keys."""

    @classmethod
    def profile_csv_content(cls, file_name: str, content: bytes, entity_name: str = "Account") -> Dict[str, Any]:
        """Profiles raw CSV bytes and extracts structural/semantic metadata."""
        try:
            df = pd.read_csv(io.BytesIO(content))
        except Exception as e:
            logger.error(f"Error parsing CSV content for {file_name}: {e}")
            return {
                "file_name": file_name,
                "entity_name": entity_name,
                "status": "ERROR_PARSING",
                "error_message": str(e),
                "row_count": 0,
                "column_count": 0,
                "candidate_keys": [],
                "column_profiles": []
            }

        row_count = len(df)
        column_profiles = []
        candidate_keys = []

        for col in df.columns:
            series = df[col]
            null_count = int(series.isna().sum())
            null_rate = round((null_count / row_count) * 100, 2) if row_count > 0 else 0.0
            unique_count = int(series.nunique(dropna=True))

            inferred_type = "string"
            if pd.api.types.is_numeric_dtype(series):
                inferred_type = "decimal" if pd.api.types.is_float_dtype(series) else "integer"
            elif pd.api.types.is_datetime64_any_dtype(series) or "date" in col.lower() or "dt" in col.lower():
                inferred_type = "date"

            is_candidate_key = (null_count == 0 and unique_count == row_count and row_count > 0)
            if is_candidate_key:
                candidate_keys.append(col)

            sample_vals = [str(v) for v in series.dropna().unique()[:3]]

            column_profiles.append({
                "column_name": col,
                "inferred_type": inferred_type,
                "null_count": null_count,
                "null_rate_pct": null_rate,
                "unique_count": unique_count,
                "is_candidate_key": is_candidate_key,
                "sample_values": sample_vals
            })

        return {
            "file_name": file_name,
            "entity_name": entity_name,
            "status": "PROFILED",
            "row_count": row_count,
            "column_count": len(df.columns),
            "candidate_keys": candidate_keys,
            "column_profiles": column_profiles,
            "profiled_utc": datetime.now(timezone.utc).isoformat()
        }