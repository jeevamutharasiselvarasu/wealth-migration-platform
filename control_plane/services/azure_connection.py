"""Azure Data Factory connection settings, entered in Setup and tested with a service principal.

Only non-secret identifiers are stored (tenant, client and subscription IDs, resource names). The service principal's client secret is
never accepted from a form or stored: the setup names an environment variable (must start with AZURE_) that the server reads when
it needs a token. Tokens and secrets are never logged, returned or persisted. Test connection uses the client-credentials flow
against Microsoft Entra and reads the factory resource; it changes nothing in Azure."""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import httpx

_DDL = """
CREATE TABLE IF NOT EXISTS azure_connection_config (
    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
    tenant_id TEXT NOT NULL, client_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
    resource_group TEXT NOT NULL, factory_name TEXT NOT NULL, storage_account TEXT NOT NULL,
    staging_container TEXT NOT NULL, sql_server TEXT NOT NULL, sql_database TEXT NOT NULL, sql_schema TEXT NOT NULL,
    key_vault_url TEXT NOT NULL, key_vault_sql_secret TEXT NOT NULL, secret_env_var TEXT NOT NULL,
    status TEXT NOT NULL, checked_utc TEXT, check_detail TEXT, configured_by TEXT NOT NULL, configured_utc TEXT NOT NULL
)
"""
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
SQL_HOST = re.compile(r"^[a-z0-9][a-z0-9.-]*\.database\.windows\.net$")
VAULT_URL = re.compile(r"^https://[a-z0-9-]{3,24}\.vault\.azure\.net/?$")
ENV_VAR = re.compile(r"^AZURE_[A-Z0-9_]{1,50}$")
FIELDS = ("tenant_id", "client_id", "subscription_id", "resource_group", "factory_name", "storage_account", "staging_container",
          "sql_server", "sql_database", "sql_schema", "key_vault_url", "key_vault_sql_secret", "secret_env_var")
DEFAULT_SECRET_ENV = "AZURE_CLIENT_SECRET"
API = "2018-06-01"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(values: Dict[str, str]) -> Dict[str, str]:
    v = {k: str(values.get(k, "")).strip() for k in FIELDS}
    v["secret_env_var"] = v["secret_env_var"] or DEFAULT_SECRET_ENV
    for k in ("tenant_id", "client_id", "subscription_id"):
        if not GUID.match(v[k]):
            raise ValueError(f"{k.replace('_', ' ').capitalize()} must be a GUID.")
    for k in ("resource_group", "factory_name", "storage_account", "staging_container", "sql_database", "sql_schema", "key_vault_sql_secret"):
        if not NAME.match(v[k]):
            raise ValueError(f"{k.replace('_', ' ').capitalize()} must be letters, digits, '-' or '_' (up to 63 characters).")
    if not SQL_HOST.match(v["sql_server"].lower()):
        raise ValueError("SQL server must be an Azure SQL host such as myserver.database.windows.net.")
    v["sql_server"] = v["sql_server"].lower()
    if not VAULT_URL.match(v["key_vault_url"].lower()):
        raise ValueError("Key Vault URL must look like https://<vault>.vault.azure.net/.")
    v["key_vault_url"] = v["key_vault_url"].lower().rstrip("/") + "/"
    if not ENV_VAR.match(v["secret_env_var"]):
        raise ValueError("The secret is read from a server environment variable whose name starts with AZURE_ (for example AZURE_CLIENT_SECRET). "
                         "Never type the secret itself here.")
    return v


def get_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM azure_connection_config WHERE config_id = 1").fetchone()
    if not row:
        return None
    cfg = dict(row)
    cfg["secret_present"] = bool(os.environ.get(cfg["secret_env_var"]))
    return cfg


def save(conn: sqlite3.Connection, actor: str, values: Dict[str, str]) -> Dict[str, Any]:
    """Save settings (status CONFIGURED). Any change clears an earlier successful test."""
    ensure_table(conn)
    v = validate(values)
    conn.execute("DELETE FROM azure_connection_config WHERE config_id = 1")
    conn.execute("INSERT INTO azure_connection_config (config_id, " + ", ".join(FIELDS) + ", status, configured_by, configured_utc) VALUES (1, "
                 + ", ".join("?" for _ in FIELDS) + ", 'CONFIGURED', ?, ?)", (*[v[k] for k in FIELDS], actor, _now()))
    return get_config(conn)


def _default_http(method: str, url: str, **kw) -> httpx.Response:
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        return client.request(method, url, **kw)


def acquire_token(cfg: Dict[str, Any], scope: str, http: Callable[..., httpx.Response] = _default_http) -> str:
    secret = os.environ.get(cfg["secret_env_var"], "")
    if not secret:
        raise ValueError(f"The server has no value for {cfg['secret_env_var']}. Set it in the server environment and restart.")
    resp = http("POST", f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": cfg["client_id"], "client_secret": secret, "scope": scope})
    if resp.status_code != 200:
        raise ValueError("Microsoft Entra rejected the service principal (check tenant ID, client ID and secret).")
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("Microsoft Entra returned no access token.")
    return token


def test_connection(conn: sqlite3.Connection, http: Callable[..., httpx.Response] = _default_http) -> Dict[str, Any]:
    """Sign in as the service principal and read the factory. Records CONNECTED or FAILED with a safe message."""
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Save the Azure settings first.")
    try:
        token = acquire_token(cfg, "https://management.azure.com/.default", http)
        url = (f"https://management.azure.com/subscriptions/{cfg['subscription_id']}/resourceGroups/{cfg['resource_group']}"
               f"/providers/Microsoft.DataFactory/factories/{cfg['factory_name']}?api-version={API}")
        resp = http("GET", url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        if resp.status_code == 200:
            status, detail = "CONNECTED", f"Signed in and found data factory {cfg['factory_name']}."
        elif resp.status_code in (401, 403):
            status, detail = "FAILED", "The service principal signed in but has no permission to read this data factory (grant it Data Factory Contributor)."
        elif resp.status_code == 404:
            status, detail = "FAILED", "Signed in, but the subscription, resource group or factory name was not found."
        else:
            status, detail = "FAILED", f"Azure returned HTTP {resp.status_code}."
    except ValueError as exc:
        status, detail = "FAILED", str(exc)
    except httpx.HTTPError:
        status, detail = "FAILED", "Could not reach Microsoft Entra or Azure."
    conn.execute("UPDATE azure_connection_config SET status = ?, checked_utc = ?, check_detail = ? WHERE config_id = 1", (status, _now(), detail))
    return get_config(conn)
