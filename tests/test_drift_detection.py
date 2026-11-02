from control_plane.services import drift_detection as dd

MAPS = [{"entity_name": "Client", "source_field": "LEGAL_NAME", "canonical_field": "LEGAL_NAME"},
        {"entity_name": "Client", "source_field": "TAX_IDENTIFIER", "canonical_field": "TAX_ID"},
        {"entity_name": "Account", "source_field": "ACCT_NUM", "canonical_field": "ACCT_NUM"}]


def test_a_mapped_field_missing_from_current_files_is_reported_without_values():
    files = {"Client": {"ENTITY_MASTER": {"filename": "clients.csv", "columns": ["CLIENT_ID", "Legal Name"], "inference_approval_status": "APPROVED"}}}
    out = dd.detect(MAPS, files)
    assert [(f["entity"], f["source_field"]) for f in out] == [("Client", "TAX_IDENTIFIER")]   # renamed spelling still matches; Account has no files
    assert "not present" in out[0]["detail"]


def test_columns_from_another_file_of_the_entity_count_and_rejected_files_do_not():
    files = {"Client": {"A": {"filename": "a.csv", "columns": ["LEGAL_NAME"], "inference_approval_status": "APPROVED"},
                        "B": {"filename": "b.csv", "columns": ["TAX_IDENTIFIER"], "inference_approval_status": "REJECTED"}}}
    assert [f["source_field"] for f in dd.detect(MAPS, files)] == ["TAX_IDENTIFIER"]
    files["Client"]["C"] = {"filename": "c.csv", "columns": ["tax_identifier"]}
    assert dd.detect(MAPS, files) == []
    assert dd.detect([], {}) == []
