"""One-time target system setup: connection configuration, schema introspection and model confirmation.

The target is an external system reached only through a configured endpoint. This POC allows loopback
endpoints only (the simulated target service); no live Azure or production connection is made, and no
credentials are stored. Once the inferred model is confirmed the record is locked; a Platform Admin can reopen it
(with a recorded reason) to reconfigure, which withdraws the confirmed model until it is confirmed again.
"""
import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.services import sql_target, target_inference

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
CONNECTOR_TYPE = "simulated-http"
AZURE_SQL_TYPE = "azure-sql"
MAX_BYTES = 2_000_000

_LEGACY_DDL = """
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
# One row per live schema (config_key; empty for a target that is not a database schema, such as the simulated service).
_DDL = """
CREATE TABLE IF NOT EXISTS target_system_models (
    config_key TEXT PRIMARY KEY,
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


_COLUMNS = ("target_name", "connector_type", "endpoint", "scope_tables", "include_code_lists", "owner", "status", "connection_checked_utc",
            "connection_detail", "schema_hash", "inferred_model", "configured_by", "configured_utc", "inferred_utc", "confirmed_by", "confirmed_utc")


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _key(conn: sqlite3.Connection) -> str:
    """Which target model this request works on: the live schema in use, empty when there is no live database."""
    from control_plane.services import live_target
    return live_target.effective_schema(conn)


def _import_legacy(conn: sqlite3.Connection, key: str) -> None:
    """The single target model saved before targets were per schema becomes the model of the schema its endpoint names."""
    conn.execute(_LEGACY_DDL)
    row = conn.execute("SELECT * FROM target_system_config WHERE config_id = 1").fetchone()
    if not row:
        return
    legacy = dict(row)
    endpoint = str(legacy.get("endpoint") or "")
    schema = endpoint.rstrip("/").rsplit("/", 1)[-1] if endpoint.startswith("azure-sql://") else ""
    if schema != key or conn.execute("SELECT 1 FROM target_system_models WHERE config_key = ?", (schema,)).fetchone():
        return
    conn.execute("INSERT INTO target_system_models (config_key, " + ", ".join(_COLUMNS) + ") VALUES (?, " + ", ".join("?" for _ in _COLUMNS) + ")",
                 (schema, *[legacy.get(c) for c in _COLUMNS]))


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
    key = _key(conn)
    row = conn.execute("SELECT * FROM target_system_models WHERE config_key = ?", (key,)).fetchone()
    if not row:
        _import_legacy(conn, key)
        row = conn.execute("SELECT * FROM target_system_models WHERE config_key = ?", (key,)).fetchone()
    if not row:
        return None
    cfg = dict(row)
    cfg["schema"] = key
    cfg["scope_tables"] = json.loads(cfg["scope_tables"] or "[]")
    cfg["include_code_lists"] = bool(cfg["include_code_lists"])
    cfg["model"] = json.loads(cfg["inferred_model"]) if cfg.get("inferred_model") else None
    return cfg


def latest_confirmed_schema(conn: sqlite3.Connection) -> str:
    """The schema of the most recently confirmed target model, or empty when none is confirmed. A Platform Admin who has not picked a schema yet
    starts on it, so the platform pages show the target that is already set up instead of "not confirmed"."""
    ensure_table(conn)
    row = conn.execute("SELECT config_key FROM target_system_models WHERE status = 'CONFIRMED' AND config_key <> '' ORDER BY confirmed_utc DESC LIMIT 1").fetchone()
    return str(row["config_key"]) if row else ""


def is_confirmed(cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(cfg and cfg["status"] == "CONFIRMED")


def configure(conn: sqlite3.Connection, actor: str, target_name: str, endpoint: str, scope_tables: List[str],
              include_code_lists: bool, owner: str) -> Dict[str, Any]:
    """Save the setup configuration and test the connection. Locked once confirmed. Changing it clears any inference."""
    ensure_table(conn)
    existing = get_config(conn)
    if is_confirmed(existing):
        raise ValueError("Target system setup is confirmed and locked. A Platform Admin must reopen it before it can be changed.")
    name = str(target_name or "").strip()
    if not name:
        raise ValueError("Target system name is required.")
    endpoint = validate_endpoint(endpoint)
    scope = sorted({s.strip().upper() for s in scope_tables if s.strip()})
    now = _now()
    key = _key(conn)
    conn.execute("DELETE FROM target_system_models WHERE config_key = ?", (key,))
    conn.execute(
        "INSERT INTO target_system_models (config_key, target_name, connector_type, endpoint, scope_tables, include_code_lists, owner, "
        "status, configured_by, configured_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIGURED', ?, ?)",
        (key, name, CONNECTOR_TYPE, endpoint, json.dumps(scope), 1 if include_code_lists else 0, str(owner or "").strip(), actor, now))
    return test_connection(conn)


def configure_azure_sql(conn: sqlite3.Connection, actor: str, owner: str, scope_tables: List[str] = ()) -> Dict[str, Any]:
    """Use the target Azure SQL database (saved on the Azure connection page) as the target system: its table and column list is read
    with the same read-only connection. Locked once confirmed, like the simulated setup."""
    ensure_table(conn)
    if is_confirmed(get_config(conn)):
        raise ValueError("Target system setup is confirmed and locked. A Platform Admin must reopen it before it can be changed.")
    from control_plane.services import live_target
    sql = live_target.get_config(conn) or sql_target.get_config(conn)  # the live database (Simplicity) when it is set up
    if not sql:
        raise ValueError("Save the live database (or target database) settings on the Azure connection page first.")
    scope = sorted({s.strip().upper() for s in scope_tables if s.strip()})
    key = _key(conn)
    conn.execute("DELETE FROM target_system_models WHERE config_key = ?", (key,))
    conn.execute(
        "INSERT INTO target_system_models (config_key, target_name, connector_type, endpoint, scope_tables, include_code_lists, owner, "
        "status, configured_by, configured_utc) VALUES (?, ?, ?, ?, ?, 0, ?, 'CONFIGURED', ?, ?)",
        (key, "Simplicity", AZURE_SQL_TYPE, f"azure-sql://{sql['server']}/{sql['database_name']}/{sql['schema_name']}", json.dumps(scope), str(owner or "").strip(), actor, _now()))
    return test_connection(conn)


def _live_source(conn: sqlite3.Connection, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The live database (Simplicity) connection when the target endpoint was configured from it; None means the XODUS database."""
    from control_plane.services import live_target
    live = live_target.get_config(conn)
    if live and cfg.get("endpoint") == f"azure-sql://{live['server']}/{live['database_name']}/{live['schema_name']}":
        return live
    return None


def reopen(conn: sqlite3.Connection, actor: str, reason: str) -> Dict[str, Any]:
    """Platform Admin unlocks a confirmed setup so it can be reconfigured. Withdraws the confirmed model; existing mappings stay for audit."""
    cfg = get_config(conn)
    if not is_confirmed(cfg):
        raise ValueError("Only a confirmed target setup can be reopened.")
    if not str(reason or "").strip():
        raise ValueError("A reason is required to reopen the target setup.")
    conn.execute("UPDATE target_system_models SET status = 'CONFIGURED', inferred_model = NULL, schema_hash = NULL, inferred_utc = NULL, "
                 "confirmed_by = NULL, confirmed_utc = NULL, connection_detail = ? WHERE config_key = ?",
                 (f"Reopened by {actor}: {str(reason).strip()[:200]}. Save and test the connection, infer, then confirm again.", _key(conn)))
    return get_config(conn)


def test_connection(conn: sqlite3.Connection) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Target system is not configured yet.")
    if is_confirmed(cfg):
        return cfg
    if cfg["connector_type"] == AZURE_SQL_TYPE:
        live = _live_source(conn, cfg)
        if live:  # the live database (Simplicity) and the schema of this target: what is read when the model is inferred
            from control_plane.services import live_target
            try:
                tables = live_target.read_columns(live)
                ok = bool(tables)
                detail = (f"Signed in to {live['database_name']}. Schema {live['schema_name']} has {len(tables)} table(s): " + ", ".join(sorted(tables)[:8])
                          + ("" if len(tables) <= 8 else ", ...") + "." if tables
                          else f"Signed in to {live['database_name']}, but schema {live['schema_name']} has no tables yet. Run the create script first.")
            except ValueError as exc:
                ok, detail = False, str(exc)
        else:
            res = sql_target.test_connection(conn)
            ok = res["status"] == "CONNECTED" and bool(res.get("found_tables"))
            detail = res["check_detail"] if ok or res["status"] != "CONNECTED" else res["check_detail"] + " Run the create script first."
        conn.execute("UPDATE target_system_models SET status = ?, connection_checked_utc = ?, connection_detail = ?, "
                     "inferred_model = NULL, schema_hash = NULL, inferred_utc = NULL WHERE config_key = ?", ("CONNECTED" if ok else "CONFIGURED", _now(), detail, _key(conn)))
        return get_config(conn)
    try:
        health = _http_get(cfg["endpoint"] + "/health")
        ok, detail = health.get("status") == "ok", f"Connected to {health.get('system', 'target')}."
        if not ok:
            detail = "Target reported an unhealthy status."
    except Exception as exc:  # network and parse failures are shown to the operator, not raised
        ok, detail = False, f"Could not reach the target: {exc}"
    conn.execute("UPDATE target_system_models SET status = ?, connection_checked_utc = ?, connection_detail = ?, "
                 "inferred_model = NULL, schema_hash = NULL, inferred_utc = NULL WHERE config_key = ?",
                 ("CONNECTED" if ok else "CONFIGURED", _now(), detail, _key(conn)))
    return get_config(conn)


def infer_model(conn: sqlite3.Connection, canonical: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg:
        raise ValueError("Target system is not configured yet.")
    if is_confirmed(cfg):
        raise ValueError("The target model is already confirmed.")
    if cfg["status"] not in {"CONNECTED", "INFERRED"}:
        raise ValueError("Test the connection successfully before inferring the target system.")
    if cfg["connector_type"] == AZURE_SQL_TYPE:
        live = _live_source(conn, cfg)
        schema = sql_target.read_schema(conn, cfg=live) if live else sql_target.read_schema(conn)
    else:
        schema = _http_get(cfg["endpoint"] + "/schema")
    if not schema.get("tables"):
        raise ValueError("The target exposed no tables.")
    model = target_inference.infer(schema, canonical, cfg["scope_tables"], cfg["include_code_lists"])
    if not model["entities"]:
        raise ValueError("No target table could be matched to Account, Client or Household.")
    conn.execute("UPDATE target_system_models SET status = 'INFERRED', schema_hash = ?, inferred_model = ?, inferred_utc = ? WHERE config_key = ?",
                 (model["schema_hash"], json.dumps(model), _now(), _key(conn)))
    return get_config(conn)


def pending_reviews(model: Dict[str, Any]) -> int:
    """Inferred target fields no reviewer has decided on yet."""
    return sum(1 for b in model["entities"].values() for f in b["fields"] if not f.get("review"))


def _inferred_for_review(conn: sqlite3.Connection) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg or cfg["status"] != "INFERRED" or not cfg["model"]:
        raise ValueError("Infer the target model before reviewing it. A confirmed setup must be reopened first.")
    return cfg


def _save_model(conn: sqlite3.Connection, model: Dict[str, Any]) -> Dict[str, Any]:
    conn.execute("UPDATE target_system_models SET inferred_model = ? WHERE config_key = ?", (json.dumps(model), _key(conn)))
    return get_config(conn)


def _find_field(model: Dict[str, Any], table: str, column: str) -> Dict[str, Any]:
    for ename, body in model["entities"].items():
        for f in body["fields"]:
            if f["table"] == table and f["column"] == column:
                return ename, f
    raise ValueError(f"Unknown target field {table}.{column}.")


def review_field(conn: sqlite3.Connection, actor: str, table: str, column: str, decision: str, canonical_field: str,
                 canonical: Dict[str, Dict[str, str]], note: str = "") -> Dict[str, Any]:
    """Reviewer decision on one inferred target field: CORRECT (as inferred), CHANGE (another canonical field, or none
    when canonical_field is blank) or REJECT (the field is not part of the target model). The inference is kept alongside."""
    cfg = _inferred_for_review(conn)
    model = cfg["model"]
    entity, f = _find_field(model, table, column)
    decision = str(decision or "").upper()
    if decision not in {"CORRECT", "CHANGE", "REJECT"}:
        raise ValueError("Decision must be correct, change or reject.")
    first = f.get("review") or {"inferred_hint": f["canonical_hint"], "inferred_confidence": f["confidence"]}
    prior, prior_conf = first["inferred_hint"], first.get("inferred_confidence", f["confidence"])
    if decision == "CHANGE":
        name = str(canonical_field or "").strip().upper()
        if name and name not in canonical.get(entity, {}):
            raise ValueError(f"{name} is not a canonical {entity} field.")
        f["canonical_hint"], f["confidence"] = (name or None), (1.0 if name else 0.0)
        if not name and not f.get("suggested_canonical_field"):  # now a gap: give it a name a change request can use
            f["suggested_canonical_field"] = target_inference.suggest_canonical_name(entity, f["column"], list(canonical.get(entity, {})))
        status = "CORRECTED"
    else:
        f["canonical_hint"], f["confidence"] = prior, prior_conf  # a correct/reject decision stands on the original inference
        status = "CONFIRMED" if decision == "CORRECT" else "REJECTED"
    f["review"] = {"status": status, "by": actor, "utc": _now(), "inferred_hint": prior, "inferred_confidence": prior_conf}
    if str(note or "").strip():
        f["review"]["note"] = str(note).strip()
    return _save_model(conn, model)


def review_all(conn: sqlite3.Connection, actor: str) -> Dict[str, Any]:
    """Accept every still-undecided field exactly as inferred (fields already decided are left alone)."""
    cfg = _inferred_for_review(conn)
    model = cfg["model"]
    for body in model["entities"].values():
        for f in body["fields"]:
            if not f.get("review"):
                f["review"] = {"status": "CONFIRMED", "by": actor, "utc": _now(), "inferred_hint": f["canonical_hint"],
                               "inferred_confidence": f["confidence"]}
    return _save_model(conn, model)


def move_table(conn: sqlite3.Connection, actor: str, table: str, entity: Optional[str],
               canonical: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    """Reviewer correction of a table's entity (or exclusion). Its fields are re-matched and need review again."""
    cfg = _inferred_for_review(conn)
    model = cfg["model"]
    target_inference.move_table(model, table, entity or None, canonical)
    model.setdefault("table_decisions", {})[table] = {"entity": entity or "EXCLUDED", "by": actor, "utc": _now()}
    return _save_model(conn, model)


def confirm(conn: sqlite3.Connection, actor: str) -> Dict[str, Any]:
    cfg = get_config(conn)
    if not cfg or cfg["status"] != "INFERRED":
        raise ValueError("Infer the target model before confirming it.")
    left = pending_reviews(cfg["model"])
    if left:
        raise ValueError(f"Review every inferred target field before confirming: {left} still need a decision (correct, change or reject).")
    conn.execute("UPDATE target_system_models SET status = 'CONFIRMED', confirmed_by = ?, confirmed_utc = ? WHERE config_key = ?", (actor, _now(), _key(conn)))
    return get_config(conn)


def spec(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Mapping spec from the confirmed model; None until the target setup is confirmed."""
    cfg = get_config(conn)
    if not is_confirmed(cfg) or not cfg["model"]:
        return None
    return target_inference.to_spec(cfg["model"], "INFERRED_CONFIRMED")


def schema_states(conn: sqlite3.Connection) -> Dict[str, str]:
    """{schema: status of its target model} for every live schema set up so far (the schema saved with the connection included)."""
    from control_plane.services import live_target
    ensure_table(conn)
    live_target.ensure_table(conn)
    live = conn.execute("SELECT schema_name FROM live_target_config WHERE config_id = 1").fetchone()
    if live and str(live["schema_name"] or ""):
        _import_legacy(conn, str(live["schema_name"]))  # a model saved before targets were per schema
    return {str(r["config_key"]): str(r["status"]) for r in conn.execute("SELECT config_key, status FROM target_system_models").fetchall() if r["config_key"]}


def confirmed_schemas(conn: sqlite3.Connection) -> List[str]:
    """Schemas a migration can target: those whose target model is confirmed."""
    return sorted(k for k, v in schema_states(conn).items() if v == "CONFIRMED")
