"""The live target database (Simplicity): where validated batches are promoted to.

Set up once by the Platform Admin, next to the XODUS database connection. XODUS only reads it: the table and column list for the
target model and the deployment check, and row counts after a promotion. Writing is Data Factory's job (its managed identity). Only
non-secret settings are stored; the password is read from a server environment variable named here.
"""
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from control_plane.services import sql_target

_DDL = """
CREATE TABLE IF NOT EXISTS live_target_config (
    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
    server TEXT NOT NULL, database_name TEXT NOT NULL, username TEXT NOT NULL, password_env_var TEXT NOT NULL, schema_name TEXT NOT NULL,
    status TEXT NOT NULL, checked_utc TEXT, check_detail TEXT, configured_by TEXT NOT NULL, configured_utc TEXT NOT NULL
)
"""
FIELDS = sql_target.FIELDS
DEFAULT_DATABASE = "Simplicity"

# The schema the current request works on. The connection (server, database, login) is platform-level; the schema is a choice of
# each migration (a Platform Admin picks the one they manage while setting the target up). The app sets it at the start of every
# request; empty means the schema saved with the connection.
_current_schema = ""
SYSTEM_SCHEMAS = {"dbo", "guest", "sys", "information_schema"}
HIDDEN_PREFIXES = ("xodus_", "db_")


def set_current_schema(name: str) -> None:
    global _current_schema
    _current_schema = str(name or "").strip()


def current_schema() -> str:
    return _current_schema


def effective_schema(conn: sqlite3.Connection) -> str:
    """The schema this request works on: the migration's choice, else the one saved with the live connection, else empty."""
    if _current_schema:
        return _current_schema
    ensure_table(conn)
    row = conn.execute("SELECT schema_name FROM live_target_config WHERE config_id = 1").fetchone()
    return str(row["schema_name"] if row else "")


def list_schemas(cfg: Dict[str, Any], connect: Callable[..., Any] = sql_target._default_connect) -> list:
    """Schemas of the live database that hold tables a migration could load into (system and XODUS schemas left out). Catalog only."""
    rows = sql_target.run(cfg, [("SELECT DISTINCT TABLE_SCHEMA FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_SCHEMA", ())], connect)[0]
    return [str(r[0]) for r in rows if str(r[0]).lower() not in SYSTEM_SCHEMAS and not str(r[0]).lower().startswith(HIDDEN_PREFIXES)]


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def get_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM live_target_config WHERE config_id = 1").fetchone()
    if not row:
        return None
    cfg = dict(row)
    cfg["saved_schema_name"] = cfg["schema_name"]
    if _current_schema:
        cfg["schema_name"] = _current_schema  # the migration's schema on the same connection
    cfg["password_present"] = bool(os.environ.get(cfg["password_env_var"]))
    cfg["username_effective"] = cfg["username"] or os.environ.get(sql_target.USERNAME_ENV, "")
    return cfg


def save(conn: sqlite3.Connection, actor: str, values: Dict[str, str]) -> Dict[str, Any]:
    ensure_table(conn)
    v = sql_target.validate({**values, "database_name": values.get("database_name") or DEFAULT_DATABASE})
    conn.execute("DELETE FROM live_target_config WHERE config_id = 1")
    conn.execute("INSERT INTO live_target_config (config_id, " + ", ".join(FIELDS) + ", status, configured_by, configured_utc) VALUES (1, "
                 + ", ".join("?" for _ in FIELDS) + ", 'CONFIGURED', ?, ?)", (*[v[k] for k in FIELDS], actor, datetime.now(timezone.utc).isoformat()))
    return get_config(conn)


def read_columns(cfg: Dict[str, Any], connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """{table: {column: {"type", "max_length", "nullable"}}} for the live schema. Catalog only."""
    rows = sql_target.run(cfg, [("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                                 "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME, ORDINAL_POSITION", (cfg["schema_name"],))], connect)[0]
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for table, column, dtype, length, nullable in rows:
        out.setdefault(table, {})[column] = {"type": str(dtype).lower(), "max_length": length, "nullable": str(nullable).upper() == "YES"}
    return out


def test_connection(conn: sqlite3.Connection, connect: Callable[..., Any] = sql_target._default_connect) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Save the live database settings first.")
    try:
        tables = read_columns(cfg, connect)
        status, detail = "CONNECTED", (f"Signed in to {cfg['database_name']}; schema {cfg['schema_name']} has {len(tables)} table(s): "
                                       + ", ".join(sorted(tables)[:8]) + ("" if len(tables) <= 8 else ", ...") if tables
                                       else f"Signed in to {cfg['database_name']}, but schema {cfg['schema_name']} has no tables yet.")
    except ValueError as exc:
        status, detail = "FAILED", str(exc)
    conn.execute("UPDATE live_target_config SET status = ?, checked_utc = ?, check_detail = ? WHERE config_id = 1",
                 (status, datetime.now(timezone.utc).isoformat(), detail))
    return get_config(conn)
