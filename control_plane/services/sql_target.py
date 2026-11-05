"""Connection to the target Azure SQL database (read-only check that the target schema and tables exist).

Only non-secret settings are stored (server, database, user name, schema and the NAME of the environment variable that holds the
password). The password itself is never accepted from a form, stored, logged or returned. The test opens one connection, reads the
catalog and closes it; it creates, changes and deletes nothing. The create scripts in the ADF package are run by a person."""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

_DDL = """
CREATE TABLE IF NOT EXISTS sql_target_config (
    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
    server TEXT NOT NULL, database_name TEXT NOT NULL, username TEXT NOT NULL, password_env_var TEXT NOT NULL, schema_name TEXT NOT NULL,
    status TEXT NOT NULL, checked_utc TEXT, check_detail TEXT, found_tables TEXT, configured_by TEXT NOT NULL, configured_utc TEXT NOT NULL
)
"""
FIELDS = ("server", "database_name", "username", "password_env_var", "schema_name")
SERVER = re.compile(r"^[a-z0-9][a-z0-9.-]{0,100}\.database\.windows\.net$")
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,99}$")
ENV_VAR = re.compile(r"^SQL_[A-Z0-9_]{1,50}$")
SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,59}$")
DEFAULT_ENV = "SQL_TARGET_PASSWORD"
DEFAULT_SCHEMA = "simplicity"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(values: Dict[str, str]) -> Dict[str, str]:
    v = {k: str(values.get(k, "")).strip() for k in FIELDS}
    v["server"] = v["server"].lower()
    v["password_env_var"] = v["password_env_var"] or DEFAULT_ENV
    v["schema_name"] = v["schema_name"] or DEFAULT_SCHEMA
    if not SERVER.match(v["server"]):
        raise ValueError("Server must look like name.database.windows.net.")
    if not NAME.match(v["database_name"]):
        raise ValueError("Database name is not valid.")
    if not NAME.match(v["username"]):
        raise ValueError("User name is not valid.")
    if not ENV_VAR.match(v["password_env_var"]):
        raise ValueError("The password is read from a server environment variable whose name starts with SQL_ (for example SQL_TARGET_PASSWORD). "
                         "Never type the password itself here.")
    if not SCHEMA.match(v["schema_name"]):
        raise ValueError("Schema must be letters, digits or underscore.")
    return v


def get_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM sql_target_config WHERE config_id = 1").fetchone()
    if not row:
        return None
    cfg = dict(row)
    cfg["password_present"] = bool(os.environ.get(cfg["password_env_var"]))
    return cfg


def save(conn: sqlite3.Connection, actor: str, values: Dict[str, str]) -> Dict[str, Any]:
    ensure_table(conn)
    v = validate(values)
    conn.execute("DELETE FROM sql_target_config WHERE config_id = 1")
    conn.execute("INSERT INTO sql_target_config (config_id, " + ", ".join(FIELDS) + ", status, configured_by, configured_utc) VALUES (1, "
                 + ", ".join("?" for _ in FIELDS) + ", 'CONFIGURED', ?, ?)", (*[v[k] for k in FIELDS], actor, _now()))
    return get_config(conn)


def _default_connect(cfg: Dict[str, Any], password: str):
    import pymssql  # imported here so the rest of the platform runs without the driver
    return pymssql.connect(server=cfg["server"], user=cfg["username"], password=password, database=cfg["database_name"], login_timeout=15, timeout=20, tds_version="7.4")


def test_connection(conn: sqlite3.Connection, connect: Callable[..., Any] = _default_connect) -> Dict[str, Any]:
    """Sign in, report the database and whether the target schema and its tables exist. Read-only."""
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Save the SQL settings first.")
    found = ""
    password = os.environ.get(cfg["password_env_var"], "")
    if not password:
        status, detail = "FAILED", f"The server has no value for {cfg['password_env_var']}. Set it in the server's .env and restart."
    else:
        try:
            db = connect(cfg, password)
            try:
                cur = db.cursor()
                cur.execute("SELECT DB_NAME()")
                name = cur.fetchone()[0]
                cur.execute("SELECT 1 FROM sys.schemas WHERE name = %s", (cfg["schema_name"],))
                has_schema = cur.fetchone() is not None
                tables = []
                if has_schema:
                    cur.execute("SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = %s ORDER BY t.name", (cfg["schema_name"],))
                    tables = [r[0] for r in cur.fetchall()]
                found = ", ".join(tables)
            finally:
                db.close()
            status = "CONNECTED"
            if not has_schema:
                detail = f"Signed in to {name}. Schema {cfg['schema_name']} does not exist yet: run the create script (sql/01_target_tables.sql)."
            elif not tables:
                detail = f"Signed in to {name}. Schema {cfg['schema_name']} exists but has no tables yet: run the create script."
            else:
                detail = f"Signed in to {name}. Schema {cfg['schema_name']} exists with {len(tables)} table(s): {found}."
        except ImportError:
            status, detail = "FAILED", "The SQL driver (pymssql) is not installed in the server image. Rebuild the image."
        except Exception as exc:  # driver errors carry the server's reason (login failed, firewall); never the password
            status, detail = "FAILED", f"Could not sign in: {type(exc).__name__}: {str(exc)[:300].replace(password, '***')}"
    conn.execute("UPDATE sql_target_config SET status = ?, checked_utc = ?, check_detail = ?, found_tables = ? WHERE config_id = 1", (status, _now(), detail, found))
    return get_config(conn)
