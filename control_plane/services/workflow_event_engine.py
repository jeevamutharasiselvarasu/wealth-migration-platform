"""Durable event log for cross-module workflow propagation.

The event log is append-only and SQLite-backed so transitions survive process restarts.
Consumers should derive current status from durable domain tables; events are the
change journal and trigger for projections, not a second source of truth.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from control_plane.db import get_db_connection, init_db


def _ensure_event_table() -> None:
    init_db()
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflow_events (
                event_id TEXT PRIMARY KEY,
                migration_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_utc TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workflow_events_migration_time
            ON workflow_events (migration_id, occurred_utc, event_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workflow_events_aggregate
            ON workflow_events (aggregate_type, aggregate_id, occurred_utc)
        """)
        conn.commit()
    finally:
        conn.close()


def publish_event(
    event_type: str,
    migration_id: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Optional[Dict[str, Any]] = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """Append one durable workflow event and return its normalized envelope."""
    if not event_type or not migration_id or not aggregate_type or not aggregate_id:
        raise ValueError("event_type, migration_id, aggregate_type and aggregate_id are required")
    _ensure_event_table()
    event = {
        "event_id": f"EVT-{uuid.uuid4().hex}",
        "migration_id": str(migration_id),
        "event_type": str(event_type),
        "aggregate_type": str(aggregate_type),
        "aggregate_id": str(aggregate_id),
        "payload": payload or {},
        "actor": str(actor or "system"),
        "occurred_utc": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
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
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return event


def read_events(
    migration_id: str,
    after_event_id: Optional[str] = None,
    limit: int = 500,
) -> list[Dict[str, Any]]:
    """Read ordered events for replay/projection rebuilding."""
    _ensure_event_table()
    limit = max(1, min(int(limit), 5000))
    conn = get_db_connection()
    try:
        if after_event_id:
            cursor = conn.execute(
                "SELECT occurred_utc FROM workflow_events WHERE event_id = ? AND migration_id = ?",
                (after_event_id, migration_id),
            ).fetchone()
            if cursor is None:
                raise ValueError("after_event_id was not found for this migration")
            rows = conn.execute(
                """SELECT * FROM workflow_events
                WHERE migration_id = ? AND (occurred_utc > ? OR
                    (occurred_utc = ? AND event_id > ?))
                ORDER BY occurred_utc, event_id LIMIT ?""",
                (migration_id, cursor["occurred_utc"], cursor["occurred_utc"], after_event_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM workflow_events WHERE migration_id = ?
                ORDER BY occurred_utc, event_id LIMIT ?""",
                (migration_id, limit),
            ).fetchall()
        return [{
            "event_id": row["event_id"],
            "migration_id": row["migration_id"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "actor": row["actor"],
            "occurred_utc": row["occurred_utc"],
            "schema_version": row["schema_version"],
        } for row in rows]
    finally:
        conn.close()


def latest_event_sequence(migration_id: str) -> int:
    """Return a monotonic count suitable for lightweight projection freshness checks."""
    _ensure_event_table()
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_events WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()
