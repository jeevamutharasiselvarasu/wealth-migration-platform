"""One-time target system setup: connection configuration, schema introspection and model confirmation.

The target is an external system reached only through a configured endpoint. This POC allows loopback
endpoints only (the simulated target service); no live Azure or production connection is made, and no
credentials are stored. Setup is one-time: once the inferred model is confirmed the record is locked.
"""
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import target_inference

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
CONNECTOR_TYPE = "simulated-http"
MAX_BYTES = 2_000_000

_DDL = """
CREATE TABLE IF NOT EXISTS target_system_config (
    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
    target_name TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    scope_tables TEXT NOT NULL DEFAULT '[]',
    include_code_lists INTEGER NOT NULL DEFAULT 1,
    owner TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    connection_checked_utc TEXT,
    connection_detail TEXT,
    schema_hash TEXT,
    inferred_model TEXT,
    configured_by TEXT NOT NULL,
    configured_utc TEXT NOT NULL,
    inferred_utc TEXT,
    confirmed_by TEXT,
    confirmed_utc TEXT
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_get(url: str) -> Dict[str, Any]:
    """HTTP GET returning parsed JSON. Redirects are not followed. Replaced in tests."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D401
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=5) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Target schema response is too large.")
    return json.loads(data)


def validate_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(str(endpoint or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Endpoint must be an http(s) URL.")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("This POC only connects to a local simulated target (127.0.0.1 or localhost); live targets are out of scope.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Endpoint must not contain credentials, a query or a fragment.")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def get_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM target_system_config WHERE config_id = 1").fetchone()
    if not row:
        return None
    cfg = dict(row)
    cfg["scope_tables"] = json.loads(cfg["scope_tables"] or "[]")
    cfg["include_code_lists"] = bool(cfg["include_code_lists"])
    cfg["model"] = json.loads(cfg["inferred_model"]) if cfg.get("inferred_model") else None
    return cfg


def is_confirmed(cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(cfg and cfg["status"] == "CONFIRMED")


def configure(conn: sqlite3.Connection, actor: str, target_name: str, endpoint: str, scope_tables: List[str],
              include_code_lists: bool, owner: str) -> Dict[str, Any]:
    """Save the setup configuration and test the connection. Locked once confirmed. Changing it clears any inference."""
    ensure_table(conn)
    existing = get_config(conn)
    if is_confirmed(existing):
        raise ValueError("Target system setup is a one-time activity and is already confirmed.")
    name = str(target_name or "").strip()
    if not name:
        raise ValueError("Target system name is required.")
    endpoint = validate_endpoint(endpoint)
    scope = sorted({s.strip().upper() for s in scope_tables if s.strip()})
    now = _now()
    conn.execute("DELETE FROM target_system_config WHERE config_id = 1")
    conn.execute(
        "INSERT INTO target_system_config (config_id, target_name, connector_type, endpoint, scope_tables, include_code_lists, owner, "
        "status, configured_by, configured_utc) VALUES (1, ?, ?, ?, ?, ?, ?, 'CONFIGURED', ?, ?)",
        (name, CONNECTOR_TYPE, endpoint, json.dumps(scope), 1 if include_code_lists else 0, str(owner or "").strip(), actor, now))
    return test_connection(conn)


def test_connection(conn: sqlite3.Connection) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Target system is not configured yet.")
    if is_confirmed(cfg):
        return cfg
    try:
        health = _http_get(cfg["endpoint"] + "/health")
        ok, detail = health.get("status") == "ok", f"Connected to {health.get('system', 'target')}."
        if not ok:
            detail = "Target reported an unhealthy status."
    except Exception as exc:  # network and parse failures are shown to the operator, not raised
        ok, detail = False, f"Could not reach the target: {exc}"
    conn.execute("UPDATE target_system_config SET status = ?, connection_checked_utc = ?, connection_detail = ?, "
                 "inferred_model = NULL, schema_hash = NULL, inferred_utc = NULL WHERE config_id = 1",
                 ("CONNECTED" if ok else "CONFIGURED", _now(), detail))
    return get_config(conn)


def infer_model(conn: sqlite3.Connection, canonical: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Target system is not configured yet.")
    if is_confirmed(cfg):
        raise ValueError("The target model is already confirmed.")
    if cfg["status"] not in {"CONNECTED", "INFERRED"}:
        raise ValueError("Test the connection successfully before inferring the target system.")
    schema = _http_get(cfg["endpoint"] + "/schema")
    if not schema.get("tables"):
        raise ValueError("The target exposed no tables.")
    model = target_inference.infer(schema, canonical, cfg["scope_tables"], cfg["include_code_lists"])
    if not model["entities"]:
        raise ValueError("No target table could be matched to Account, Client or Household.")
    conn.execute("UPDATE target_system_config SET status = 'INFERRED', schema_hash = ?, inferred_model = ?, inferred_utc = ? WHERE config_id = 1",
                 (model["schema_hash"], json.dumps(model), _now()))
    return get_config(conn)


def confirm(conn: sqlite3.Connection, actor: str) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg or cfg["status"] != "INFERRED":
        raise ValueError("Infer the target model before confirming it.")
    conn.execute("UPDATE target_system_config SET status = 'CONFIRMED', confirmed_by = ?, confirmed_utc = ? WHERE config_id = 1", (actor, _now()))
    return get_config(conn)


def spec(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Mapping spec from the confirmed model; None until the target setup is confirmed."""
    cfg = get_config(conn)
    if not is_confirmed(cfg) or not cfg["model"]:
        return None
    return target_inference.to_spec(cfg["model"], "INFERRED_CONFIRMED")
