"""Each live schema (orion, sparc, ...) has its own target model; the connection is shared and a migration chooses its schema."""
import json
import sqlite3

import pytest

from control_plane.services import live_target, target_connector as tc


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    live_target.set_current_schema("")
    yield c
    live_target.set_current_schema("")
    c.close()


def _live(c, schema="orion"):
    live_target.ensure_table(c)
    c.execute("INSERT INTO live_target_config (config_id, server, database_name, username, password_env_var, schema_name, status, configured_by, configured_utc) "
              "VALUES (1, 'srv.database.windows.net', 'Simplicity', 'u', 'PW', ?, 'CONFIGURED', 't', 'now')", (schema,))


def _model(c, key, status, hint=None):
    tc.ensure_table(c)
    model = {"entities": {hint: {"target_table": "t", "fields": []}} if hint else {}, "schema_hash": "h-" + key}
    c.execute("INSERT INTO target_system_models (config_key, target_name, connector_type, endpoint, scope_tables, include_code_lists, owner, status, "
              "configured_by, configured_utc, inferred_model, schema_hash) VALUES (?, 'Simplicity', 'azure-sql', ?, '[]', 0, '', ?, 't', 'now', ?, ?)",
              (key, f"azure-sql://srv.database.windows.net/Simplicity/{key}", status, json.dumps(model), model["schema_hash"]))


def test_live_connection_takes_the_schema_of_the_request(conn):
    _live(conn, "orion")
    assert live_target.get_config(conn)["schema_name"] == "orion"
    live_target.set_current_schema("sparc")
    cfg = live_target.get_config(conn)
    assert cfg["schema_name"] == "sparc" and cfg["saved_schema_name"] == "orion" and cfg["server"] == "srv.database.windows.net"
    assert live_target.effective_schema(conn) == "sparc"


def test_each_schema_keeps_its_own_target_model(conn):
    _live(conn, "orion")
    _model(conn, "orion", "CONFIRMED", "Account")
    _model(conn, "sparc", "INFERRED", "Client")
    assert tc.get_config(conn)["model"]["entities"].keys() == {"Account"} and tc.is_confirmed(tc.get_config(conn))
    live_target.set_current_schema("sparc")
    cfg = tc.get_config(conn)
    assert cfg["model"]["entities"].keys() == {"Client"} and cfg["status"] == "INFERRED" and not tc.is_confirmed(cfg)
    assert tc.confirmed_schemas(conn) == ["orion"]


def test_changing_one_schema_leaves_the_other_alone(conn):
    _live(conn, "orion")
    _model(conn, "orion", "CONFIRMED", "Account")
    _model(conn, "sparc", "CONFIRMED", "Client")
    live_target.set_current_schema("sparc")
    tc.reopen(conn, "admin", "owner changed the layout")
    assert tc.get_config(conn)["status"] == "CONFIGURED" and tc.get_config(conn)["model"] is None
    live_target.set_current_schema("orion")
    assert tc.is_confirmed(tc.get_config(conn)) and tc.get_config(conn)["model"] is not None
    assert tc.schema_states(conn) == {"orion": "CONFIRMED", "sparc": "CONFIGURED"}


def test_a_model_saved_before_targets_were_per_schema_belongs_to_the_schema_its_endpoint_names(conn):
    _live(conn, "orion")
    conn.execute(tc._LEGACY_DDL)
    conn.execute("INSERT INTO target_system_config (config_id, target_name, connector_type, endpoint, scope_tables, include_code_lists, owner, status, "
                 "configured_by, configured_utc, inferred_model) VALUES (1, 'Simplicity', 'azure-sql', 'azure-sql://srv.database.windows.net/Simplicity/orion', '[]', 0, '', "
                 "'CONFIRMED', 't', 'now', '{\"entities\": {}}')")
    assert tc.schema_states(conn) == {"orion": "CONFIRMED"}
    live_target.set_current_schema("sparc")
    assert tc.get_config(conn) is None  # another schema does not inherit it
    live_target.set_current_schema("orion")
    assert tc.is_confirmed(tc.get_config(conn))


def _two_schemas(app_module):
    """The seeded confirmed target becomes schema orion (all its entities); schema sparc gets a model with the Client entity only."""
    conn = app_module.get_db_connection()
    try:
        row = dict(conn.execute("SELECT * FROM target_system_models WHERE config_key = ''").fetchone())
        model = json.loads(row["inferred_model"])
        sparc = {**model, "entities": {k: v for k, v in model["entities"].items() if k == "Client"}}
        conn.execute("DELETE FROM target_system_models")
        for key, m in (("orion", model), ("sparc", sparc)):
            cols = [c for c in row if c != "config_key"]
            vals = [json.dumps(m) if c == "inferred_model" else row[c] for c in cols]
            conn.execute("INSERT INTO target_system_models (config_key, " + ", ".join(cols) + ") VALUES (?, " + ", ".join("?" for _ in cols) + ")", (key, *vals))
        conn.commit()
    finally:
        conn.close()


def _post(app_module, session_id, path, fields, method="post"):
    import asyncio
    from urllib.parse import urlencode

    import httpx

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://testserver", cookies={"session_id": session_id}) as client:
            if method == "get":
                return await client.get(path + "?" + urlencode(fields), follow_redirects=False)
            return await client.post(path, content=urlencode(fields), headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    return asyncio.run(run())


def test_a_migration_chooses_its_schema_and_the_entities_follow_that_schemas_model(tmp_path, monkeypatch):
    from tests.test_firm_config import _setup_client

    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "schemas")
    _two_schemas(app_module)
    base = [("migration_name", "CoreCap to Simplicity SPARC"), ("source_firm", "Amplify"), ("target_system", "Simplicity"),
            ("canonical_version", "Simplicity-MCL-v1"), ("source_acquisition", "SAMPLE_FILES")]
    assert _post(app_module, sid, "/api/migration/setup", base + [("entities", "Client")]).status_code == 400  # no schema chosen
    assert _post(app_module, sid, "/api/migration/setup", base + [("target_schema", "sparc"), ("entities", "Account")]).status_code == 400  # sparc has no Account table
    ok = _post(app_module, sid, "/api/migration/setup", base + [("target_schema", "sparc"), ("entities", "Client")])
    assert ok.status_code == 303
    assert state.questionnaire_data["migration_setup"]["target_schema"] == "sparc"
    assert state.questionnaire_data["migration_setup"]["target_entities"] == ["Client"]
    page = _post(app_module, sid, "/migration-setup", {}, method="get")
    assert 'id="target-schema"' in page.text and 'name="target_schema" value="sparc"' in page.text
    assert live_target.current_schema() == "sparc"  # later requests of this migration work on its schema


def test_the_other_schema_still_offers_its_own_entities(tmp_path, monkeypatch):
    from tests.test_firm_config import _setup_client

    app_module, sid, state = _setup_client(tmp_path, monkeypatch, "schemas2")
    _two_schemas(app_module)
    orion = _post(app_module, sid, "/migration-setup", {"target_schema": "orion"}, method="get").text
    sparc = _post(app_module, sid, "/migration-setup", {"target_schema": "sparc"}, method="get").text
    assert 'value="Account"' in orion and 'value="Account"' not in sparc.split('id="migration-setup-form"')[1].split("Migration entity scope")[1][:3000]
