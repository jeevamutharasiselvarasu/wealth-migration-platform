import io
import json

import pandas as pd
import pytest

from control_plane.services.source_file_adapter import (
    SourceFileError,
    read_source_records,
    supported_source_formats,
)


def test_csv_preserves_leading_zero_identifiers():
    rows = read_source_records("accounts.csv", b"ACCOUNT_NO,STATUS\n000012,OPEN\n")
    assert rows == [{"ACCOUNT_NO": "000012", "STATUS": "OPEN"}]


def test_tsv_is_supported():
    rows = read_source_records("accounts.tsv", b"ACCOUNT_NO\tSTATUS\n000012\tOPEN\n")
    assert rows[0]["ACCOUNT_NO"] == "000012"


def test_excel_xlsx_is_supported_and_preserves_text_ids():
    buffer = io.BytesIO()
    pd.DataFrame({"ACCOUNT_NO": ["000012"], "STATUS": ["OPEN"]}).to_excel(
        buffer, index=False, engine="openpyxl"
    )
    rows = read_source_records("accounts.xlsx", buffer.getvalue())
    assert rows[0]["ACCOUNT_NO"] == "000012"


def test_json_array_of_objects_is_supported():
    rows = read_source_records("accounts.json", b'[{"ACCOUNT_NO":"000012"}]')
    assert rows == [{"ACCOUNT_NO": "000012"}]


def test_json_object_records_path_is_supported():
    rows = read_source_records(
        "accounts.json", json.dumps({"payload": {"items": [{"id": "001"}]}}).encode(),
        json_records_path="payload.items",
    )
    assert rows == [{"id": "001"}]


def test_json_object_without_records_array_is_rejected():
    with pytest.raises(SourceFileError, match="records"):
        read_source_records("accounts.json", b'{"id":"001"}')


def test_unsupported_format_lists_supported_formats():
    with pytest.raises(SourceFileError, match="Supported formats"):
        read_source_records("accounts.parquet", b"not-parquet")
    assert {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".json"} <= set(supported_source_formats())


def test_empty_upload_is_rejected():
    with pytest.raises(SourceFileError, match="non-empty"):
        read_source_records("accounts.csv", b"")


def test_duplicate_headers_are_rejected():
    with pytest.raises(SourceFileError, match="duplicate"):
        read_source_records("accounts.csv", b"ACCOUNT_NO,ACCOUNT_NO\n001,002\n")
