import json

from ui.state import WorkspaceStateStore


def _extract(payload, filename="dictionary.json"):
    store = WorkspaceStateStore()
    record = store.add_firm_file(
        filename,
        "data_dictionary",
        json.dumps(payload).encode("utf-8"),
    )
    return record["extracted_columns"]


def test_knowledge_json_extracts_field_name_array():
    assert _extract({"columns": ["ACCOUNT_ID", "CLIENT_ID"]}) == ["ACCOUNT_ID", "CLIENT_ID"]


def test_knowledge_json_extracts_nested_json_schema_properties():
    payload = {
        "definitions": {
            "Account": {
                "properties": {
                    "ACCT_NUM": {"type": "string", "description": "Account identifier"},
                    "CUSTODIAN_CODE": {"type": "string"},
                }
            }
        }
    }
    assert _extract(payload) == ["ACCT_NUM", "CUSTODIAN_CODE"]


def test_knowledge_json_extracts_dictionary_records_by_field_name():
    payload = {
        "fields": [
            {"field_name": "CLIENT_ID", "description": "Client identifier"},
            {"field_name": "LEGAL_NAME", "description": "Legal name"},
        ]
    }
    assert _extract(payload) == ["CLIENT_ID", "LEGAL_NAME"]


def test_knowledge_json_extracts_nested_records():
    payload = {"metadata": {"schema": {"columns": ["HH_ID", "HH_NAME"]}}}
    assert _extract(payload) == ["HH_ID", "HH_NAME"]



def test_knowledge_asset_summary_and_status_reflect_extracted_columns():
    store = WorkspaceStateStore()
    record = store.add_firm_file(
        "account_dictionary.json",
        "data_dictionary",
        json.dumps({"columns": ["ACCT_NUM", "CUSTODIAN_CODE"]}).encode("utf-8"),
    )

    assert record["extracted_columns"] == ["ACCT_NUM", "CUSTODIAN_CODE"]
    assert "2 column names" in record["parsed_summary"]
    assert "ACCT_NUM" in record["parsed_summary"]
    assert record["status"] == "REGISTERED_REVIEW_REQUIRED"


def test_knowledge_asset_rejects_malformed_json():
    store = WorkspaceStateStore()
    try:
        store.add_firm_file("broken_dictionary.json", "data_dictionary", b"{not-json")
    except ValueError as exc:
        assert "Could not parse knowledge asset" in str(exc)
    else:
        raise AssertionError("Malformed JSON dictionary should be rejected")
