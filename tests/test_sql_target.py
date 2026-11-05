import sqlite3

import pytest

from control_plane.services import sql_target as st

GOOD = {"server": "AFSW.database.windows.net", "database_name": "simplicitydb", "username": "loader", "password_env_var": "", "schema_name": ""}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


class _Cur:
    def __init__(self, schema, tables): self.schema, self.tables, self.last = schema, tables, ""
    def execute(self, sql, params=()): self.last = sql
    def fetchone(self):
        return ("simplicitydb",) if "DB_NAME" in self.last else ((1,) if self.schema else None)
    def fetchall(self): return [(t,) for t in self.tables]


class _Db:
    def __init__(self, schema=True, tables=()): self.c = _Cur(schema, list(tables)); self.closed = False
    def cursor(self): return self.c
    def close(self): self.closed = True


def test_validation_defaults_and_rejects_a_typed_password():
    v = st.validate(GOOD)
    assert v["server"] == "afsw.database.windows.net" and v["password_env_var"] == "SQL_TARGET_PASSWORD" and v["schema_name"] == "simplicity"
    for key, bad in [("server", "localhost"), ("password_env_var", "P@ssw0rd!"), ("password_env_var", "POSTGRES_PASSWORD"), ("schema_name", "a;b")]:
        with pytest.raises(ValueError):
            st.validate({**GOOD, key: bad})


def test_test_reports_missing_schema_then_tables_and_never_returns_the_password(conn, monkeypatch):
    st.save(conn, "Ann", GOOD)
    monkeypatch.delenv("SQL_TARGET_PASSWORD", raising=False)
    assert st.test_connection(conn)["status"] == "FAILED"
    monkeypatch.setenv("SQL_TARGET_PASSWORD", "s3cret")
    db = _Db(schema=False)
    cfg = st.test_connection(conn, lambda c, p: db)
    assert cfg["status"] == "CONNECTED" and "does not exist yet" in cfg["check_detail"] and db.closed
    cfg = st.test_connection(conn, lambda c, p: _Db(True, ["ORION_MCL"]))
    assert "1 table(s): ORION_MCL" in cfg["check_detail"]

    def boom(c, p): raise RuntimeError("Login failed for user; password s3cret rejected")
    cfg = st.test_connection(conn, boom)
    assert cfg["status"] == "FAILED" and "s3cret" not in str(cfg) and "***" in cfg["check_detail"]


def test_user_name_falls_back_to_the_server_environment(conn, monkeypatch):
    st.save(conn, "Ann", {**GOOD, "username": ""})
    monkeypatch.setenv("SQL_TARGET_PASSWORD", "x")
    monkeypatch.delenv("SQL_TARGET_USERNAME", raising=False)
    assert st.test_connection(conn, lambda c, p: _Db())["status"] == "FAILED"
    monkeypatch.setenv("SQL_TARGET_USERNAME", "loader")
    seen = {}
    cfg = st.test_connection(conn, lambda c, p: seen.update(user=c["username"]) or _Db())
    assert cfg["status"] == "CONNECTED" and seen["user"] == "loader"


def test_read_schema_returns_tables_columns_types_and_required_flags(conn, monkeypatch):
    st.save(conn, "Ann", {**GOOD, "username": "u"})
    monkeypatch.setenv("SQL_TARGET_PASSWORD", "x")

    class Cur:
        def execute(self, sql, params=()): self.params = params
        def fetchall(self): return [("T1", "ID", "nvarchar", "NO"), ("T1", "DOB", "date", "YES"), ("T2", "N", "decimal", "YES")]

    class Db:
        def cursor(self): return Cur()
        def close(self): pass
    sch = st.read_schema(conn, lambda c, p: Db())
    assert [t["name"] for t in sch["tables"]] == ["T1", "T2"] and sch["tables"][0]["columns"][0] == {"name": "ID", "type": "string", "required": True, "generated": False, "description": "", "code_list": None}
    assert sch["tables"][0]["columns"][1]["type"] == "date" and sch["tables"][1]["columns"][0]["type"] == "decimal" and sch["code_lists"] == {}


def test_azure_sql_can_be_the_target_system_and_is_inferred_from_its_catalog(conn, monkeypatch):
    from control_plane.services import target_connector as tc
    st.save(conn, "Ann", {**GOOD, "username": "u"})
    monkeypatch.setenv("SQL_TARGET_PASSWORD", "x")
    schema = {"system": "Azure SQL xodus.simplicity", "version": "", "code_lists": {}, "tables": [
        {"name": "ORION_MCL_ACCOUNT", "description": "", "columns": [
            {"name": n, "type": "string", "required": False, "generated": False, "description": "", "code_list": None}
            for n in ("ACCOUNT_NUMBER", "CUSTODIAN", "REGISTRATION_TYPE")]}]}
    monkeypatch.setattr(st, "read_schema", lambda c: schema)
    monkeypatch.setattr(st, "test_connection", lambda c: {"status": "CONNECTED", "found_tables": "ORION_MCL_ACCOUNT", "check_detail": "Signed in to XODUS."})
    cfg = tc.configure_azure_sql(conn, "Ann", "Sam")
    assert cfg["connector_type"] == "azure-sql" and cfg["status"] == "CONNECTED" and cfg["endpoint"] == "azure-sql://afsw.database.windows.net/simplicitydb/simplicity"
    cfg = tc.infer_model(conn, {"Account": {"ACCT_NUM": "string", "CUSTODIAN_NAME": "string", "REG_TYPE": "string"}})
    assert cfg["status"] == "INFERRED" and "Account" in cfg["model"]["entities"] and cfg["model"]["entities"]["Account"]["target_table"] == ["ORION_MCL_ACCOUNT"] or "ORION_MCL_ACCOUNT" in str(cfg["model"]["entities"]["Account"]["target_table"])
    monkeypatch.setattr(st, "test_connection", lambda c: {"status": "CONNECTED", "found_tables": "", "check_detail": "Signed in. Schema exists but has no tables yet."})
    assert tc.configure_azure_sql(conn, "Ann", "Sam")["status"] == "CONFIGURED"  # no tables yet: not connected for inference
