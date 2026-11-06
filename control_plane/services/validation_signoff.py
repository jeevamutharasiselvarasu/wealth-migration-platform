"""Validation specialist sign-off on a target validation run.

The validation specialist reviews the automated checks (and any accepted exceptions), then signs off the run with a comment.
A sign-off applies to one run only: running target validation again makes the earlier sign-off stale. Nothing here loads or
changes data; it records who accepted which run, when, and why.
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_DDL = """CREATE TABLE IF NOT EXISTS validation_signoffs (
    signoff_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL,
    comment TEXT NOT NULL, signed_by TEXT NOT NULL, signed_utc TEXT NOT NULL)"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def sign_off(conn: sqlite3.Connection, migration_id: str, run_id: str, effective_status: str, comment: str, actor: str,
             latest_run_id: Optional[str]) -> Dict[str, Any]:
    """Record a sign-off. Only the latest run can be signed off, only when its effective status is PASS (failures accepted through an
    approved exception count as passed), and a comment is required."""
    ensure_table(conn)
    comment = str(comment or "").strip()
    if not comment:
        raise ValueError("A sign-off needs a comment.")
    if not latest_run_id or run_id != latest_run_id:
        raise ValueError("Only the latest target validation run can be signed off. Run target validation again if it is out of date.")
    if effective_status != "PASS":
        raise ValueError(f"The run is {effective_status}, not PASS. Fix the findings or have an exception approved before signing off.")
    sid = "SO-" + uuid.uuid4().hex[:8].upper()
    when = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO validation_signoffs (signoff_id, migration_id, run_id, status, comment, signed_by, signed_utc) VALUES (?, ?, ?, 'SIGNED_OFF', ?, ?, ?)",
                 (sid, migration_id, run_id, comment[:1000], actor, when))
    return {"signoff_id": sid, "run_id": run_id, "signed_by": actor, "signed_utc": when, "comment": comment[:1000]}


def current(conn: sqlite3.Connection, migration_id: str, latest_run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The sign-off for the latest run, or None (never signed off, or the run changed since)."""
    ensure_table(conn)
    if not latest_run_id:
        return None
    row = conn.execute("SELECT * FROM validation_signoffs WHERE migration_id = ? AND run_id = ? ORDER BY signed_utc DESC LIMIT 1", (migration_id, latest_run_id)).fetchone()
    return dict(row) if row else None
