import json

import control_plane.db as db
from control_plane.services.activity_feed import recent_activity


def _event(conn, event_id, mig, etype, payload, actor, when):
    conn.execute("INSERT INTO workflow_events (event_id, migration_id, event_type, aggregate_type, aggregate_id, payload_json, actor, "
                 "occurred_utc, schema_version) VALUES (?, ?, ?, 'x', 'a', ?, ?, ?, 1)",
                 (event_id, mig, etype, json.dumps(payload), actor, when))


def test_activity_feed_is_scoped_readable_collapsed_and_never_leaks_paths(tmp_path, monkeypatch):
    from control_plane.services.workflow_event_engine import _ensure_event_table

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "act.db")
    db.init_db()
    _ensure_event_table()
    conn = db.get_db_connection()
    _event(conn, "E1", "MIG-A", "SOURCE_FILE_UPLOADED", {"filename": "accounts.csv", "stored_path": "C:\\secret\\accounts.csv"}, "Ann", "2026-10-27T10:00:00+00:00")
    for i in range(3):
        _event(conn, f"E{i+2}", "MIG-A", "MAPPING_REVIEWED", {"entity": "Account", "source_field": f"F{i}", "canonical_field": f"C{i}", "decision": "APPROVED"}, "Ann", f"2026-10-27T10:05:0{i}+00:00")
    _event(conn, "E9", "MIG-B", "SOURCE_FILE_UPLOADED", {"filename": "other.csv"}, "Bob", "2026-10-27T11:00:00+00:00")
    conn.execute("INSERT INTO validation_runs (run_id, migration_id, status, results_json, executed_by, executed_utc) "
                 "VALUES ('VAL-1', 'MIG-A', 'PASS', '{}', 'Val', '2026-10-27T10:30:00+00:00')")
    conn.commit()
    conn.close()

    feed = recent_activity("MIG-A")
    titles = [i["title"] for i in feed]
    assert titles == ["Validation run", "Mapping approved ×3", "Source file added"]
    assert "other.csv" not in json.dumps(feed) and "secret" not in json.dumps(feed)
    assert feed[0]["detail"] == "VAL-1 · PASS" and feed[0]["actor"] == "Val"
    assert recent_activity("MIG-NONE") == []
