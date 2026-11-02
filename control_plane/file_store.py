"""Where uploaded source files and generated artifacts keep their bytes.

SQLite mode (default): files stay on disk exactly as before.
PostgreSQL mode (DATABASE_URL set): the bytes live in the `stored_files` table, so they survive the web host's disk being wiped.
A stored location is either a filesystem path or a `db://...` locator; callers treat it as an opaque string.
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from control_plane.db import get_db_connection, using_postgres

DB_PREFIX = "db://"
_DDL = """CREATE TABLE IF NOT EXISTS stored_files (
    locator TEXT PRIMARY KEY, content BYTEA NOT NULL, size_bytes INTEGER NOT NULL, content_sha256 TEXT NOT NULL, created_utc TEXT NOT NULL)"""


def put_db(conn, locator: str, data: bytes) -> str:
    conn.execute(_DDL)
    conn.execute("INSERT INTO stored_files (locator, content, size_bytes, content_sha256, created_utc) VALUES (?, ?, ?, ?, ?) "
                 "ON CONFLICT (locator) DO UPDATE SET content = excluded.content, size_bytes = excluded.size_bytes, "
                 "content_sha256 = excluded.content_sha256, created_utc = excluded.created_utc",
                 (locator, data, len(data), hashlib.sha256(data).hexdigest(), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return locator


def get_db(conn, locator: str) -> Optional[bytes]:
    conn.execute(_DDL)
    row = conn.execute("SELECT content FROM stored_files WHERE locator = ?", (locator,)).fetchone()
    return bytes(row[0]) if row else None


def write(directory: Path, name: str, data: bytes) -> Union[Path, str]:
    """Store a new file; returns a Path (disk) or a db:// locator (PostgreSQL)."""
    if using_postgres():
        conn = get_db_connection()
        try:
            locator = put_db(conn, f"{DB_PREFIX}{directory.name}/{name}", data)
            conn.commit()
        finally:
            conn.close()
        return locator
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def _is_db(location) -> bool:
    return isinstance(location, str) and location.startswith(DB_PREFIX)


def is_file(location) -> bool:
    if _is_db(location):
        conn = get_db_connection()
        try:
            return get_db(conn, location) is not None
        finally:
            conn.close()
    return Path(location).is_file()


def read_bytes(location) -> bytes:
    if _is_db(location):
        conn = get_db_connection()
        try:
            data = get_db(conn, location)
        finally:
            conn.close()
        if data is None:
            raise OSError("The stored file is missing from storage.")
        return data
    return Path(location).read_bytes()
