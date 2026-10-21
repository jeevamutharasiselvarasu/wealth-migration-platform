from agents.p0_agent_suite import MigrationAgentCoordinator, P0_ENTITIES


def test_agent_coordinator_recommends_without_approving_or_executing():
    result = MigrationAgentCoordinator().analyze(
        [{"LEGACY_ACCT_NO": "000123", "ACCOUNT_ID": "000123", "LEGACY_CLIENT_ID": "C-1"}],
        context="CoreCap account master extract",
        in_scope=["Account"],
    )
    assert result["source_firm"] == "CoreCap"
    assert result["target_firm"] == "Simplicity"
    assert result["classification"]["entity"] == "Account"
    assert all(item["approved"] is False for item in result["mapping"]["proposals"])
    assert result["execution"] == {"target_loading": "DEFERRED", "wave_assignment": "DEFERRED"}
    assert result["review"]["human_approval_required"] is True


def test_scope_selection_blocks_mapping_for_unselected_entity():
    result = MigrationAgentCoordinator().analyze(
        [{"CLIENT_ID": "C-1", "LEGAL_NAME": "Synthetic Client"}],
        context="CoreCap client master extract",
        in_scope=["Account"],
    )
    assert result["classification"]["status"] == "OUT_OF_SCOPE"
    assert result["mapping"]["status"] == "BLOCKED_CLASSIFICATION"
    assert result["mapping"]["proposals"] == []


def test_ambiguous_entity_does_not_receive_mapping_suggestions():
    result = MigrationAgentCoordinator().analyze(
        [{"ID": "1", "STATUS": "ACTIVE"}], context="unknown extract",
        in_scope=list(P0_ENTITIES),
    )
    assert result["classification"]["entity"] is None
    assert result["mapping"]["status"] == "BLOCKED_CLASSIFICATION"
    assert result["review"]["human_approval_required"] is True


def test_profile_preserves_leading_zeroes_as_strings():
    result = MigrationAgentCoordinator().analyze(
        [{"LEGACY_ACCT_NO": "0000123", "ACCOUNT_TYPE": "BROKERAGE"}],
        context="CoreCap account master extract", in_scope=["Account"],
    )
    account_id = next(c for c in result["profile"]["columns"] if c["field"] == "LEGACY_ACCT_NO")
    assert account_id["sample_values"] == ["0000123"]
