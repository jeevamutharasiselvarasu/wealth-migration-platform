import sqlite3

import pytest

from control_plane.services import live_target, target_connector as tc
from tests.test_data_plane import _Conn


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    tc.ensure_table(c)
    return c


def _model(conn, key, status, confirmed):
    conn.execute("INSERT INTO target_system_models (config_key, target_name, connector_type, endpoint, status, configured_by, configured_utc, confirmed_utc) "
                 "VALUES (?, 'Simplicity', 'AZURE_SQL', 'e', ?, 'Ada', '2026-11-01T00:00:00', ?)", (key, status, confirmed))


def test_the_latest_confirmed_schema_is_the_default_and_only_a_confirmed_one(conn):
    assert tc.latest_confirmed_schema(conn) == ""
    _model(conn, "orion", "CONFIRMED", "2026-11-10T00:00:00")
    _model(conn, "sparc", "CONFIRMED", "2026-11-12T00:00:00")
    _model(conn, "draft", "INFERRED", None)
    _model(conn, "", "CONFIRMED", "2026-11-20T00:00:00")  # a target that is not a database schema is not a default
    assert tc.latest_confirmed_schema(conn) == "sparc"


def test_a_new_admin_sign_in_starts_on_the_confirmed_target_but_a_pick_is_kept(conn, monkeypatch):
    import ui.app as app_module
    _model(conn, "orion", "CONFIRMED", "2026-11-10T00:00:00")
    monkeypatch.setattr(app_module, "get_db_connection", lambda: _Conn(conn))
    live_target.set_current_schema("")
    admin = {"role": "Platform Admin", "name": "Ada"}
    app_module._use_target_schema(admin)
    assert admin["target_schema"] == "orion" and live_target.current_schema() == "orion"  # the platform pages show the target that is set up
    picked = {"role": "Platform Admin", "name": "Ada", "target_schema": "sparc"}
    app_module._use_target_schema(picked)
    assert picked["target_schema"] == "sparc" and live_target.current_schema() == "sparc"
    cleared = {"role": "Platform Admin", "name": "Ada", "target_schema": ""}  # choosing "the schema saved with the connection" stays chosen
    app_module._use_target_schema(cleared)
    assert cleared["target_schema"] == "" and live_target.current_schema() == ""
    analyst = {"role": "Migration Analyst", "name": "Ann"}
    app_module._use_target_schema(analyst)
    assert "target_schema" not in analyst  # migration roles follow their workspace, never the admin default
    live_target.set_current_schema("")


def test_a_failing_lookup_never_blocks_the_request(monkeypatch):
    import ui.app as app_module

    def down():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(app_module, "get_db_connection", down)
    admin = {"role": "Platform Admin", "name": "Ada"}
    app_module._use_target_schema(admin)
    assert admin["target_schema"] == ""
    live_target.set_current_schema("")


def test_the_platform_setup_hub_names_the_saved_data_factory(monkeypatch, tmp_path):
    import asyncio

    import httpx

    import control_plane.db as db
    import ui.app as app_module
    from control_plane.services import azure_connection
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "hub.db")
    monkeypatch.setattr(azure_connection, "get_config", lambda conn: {"status": "CONFIGURED", "factory_name": "adf-test-factory"})
    app_module.session_mgr.active_sessions["hub-admin"] = {"email": "a@example.invalid", "name": "Ada", "role": "Platform Admin", "target_schema": ""}

    async def get():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "hub-admin"}) as client:
            return await client.get("/setup")
    page = asyncio.run(get())
    assert page.status_code == 200 and "adf-test-factory" in page.text and "no factory saved" not in page.text
    live_target.set_current_schema("")
