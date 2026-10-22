"""SQLite-backed workflow events and idempotent durable projections."""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from control_plane.db import get_db_connection, init_db


def _ensure_event_table() -> None:
    init_db()
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_events (
                event_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL,
                event_type TEXT NOT NULL, aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                actor TEXT NOT NULL, occurred_utc TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_workflow_events_migration_time
            ON workflow_events (migration_id, occurred_utc, event_id)""")
        conn.execute("""CREATE INDEX IF NOT EXISTS idx_workflow_events_aggregate
            ON workflow_events (aggregate_type, aggregate_id, occurred_utc)""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_state_projection (
                migration_id TEXT NOT NULL, aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL, state_json TEXT NOT NULL,
                last_event_id TEXT NOT NULL, updated_utc TEXT NOT NULL,
                PRIMARY KEY (migration_id, aggregate_type, aggregate_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_projection_receipts (
                event_id TEXT PRIMARY KEY, processed_utc TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _apply_projection(conn, event: Dict[str, Any]) -> None:
    """Apply one event once. The projection is rebuildable from the append-only log."""
    receipt = conn.execute(
        "SELECT 1 FROM workflow_projection_receipts WHERE event_id = ?",
        (event["event_id"],),
    ).fetchone()
    if receipt:
        return
    key = (event["migration_id"], event["aggregate_type"], event["aggregate_id"])
    row = conn.execute(
        """SELECT state_json FROM workflow_state_projection
           WHERE migration_id = ? AND aggregate_type = ? AND aggregate_id = ?""", key
    ).fetchone()
    state = json.loads(row["state_json"]) if row else {}
    state.setdefault("events", [])
    state["last_event_type"] = event["event_type"]
    state["last_event_id"] = event["event_id"]
    state["last_actor"] = event["actor"]
    state["updated_utc"] = event["occurred_utc"]
    # Keep a latest-value view of workflow facts for screens/services to consume.
    payload = event["payload"]
    if event["event_type"] == "MIGRATION_CONTEXT_UPDATED":
        state["context"] = payload
    elif event["event_type"] == "ENTITY_SCOPES_UPDATED":
        state["scopes"] = payload.get("scopes", {})
    elif event["event_type"] == "SOURCE_FILE_UPLOADED":
        files = state.setdefault("source_files", {})
        files[event["aggregate_id"]] = payload
    elif event["event_type"] == "MAPPING_REVIEWED":
        mappings = state.setdefault("mapping_reviews", {})
        mappings[event["aggregate_id"]] = payload
    elif event["event_type"] == "ENTITY_MAPPINGS_APPROVED":
        approvals = state.setdefault("entity_mapping_approvals", {})
        approvals[event["aggregate_id"]] = payload
    elif event["event_type"] == "MAPPING_INFORMATION_REQUEST_CHANGED":
        requests = state.setdefault("information_requests", {})
        requests[event["aggregate_id"]] = payload
    elif event["event_type"] == "ENTITY_INFERENCE_REVIEWED":
        state["inference_review"] = payload
    elif event["event_type"] == "KNOWLEDGE_ASSET_REGISTERED":
        assets = state.setdefault("knowledge_assets", {})
        assets[event["aggregate_id"]] = payload
    elif event["event_type"] == "MAPPING_PROPOSALS_GENERATED":
        state["mapping_proposal_run"] = payload
    elif event["event_type"] == "MCL_EXECUTION_SUMMARY_PERSISTED":
        state["mcl_execution"] = payload
    state["latest_payload"] = payload
    # Store bounded event references, not an unbounded duplicate of the event log.
    state["events"] = (state["events"] + [event["event_id"]])[-100:]
    conn.execute(
        """INSERT INTO workflow_state_projection
           (migration_id, aggregate_type, aggregate_id, state_json, last_event_id, updated_utc)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(migration_id, aggregate_type, aggregate_id) DO UPDATE SET
             state_json = excluded.state_json, last_event_id = excluded.last_event_id,
             updated_utc = excluded.updated_utc""",
        (*key, json.dumps(state), event["event_id"], event["occurred_utc"]),
    )
    conn.execute(
        "INSERT INTO workflow_projection_receipts (event_id, processed_utc) VALUES (?, ?)",
        (event["event_id"], datetime.now(timezone.utc).isoformat()),
    )


def publish_event(
    event_type: str, migration_id: str, aggregate_type: str, aggregate_id: str,
    payload: Optional[Dict[str, Any]] = None, actor: str = "system",
) -> Dict[str, Any]:
    """Append event and update its projection atomically in the same SQLite transaction."""
    if not event_type or not migration_id or not aggregate_type or not aggregate_id:
        raise ValueError("event_type, migration_id, aggregate_type and aggregate_id are required")
    _ensure_event_table()
    event = {
        "event_id": f"EVT-{uuid.uuid4().hex}", "migration_id": str(migration_id),
        "event_type": str(event_type), "aggregate_type": str(aggregate_type),
        "aggregate_id": str(aggregate_id), "payload": payload or {},
        "actor": str(actor or "system"),
        "occurred_utc": datetime.now(timezone.utc).isoformat(), "schema_version": 1,
    }
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO workflow_events
            (event_id, migration_id, event_type, aggregate_type, aggregate_id,
             payload_json, actor, occurred_utc, schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event["event_id"], event["migration_id"], event["event_type"],
             event["aggregate_type"], event["aggregate_id"], json.dumps(event["payload"]),
             event["actor"], event["occurred_utc"], event["schema_version"]),
        )
        _apply_projection(conn, event)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return event


def read_events(migration_id: str, after_event_id: Optional[str] = None, limit: int = 500) -> list[Dict[str, Any]]:
    """Read events in deterministic order; an event ID can be used as a replay checkpoint."""
    _ensure_event_table()
    limit = max(1, min(int(limit), 5000))
    conn = get_db_connection()
    try:
        if after_event_id:
            checkpoint = conn.execute(
                "SELECT occurred_utc FROM workflow_events WHERE event_id = ? AND migration_id = ?",
                (after_event_id, migration_id),
            ).fetchone()
            if checkpoint is None:
                raise ValueError("after_event_id was not found for this migration")
            rows = conn.execute(
                """SELECT * FROM workflow_events WHERE migration_id = ?
                   AND (occurred_utc > ? OR (occurred_utc = ? AND event_id > ?))
                   ORDER BY occurred_utc, event_id LIMIT ?""",
                (migration_id, checkpoint["occurred_utc"], checkpoint["occurred_utc"], after_event_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workflow_events WHERE migration_id = ? ORDER BY occurred_utc, event_id LIMIT ?",
                (migration_id, limit),
            ).fetchall()
        return [{
            "event_id": row["event_id"], "migration_id": row["migration_id"],
            "event_type": row["event_type"], "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"], "payload": json.loads(row["payload_json"] or "{}"),
            "actor": row["actor"], "occurred_utc": row["occurred_utc"],
            "schema_version": row["schema_version"],
        } for row in rows]
    finally:
        conn.close()


def replay_events(migration_id: str, after_event_id: Optional[str] = None, limit: int = 5000) -> int:
    """Idempotently rebuild/update projections from the durable event journal."""
    events = read_events(migration_id, after_event_id=after_event_id, limit=limit)
    conn = get_db_connection()
    try:
        for event in events:
            _apply_projection(conn, event)
        conn.commit()
        return len(events)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_projection(migration_id: str, aggregate_type: str, aggregate_id: str) -> Dict[str, Any]:
    """Read a durable projection, usable after a process restart."""
    _ensure_event_table()
    conn = get_db_connection()
    try:
        row = conn.execute(
            """SELECT state_json FROM workflow_state_projection
               WHERE migration_id = ? AND aggregate_type = ? AND aggregate_id = ?""",
            (migration_id, aggregate_type, aggregate_id),
        ).fetchone()
        return json.loads(row["state_json"]) if row else {}
    finally:
        conn.close()


def latest_event_sequence(migration_id: str) -> int:
    _ensure_event_table()
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM workflow_events WHERE migration_id = ?", (migration_id,)).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()
