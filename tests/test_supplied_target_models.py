import asyncio

import httpx

import simulated_target_service.database as tdb
from control_plane.services import target_connector as tc


def _schema(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATED_TARGET_DB", str(tmp_path / "t.db"))
    assert tdb.bootstrap() is True
    return tdb.read_schema()


def test_shipped_seed_is_built_from_the_supplied_orion_and_sparc_workbook(tmp_path, monkeypatch):
    schema = _schema(tmp_path, monkeypatch)
    cols = {t["name"]: t["columns"] for t in schema["tables"]}
    assert set(cols) == {"ORION_MCL_HOUSEHOLD", "ORION_MCL_CLIENT", "ORION_MCL_ACCOUNT", "SPARC_HOUSEHOLD", "SPARC_CLIENT", "SPARC_ACCOUNT"}
    assert sum(len(c) for n, c in cols.items() if n.startswith("ORION_MCL")) == 27   # ImportRecords headers
    assert sum(len(c) for n, c in cols.items() if n.startswith("SPARC")) == 34       # SPARC target headers
    req = {n for t, c in cols.items() for n in [x["name"] for x in c if x["required"]] if t.startswith("ORION")}
    assert {"HOUSEHOLD_LAST_NAME", "REPRESENTATIVE_NAME", "REGISTRATION_TYPE", "CUSTODIAN", "ACCOUNT_NUMBER"} <= req
    assert "HH_GROUPING_CODE" not in req and "HOUSEHOLD_SSN" not in req            # conditional or optional in the tooltips
    assert set(schema["code_lists"]) == {"ORION_REF_REGISTRATION_TYPES", "ORION_REF_CUSTODIANS", "ORION_REF_FUND_NAMES", "ORION_REF_COUNTRIES"}
    assert "TD Ameritrade" in schema["code_lists"]["ORION_REF_CUSTODIANS"] and schema["code_lists"]["ORION_REF_COUNTRIES"] == []
    assert all(c["description"] for t in schema["tables"] for c in t["columns"])


def test_models_page_shows_supplied_target_beside_canonical_model(tmp_path, monkeypatch):
    import control_plane.db as db
    import ui.app as app_module
    import ui.state as state_module
    from ui.state import WorkspaceStateStore

    schema = _schema(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "m.db")
    monkeypatch.setattr(state_module, "SOURCE_UPLOAD_ROOT", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "state_store", WorkspaceStateStore())
    monkeypatch.setitem(app_module.session_mgr.active_sessions, "sid", {"email": "a@example.invalid", "name": "Ann", "role": "Domain SME"})
    monkeypatch.setattr(tc, "_http_get", lambda url: schema)
    conn = db.get_db_connection()
    tc.configure(conn, "Ann", "Orion", "http://127.0.0.1:8100", [], True, "")
    conn.commit()
    conn.close()

    async def get():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies={"session_id": "sid"}) as client:
            return await client.get("/models", headers={"accept": "text/html"})
    page = asyncio.run(get())
    assert page.status_code == 200
    for text in ("ORION_MCL_HOUSEHOLD", "SPARC_CLIENT", "ORION_REF_CUSTODIANS", "Canonical model", "Household", "Account", "Client"):
        assert text in page.text


def test_a_database_from_an_older_seed_is_renamed_aside_and_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMULATED_TARGET_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(tdb, "SEED_PATH", tdb.SEED_PATH.with_name("seed_demo.yaml"))
    assert tdb.bootstrap() is True and tdb.bootstrap() is False          # same version: left alone
    assert {t["name"] for t in tdb.read_schema()["tables"]} >= {"SIMP_GROUP"}
    monkeypatch.setattr(tdb, "SEED_PATH", tdb.SEED_PATH.with_name("seed.yaml"))
    assert tdb.bootstrap() is True                                         # newer seed: rebuilt
    assert {t["name"] for t in tdb.read_schema()["tables"]} >= {"ORION_MCL_ACCOUNT"}
    assert (tmp_path / "t.0.2.0.bak.db").is_file()                         # the old file is kept
