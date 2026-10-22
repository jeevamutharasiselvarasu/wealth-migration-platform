from control_plane.services.entity_classification_agent import classify_source_file


def test_blind_pack_expected_classification_without_context_or_dictionary():
    cases = [
        (["ACCT_REF", "REG_CODE", "OPENED_ON", "STATUS_CODE"],
         [{"ACCT_REF": "A001", "REG_CODE": "IRA", "OPENED_ON": "2024-01-01", "STATUS_CODE": "OPEN"},
          {"ACCT_REF": "A002", "REG_CODE": "TAXABLE", "OPENED_ON": "2023-02-03", "STATUS_CODE": "OPEN"}],
         "Account", "INFERRED"),
        (["GROUP_REF", "GROUP_LABEL", "LEAD_REF", "REVIEW_DATE"],
         [{"GROUP_REF": "G001", "GROUP_LABEL": "Smith Family", "LEAD_REF": "C001", "REVIEW_DATE": "2025-01-01"},
          {"GROUP_REF": "G002", "GROUP_LABEL": "Jones Family", "LEAD_REF": "C002", "REVIEW_DATE": "2025-02-01"}],
         "Household", "INFERRED"),
        (["PARTY_REF", "GIVEN_NM", "SURNAME", "RECORD_KIND", "ACTIVE_FLAG"],
         [{"PARTY_REF": "P001", "GIVEN_NM": "Ava", "SURNAME": "Smith", "RECORD_KIND": "INDIVIDUAL", "ACTIVE_FLAG": "Y"},
          {"PARTY_REF": "P002", "GIVEN_NM": "Noah", "SURNAME": "Jones", "RECORD_KIND": "INDIVIDUAL", "ACTIVE_FLAG": "Y"}],
         "Client", "INFERRED"),
        (["OWNER_REF", "OBJECT_REF", "LINK_KIND", "VALID_FROM", "VALID_TO"],
         [{"OWNER_REF": "C001", "OBJECT_REF": "H001", "LINK_KIND": "MEMBER_OF", "VALID_FROM": "2024-01-01", "VALID_TO": ""},
          {"OWNER_REF": "C002", "OBJECT_REF": "H002", "LINK_KIND": "MEMBER_OF", "VALID_FROM": "2024-01-01", "VALID_TO": ""}],
         "Relationship", "RELATIONSHIP_REVIEW"),
        (["REF_A", "REF_B", "TYPE_CD", "START_DT", "END_DT"],
         [{"REF_A": "A001", "REF_B": "C001", "TYPE_CD": "OWNED_BY", "START_DT": "2024-01-01", "END_DT": ""},
          {"REF_A": "A002", "REF_B": "C002", "TYPE_CD": "OWNED_BY", "START_DT": "2024-01-01", "END_DT": ""}],
         "Relationship", "RELATIONSHIP_REVIEW"),
        (["CLIENT_REF", "DISPLAY_TXT", "EMAIL_TXT", "CHANNEL_CD", "UPDATED_AT"],
         [{"CLIENT_REF": "C001", "DISPLAY_TXT": "Ava Smith", "EMAIL_TXT": "ava@example.test", "CHANNEL_CD": "EMAIL", "UPDATED_AT": "2025-01-01"},
          {"CLIENT_REF": "C002", "DISPLAY_TXT": "Noah Jones", "EMAIL_TXT": "noah@example.test", "CHANNEL_CD": "EMAIL", "UPDATED_AT": "2025-01-02"}],
         "Client", "INFERRED"),
    ]
    results = []
    for headers, records, expected, expected_status in cases:
        result = classify_source_file(headers, records=records)
        results.append((expected, result["proposed_classification"], expected_status, result["status"]))
        assert result["proposed_classification"] == expected, result
        assert result["status"] == expected_status, result
        assert result["approval_status"] == "PENDING"
        assert "competing_candidates" in result
        assert "review_reasons" in result
        assert "matched_evidence" in result
        assert result["profile"]["row_count"] == len(records)
    assert len(results) == 6


def test_generic_headers_remain_ambiguous_without_context():
    result = classify_source_file(
        ["ID", "NAME", "STATUS"],
        records=[{"ID": "1", "NAME": "Example", "STATUS": "ACTIVE"}],
    )
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert result["proposed_classification"] is None


def test_relationship_files_are_not_assigned_a_master_entity():
    result = classify_source_file(
        ["OWNER_REF", "OBJECT_REF", "LINK_KIND", "VALID_FROM", "VALID_TO"],
        records=[{"OWNER_REF": "A1", "OBJECT_REF": "C1", "LINK_KIND": "OWNS", "VALID_FROM": "2024-01-01", "VALID_TO": ""}],
    )
    assert result["classification_type"] == "RELATIONSHIP"
    assert result["entity"] is None
    assert result["approval_status"] == "PENDING"
