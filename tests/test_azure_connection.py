import sqlite3

import httpx
import pytest

from control_plane.services import azure_connection as az

G = "11111111-2222-3333-4444-555555555555"
GOOD = {"tenant_id": G, "client_id": G, "subscription_id": G, "resource_group": "rg-xodus", "factory_name": "adf-xodus",
        "storage_account": "stxodus", "staging_container": "canonical-staging", "sql_server": "Sql-Xodus.database.windows.net",
        "sql_database": "simplicity", "sql_schema": "dbo", "key_vault_url": "https://kv-xodus.vault.azure.net", "key_vault_sql_secret": "sql-conn",
        "secret_env_var": ""}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


def test_validation_rejects_bad_ids_hosts_and_typed_secrets():
    ok = az.validate(GOOD)
    assert ok["sql_server"] == "sql-xodus.database.windows.net" and ok["key_vault_url"].endswith(".vault.azure.net/") and ok["secret_env_var"] == "AZURE_CLIENT_SECRET"
    for key, bad in [("tenant_id", "not-a-guid"), ("sql_server", "evil.example.com"), ("key_vault_url", "http://kv.vault.azure.net"),
                     ("factory_name", "a b"), ("secret_env_var", "s3cr3t-value-typed-by-mistake"), ("secret_env_var", "POSTGRES_PASSWORD")]:
        with pytest.raises(ValueError):
            az.validate({**GOOD, key: bad})


def test_save_stores_no_secret_and_reports_only_whether_it_is_present(conn, monkeypatch):
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)
    cfg = az.save(conn, "Ann", GOOD)
    assert cfg["status"] == "CONFIGURED" and cfg["secret_present"] is False
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "super-secret")
    cfg = az.get_config(conn)
    assert cfg["secret_present"] is True and "super-secret" not in str(cfg)


def _http(token_status=200, factory_status=200, seen=None):
    def call(method, url, **kw):
        if seen is not None:
            seen.append((method, url))
        if "login.microsoftonline.com" in url:
            return httpx.Response(token_status, json={"access_token": "tok"})
        assert kw["headers"]["Authorization"] == "Bearer tok"
        return httpx.Response(factory_status, json={})
    return call


def test_connection_test_outcomes_never_expose_secret_or_token(conn, monkeypatch):
    az.save(conn, "Ann", GOOD)
    monkeypatch.delenv("AZURE_CLIENT_SECRET", raising=False)
    assert "no value for AZURE_CLIENT_SECRET" in az.test_connection(conn, _http())["check_detail"]
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "super-secret")
    seen = []
    ok = az.test_connection(conn, _http(seen=seen))
    assert ok["status"] == "CONNECTED" and seen[1][0] == "GET" and "/factories/adf-xodus?" in seen[1][1]
    assert "super-secret" not in str(ok) and "tok" not in ok["check_detail"].split()
    assert az.test_connection(conn, _http(token_status=401))["status"] == "FAILED"
    assert "permission" in az.test_connection(conn, _http(factory_status=403))["check_detail"]
    assert "not found" in az.test_connection(conn, _http(factory_status=404))["check_detail"]

    def down(method, url, **kw):
        raise httpx.ConnectError("x")
    assert "Could not reach" in az.test_connection(conn, down)["check_detail"]


def test_setup_page_and_routes_are_admin_only_and_never_echo_the_secret(tmp_path, monkeypatch):
    import asyncio
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "az.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "super-secret")
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "a@example.invalid", "name": "Ann", "role": "Platform Admin"})
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "an", {"email": "b@example.invalid", "name": "Bo", "role": "Migration Analyst"})

    async def call(sid, method, path, **kw):
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": sid}) as client:
            return await client.request(method, path, follow_redirects=False, headers={"accept": "text/html"}, **kw)

    denied = asyncio.run(call("an", "POST", "/api/azure/connection", data=GOOD))
    assert "may+not+perform" in denied.headers["location"]
    bad = asyncio.run(call("sid", "POST", "/api/azure/connection", data={**GOOD, "tenant_id": "x"}))
    assert "error=" in bad.headers["location"]
    ok = asyncio.run(call("sid", "POST", "/api/azure/connection", data=GOOD))
    assert "notice=" in ok.headers["location"]
    page = asyncio.run(call("sid", "GET", "/azure-setup"))
    assert page.status_code == 200 and "super-secret" not in page.text and "Present" in page.text and 'name="tenant_id"' in page.text
    viewer = asyncio.run(call("an", "GET", "/azure-setup"))
    assert viewer.status_code == 200 and "Read-only for Migration Analyst" in viewer.text


def test_source_container_is_optional_but_validated_and_old_tables_gain_the_column():
    import sqlite3
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE azure_connection_config (config_id INTEGER PRIMARY KEY, tenant_id TEXT)")
    az.ensure_table(c)  # table already exists without the new column
    assert "source_container" in {r[1] for r in c.execute("PRAGMA table_info(azure_connection_config)")}
    base = {k: "x" for k in az.FIELDS}
    base.update(GOOD) if isinstance(GOOD, dict) else None
    assert az.validate({**base, "source_container": ""})["source_container"] == ""
    assert az.validate({**base, "source_container": "corecap-src"})["source_container"] == "corecap-src"
    import pytest
    with pytest.raises(ValueError):
        az.validate({**base, "source_container": "bad container!"})
