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
USERNAME_ENV = "SQL_TARGET_USERNAME"
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
    if v["username"] and not NAME.match(v["username"]):
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
    cfg["username_effective"] = cfg["username"] or os.environ.get(USERNAME_ENV, "")  # blank on the page means: use SQL_TARGET_USERNAME from the server's .env
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


def explain(exc: Exception, password: str = "") -> str:
    """A readable reason for a sign-in failure. The Azure SQL firewall refusal (40615) names the address to allow; never the password."""
    text = str(exc).replace(password, "***") if password else str(exc)
    ip = re.search(r"IP address '([0-9a-fA-F.:]+)'", text)
    if "40615" in text or ip:
        return ("the Azure SQL firewall does not allow this machine" + (f" (IP {ip.group(1)})" if ip else "")
                + ". Add it under SQL server > Networking > Firewall rules (Add your client IPv4 address), then try again.")
    if "18456" in text:
        return "the user name or password was rejected (error 18456)."
    return f"{type(exc).__name__}: {text[:300]}"


def run(cfg: Optional[Dict[str, Any]], statements, connect: Callable[..., Any] = _default_connect, commit: bool = False) -> list:
    """Run [(sql, params), ...] with the saved sign-in of `cfg` and return each statement's rows (an empty list when it returns none).
    Reads only, unless commit=True (used solely to record a promotion sign-off). Raises ValueError with a readable reason; never
    includes the password."""
    if not cfg:
        raise ValueError("The SQL connection is not set up.")
    password = os.environ.get(cfg["password_env_var"], "")
    user = cfg.get("username_effective") or cfg.get("username")
    if not (user and password):
        raise ValueError("The SQL user name or password is missing on the server.")
    try:
        db = connect({**cfg, "username": user}, password)
    except ImportError:
        raise ValueError("The SQL driver (pymssql) is not installed in the server image.")
    except Exception as exc:
        raise ValueError(f"Could not sign in to {cfg['database_name']}: " + explain(exc, password))
    out = []
    try:
        cur = db.cursor()
        for sql, params in statements:
            cur.execute(sql, params)
            try:
                out.append(cur.fetchall())
            except Exception:  # statements that return no rows
                out.append([])
        if commit:
            db.commit()
    except Exception as exc:
        raise ValueError(f"{cfg['database_name']}: {type(exc).__name__}: {str(exc)[:300].replace(password, '***')}")
    finally:
        db.close()
    return out


def test_connection(conn: sqlite3.Connection, connect: Callable[..., Any] = _default_connect) -> Dict[str, Any]:
    """Sign in, report the database and whether the target schema and its tables exist. Read-only."""
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Save the SQL settings first.")
    found = ""
    password = os.environ.get(cfg["password_env_var"], "")
    cfg["username"] = cfg["username_effective"]
    if not cfg["username"]:
        status, detail = "FAILED", f"No user name: enter it here or set {USERNAME_ENV} in the server's .env and restart."
    elif not password:
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


RUN_LOG_TABLE = "xodus_run_log"


def run_log_status(conn: sqlite3.Connection, connect: Callable[..., Any] = _default_connect, table: str = RUN_LOG_TABLE) -> Dict[str, Any]:
    """Does the run-log table the pipeline writes to exist in the target schema? Read-only catalog query, used before the ADF package
    is published so a missing table is found at deploy time, not after the first run.
    Returns {"checked": bool, "exists": bool | None, "detail": str}; checked is False when the database cannot be reached or is not set up."""
    cfg = get_config(conn)
    if not cfg:
        return {"checked": False, "exists": None, "detail": "The SQL target is not set up, so the run-log table could not be checked."}
    password = os.environ.get(cfg["password_env_var"], "")
    user = cfg["username_effective"]
    if not (user and password):
        return {"checked": False, "exists": None, "detail": "The SQL user name or password is missing on the server, so the run-log table could not be checked."}
    try:
        db = connect({**cfg, "username": user}, password)
        try:
            cur = db.cursor()
            cur.execute("SELECT 1 FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = %s AND t.name = %s", (cfg["schema_name"], table))
            exists = cur.fetchone() is not None
        finally:
            db.close()
    except Exception as exc:  # never include the password in the message
        return {"checked": False, "exists": None, "detail": f"The run-log table could not be checked: {type(exc).__name__}: {str(exc)[:200].replace(password, '***')}"}
    detail = (f"Run-log table {cfg['schema_name']}.{table} exists." if exists else
              f"Run-log table {cfg['schema_name']}.{table} does not exist. Run sql/02_run_log.sql from the ADF package against {cfg['database_name']} first.")
    return {"checked": True, "exists": exists, "detail": detail}


_GROUPS = {"date": "date", "datetime": "date", "datetime2": "date", "smalldatetime": "date", "int": "integer", "bigint": "integer", "smallint": "integer",
           "tinyint": "integer", "decimal": "decimal", "numeric": "decimal", "float": "decimal", "real": "decimal", "money": "decimal", "bit": "boolean"}


def read_schema(conn: sqlite3.Connection, connect: Callable[..., Any] = _default_connect, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Table and column list of the target schema, in the shape target inference reads. Catalog only: no row data, nothing changed.
    The database has no column descriptions or code lists, so those are empty; a column that does not allow NULL is reported as required."""
    cfg = cfg or get_config(conn)
    if not cfg:
        raise ValueError("Save the SQL settings first.")
    password = os.environ.get(cfg["password_env_var"], "")
    user = cfg["username_effective"]
    if not (user and password):
        raise ValueError("The SQL user name or password is missing on the server (SQL_TARGET_USERNAME / " + cfg["password_env_var"] + ").")
    cfg = {**cfg, "username": user}
    db = connect(cfg, password)
    try:
        cur = db.cursor()
        cur.execute("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION", (cfg["schema_name"],))
        rows = cur.fetchall()
    finally:
        db.close()
    tables: Dict[str, Any] = {}
    for table, column, dtype, nullable in rows:
        t = tables.setdefault(table, {"name": table, "description": "", "columns": []})
        t["columns"].append({"name": column, "type": _GROUPS.get(str(dtype).lower(), "string"), "required": str(nullable).upper() == "NO",
                             "generated": False, "description": "", "code_list": None})
    return {"system": f"Azure SQL {cfg['database_name']}.{cfg['schema_name']}", "version": "", "tables": list(tables.values()), "code_lists": {}}
