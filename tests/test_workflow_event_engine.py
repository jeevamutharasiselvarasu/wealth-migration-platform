import pytest

from control_plane.services import workflow_event_engine as engine


@pytest.fixture
def isolated_event_db(tmp_path, monkeypatch):
    monkeypatch.setattr("control_plane.db.DB_PATH", tmp_path / "controlplane.db")
    engine._ensure_event_table()


def test_events_are_durable_and_replayable_in_order(isolated_event_db):
    first = engine.publish_event(
        "SOURCE_FILE_UPLOADED", "MIG-TEST", "source_file", "FILE-1",
        {"entity": "Account", "filename": "accounts.csv"}, "analyst",
    )
    second = engine.publish_event(
        "MAPPING_REVIEWED", "MIG-TEST", "mapping_suggestion", "MAP-1",
        {"decision": "APPROVED"}, "approver",
    )
    replay = engine.read_events("MIG-TEST")
    assert [event["event_type"] for event in replay] == ["SOURCE_FILE_UPLOADED", "MAPPING_REVIEWED"]
    assert replay[0]["event_id"] == first["event_id"]
    assert replay[1]["event_id"] == second["event_id"]
    assert replay[1]["payload"] == {"decision": "APPROVED"}
    assert engine.latest_event_sequence("MIG-TEST") == 2


def test_event_replay_can_resume_after_checkpoint(isolated_event_db):
    first = engine.publish_event("A", "MIG-TEST", "migration", "MIG-TEST")
    engine.publish_event("B", "MIG-TEST", "migration", "MIG-TEST")
    resumed = engine.read_events("MIG-TEST", after_event_id=first["event_id"])
    assert [event["event_type"] for event in resumed] == ["B"]


def test_unknown_checkpoint_is_rejected(isolated_event_db):
    with pytest.raises(ValueError, match="not found"):
        engine.read_events("MIG-TEST", after_event_id="EVT-MISSING")


def test_event_envelope_requires_aggregate_and_migration_identity(isolated_event_db):
    with pytest.raises(ValueError, match="required"):
        engine.publish_event("MAPPING_REVIEWED", "", "mapping", "MAP-1")


def test_projection_survives_reopen_and_replay_is_idempotent(isolated_event_db):
    event = engine.publish_event(
        "ENTITY_SCOPES_UPDATED", "MIG-RESTART", "migration", "MIG-RESTART",
        {"scopes": {"Account": "IN_SCOPE", "Client": "OUT_OF_SCOPE"}}, "analyst",
    )
    before = engine.get_projection("MIG-RESTART", "migration", "MIG-RESTART")
    assert before["scopes"]["Account"] == "IN_SCOPE"
    assert engine.replay_events("MIG-RESTART") == 1
    after = engine.get_projection("MIG-RESTART", "migration", "MIG-RESTART")
    assert after == before
    assert after["last_event_id"] == event["event_id"]


def test_distinct_aggregates_project_independently(isolated_event_db):
    engine.publish_event("SOURCE_FILE_UPLOADED", "MIG-TEST", "source_file", "FILE-1",
                         {"entity": "Account", "filename": "accounts.csv"})
    engine.publish_event("MAPPING_REVIEWED", "MIG-TEST", "mapping_suggestion", "MAP-1",
                         {"decision": "APPROVED"})
    assert engine.get_projection("MIG-TEST", "source_file", "FILE-1")["source_files"]["FILE-1"]["filename"] == "accounts.csv"
    assert engine.get_projection("MIG-TEST", "mapping_suggestion", "MAP-1")["mapping_reviews"]["MAP-1"]["decision"] == "APPROVED"



def test_rebuild_recovers_projection_when_receipts_survive_projection_loss(isolated_event_db):
    event = engine.publish_event(
        "ENTITY_SCOPES_UPDATED", "MIG-REBUILD", "migration", "MIG-REBUILD",
        {"scopes": {"Account": "IN_SCOPE", "Client": "OUT_OF_SCOPE"}}, "analyst",
    )
    conn = engine.get_db_connection()
    try:
        conn.execute("DELETE FROM workflow_state_projection WHERE migration_id = ?", ("MIG-REBUILD",))
        conn.commit()
        # Receipt remains, so incremental replay alone cannot recreate the lost projection.
        assert conn.execute(
            "SELECT 1 FROM workflow_projection_receipts WHERE event_id = ?", (event["event_id"],)
        ).fetchone()
    finally:
        conn.close()

    assert engine.replay_events("MIG-REBUILD") == 1
    assert engine.get_projection("MIG-REBUILD", "migration", "MIG-REBUILD") == {}
    assert engine.replay_events("MIG-REBUILD", rebuild=True) == 1
    restored = engine.get_projection("MIG-REBUILD", "migration", "MIG-REBUILD")
    assert restored["scopes"] == {"Account": "IN_SCOPE", "Client": "OUT_OF_SCOPE"}
    assert restored["last_event_id"] == event["event_id"]
