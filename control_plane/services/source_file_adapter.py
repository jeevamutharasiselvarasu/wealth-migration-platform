"""P0 source-file adapters for local/sample-based source-to-canonical discovery.
Supported inputs: CSV, TSV, XLSX, XLSM, XLS, and JSON. Values are returned as
strings to preserve identifiers. Mapping and approval are intentionally separate.
"""
from __future__ import annotations
import csv
import io
import json
from pathlib import PurePath
from typing import Any
import pandas as pd

SUPPORTED_SOURCE_EXTENSIONS = frozenset({".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".json"})

class SourceFileError(ValueError):
    """Raised when a source file is unsupported or structurally invalid."""

def supported_source_formats() -> list[str]:
    return sorted(SUPPORTED_SOURCE_EXTENSIONS)

def read_source_records(file_name: str, content: bytes, *, sheet_name: str | int = 0,
                        json_records_path: str | None = None) -> list[dict[str, Any]]:
    """Read sample bytes into records without coercing identifier strings.

    JSON accepts an array of objects or an object containing an array. For nested
    JSON, json_records_path is a dot-separated key path. Empty files, duplicate
    headers, and non-object JSON records are rejected instead of silently dropped.
    """
    if not file_name or not content:
        raise SourceFileError("A non-empty source filename and file content are required.")
    suffix = PurePath(file_name).suffix.lower()
    if suffix not in SUPPORTED_SOURCE_EXTENSIONS:
        raise SourceFileError(
            f"Unsupported source format '{suffix or '[no extension]'}'. "
            f"Supported formats: {', '.join(supported_source_formats())}."
        )
    try:
        if suffix == ".json":
            payload = json.loads(content.decode("utf-8-sig"))
            if json_records_path:
                for part in json_records_path.split("."):
                    if not isinstance(payload, dict) or part not in payload:
                        raise SourceFileError(f"JSON records path '{json_records_path}' was not found.")
                    payload = payload[part]
            elif isinstance(payload, dict):
                if "records" in payload:
                    payload = payload["records"]
                else:
                    raise SourceFileError("JSON object input must contain a 'records' array or use json_records_path.")
            if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
                raise SourceFileError("JSON input must resolve to an array of objects.")
            return [{str(k): _stringify(v) for k, v in row.items()} for row in payload]
        if suffix in {".csv", ".tsv"}:
            separator = "\t" if suffix == ".tsv" else ","
            try:
                raw_header = next(csv.reader(io.StringIO(content.decode("utf-8-sig")), delimiter=separator))
            except (UnicodeDecodeError, StopIteration) as exc:
                raise SourceFileError("Source file has no readable header row.") from exc
            normalized_header = [str(value).strip() for value in raw_header]
            if len(set(normalized_header)) != len(normalized_header):
                raise SourceFileError("Source file contains duplicate column headers.")
            if any(not header for header in normalized_header):
                raise SourceFileError("Source file contains an empty column header.")
            frame = pd.read_csv(io.BytesIO(content), sep=separator, dtype=str,
                                keep_default_na=False, encoding="utf-8-sig")
        else:
            frame = pd.read_excel(io.BytesIO(content), sheet_name=sheet_name, dtype=str,
                                  keep_default_na=False)
    except SourceFileError:
        raise
    except Exception as exc:
        raise SourceFileError(f"Could not parse '{file_name}': {exc}") from exc
    if len(frame.columns) == 0:
        raise SourceFileError("Source file has no column headers.")
    headers = [str(column).strip() for column in frame.columns]
    if any(not header for header in headers):
        raise SourceFileError("Source file contains an empty column header.")
    if len(set(headers)) != len(headers):
        raise SourceFileError("Source file contains duplicate column headers.")
    frame.columns = headers
    return [{str(key): _stringify(value) for key, value in row.items()}
            for row in frame.to_dict(orient="records")]

def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)
