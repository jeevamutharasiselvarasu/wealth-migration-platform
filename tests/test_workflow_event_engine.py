import pytest

from control_plane.db import DB_PATH
from control_plane.services import workflow_event_engine as engine


@pytest.fixture
def isolated_event_db(tmp_path, monkeypatch):
    monkeypatch.setattr("control_plane.db.DB_PATH", tmp_path / "controlplane.db")
    # workflow_event_engine imports DB helpers, which resolve DB_PATH dynamically.
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

    assert [event["event_type"] for event in replay] == [
        "SOURCE_FILE_UPLOADED", "MAPPING_REVIEWED"
    ]
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
