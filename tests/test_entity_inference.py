from control_plane.services.entity_inference import infer_entity_from_headers

def test_infers_account_from_account_identity_columns():
    result = infer_entity_from_headers(["ACCOUNT_NUMBER", "CUSTODIAN", "OPEN_DATE", "REP_CODE"])
    assert result["entity"] == "Account"
    assert result["status"] == "NEEDS_REVIEW"

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
    assert result["status"] == "INFERRED"
