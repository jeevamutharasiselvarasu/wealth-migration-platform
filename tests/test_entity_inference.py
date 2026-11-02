from control_plane.services.entity_inference import infer_entity_from_headers

def test_infers_account_from_account_identity_columns():
    result = infer_entity_from_headers(["ACCOUNT_NUMBER", "CUSTODIAN", "OPEN_DATE", "REP_CODE"])
    assert result["entity"] == "Account"
    assert result["status"] == "INFERRED"

def test_infers_household_from_household_identity_columns():
    result = infer_entity_from_headers(["HOUSEHOLD_ID", "HOUSEHOLD_NAME", "REP_CODE"])
    assert result["entity"] == "Household"
    assert result["status"] == "INFERRED"

def test_infers_client_from_client_identity_columns():
    result = infer_entity_from_headers(["CLIENT_ID", "LEGAL_NAME", "CLIENT_TYPE", "TAX_ID"])
    assert result["entity"] == "Client"
    assert result["status"] == "INFERRED"

def test_ambiguous_file_requests_more_information_instead_of_guessing():
    result = infer_entity_from_headers(["ID", "NAME", "STATUS"])
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert result["entity"] is None

def test_context_can_disambiguate_generic_headers_when_source_dictionary_is_explicit():
    result = infer_entity_from_headers(["ID", "NAME", "STATUS"], context="This is the household register. Household ID and household name are defined in the dictionary.")
    assert result["entity"] == "Household"
    assert result["status"] == "NEEDS_REVIEW"
def test_demo_manifest_source_files_have_entity_inference_evidence():
    import json
    from pathlib import Path
    from control_plane.services.source_file_adapter import read_source_records

    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "demo_data" / "demo_migration.json").read_text(encoding="utf-8"))
    assert manifest["source_files"], "Demo manifest must contain source files"

    for item in manifest["source_files"]:
        path = root / item["path"]
        records = read_source_records(path.name, path.read_bytes())
        headers = list(records[0].keys()) if records else []
        result = infer_entity_from_headers(headers, context=str(item.get("context", "")))
        assert result.get("entity") in {"Account", "Household", "Client"}, (
            f"{path.name} has no supported entity inference: {result}"
        )
        assert result.get("status") != "NEEDS_CLARIFICATION", (
            f"{path.name} needs more entity evidence: {result}"
        )

