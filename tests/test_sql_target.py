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
