"""Azure SQL adapter: the platform's SQL translated to T-SQL (no database needed; a recording connection stands in for pymssql)."""
import importlib
import pkgutil
import re

import pytest

from control_plane import mssql_compat as mc


class _RecCursor:
    def __init__(self, log, fail=None):
        self.log, self.fail, self.description, self.rowcount = log, fail, None, 1

    def execute(self, sql, args=None):
        self.log.append((sql, args))
        if self.fail:
            raise self.fail
        self.description = [("a",)] if sql.lstrip().upper().startswith("SELECT") else None  # pymssql describes every read

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _RecConn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _RecCursor(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def get(self):
        return self.conn

    def put(self, c):
        pass

    def discard(self, c):
        pass


def _connection():
    raw = _RecConn()
    return mc.MssqlConnection(_Pool(raw)), raw


def test_every_table_definition_of_the_app_translates():
    import control_plane.services as services
    from control_plane import file_store
    conn, raw = _connection()
    conn.execute(file_store._DDL)
    n = 0
    for mod in pkgutil.iter_modules(services.__path__):
        m = importlib.import_module(f"control_plane.services.{mod.name}")
        for fn in ("ensure_table", "ensure_tables"):
            if callable(getattr(m, fn, None)):
                getattr(m, fn)(conn)
                n += 1
    ddl = [s for s, _ in raw.log if "CREATE TABLE" in s]
    assert n > 25 and len(ddl) > 30
    for s in ddl:
        assert s.startswith("IF OBJECT_ID(N'") and " TEXT" not in s and "INTEGER" not in s and "BYTEA" not in s, s
        # text keys are bounded so the index key stays within 900 bytes
        if "PRIMARY KEY" in s:
            assert "NVARCHAR(MAX) COLLATE Latin1_General_100_CS_AS PRIMARY KEY" not in s, s


def test_key_columns_are_bounded_by_the_number_of_key_columns():
    s = mc.translate_create_table("CREATE TABLE IF NOT EXISTS t (a TEXT NOT NULL, b TEXT NOT NULL, c TEXT, d INTEGER DEFAULT 0, e REAL, f BYTEA, "
                                  "PRIMARY KEY (a, b))")
    assert "[a] NVARCHAR(225) COLLATE Latin1_General_100_CS_AS NOT NULL" in s and "[b] NVARCHAR(225)" in s
    assert "[c] NVARCHAR(MAX)" in s and "[d] BIGINT DEFAULT 0" in s and "[e] FLOAT" in s and "[f] VARBINARY(MAX)" in s
    assert mc.translate_create_table("CREATE TABLE IF NOT EXISTS u (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL)").count("NVARCHAR(450)") == 2


def test_upserts_become_merge_with_the_same_parameter_order():
    sql, mode = mc.translate("INSERT INTO relationship_reviews (migration_id, child, parent, status) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT (migration_id, child, parent) DO UPDATE SET status = excluded.status")
    assert mode == "plain" and sql.startswith("MERGE [relationship_reviews] AS tgt USING (SELECT %s AS [migration_id], %s AS [child], %s AS [parent], %s AS [status]) AS excluded")
    assert "ON tgt.[migration_id] = excluded.[migration_id] AND tgt.[child] = excluded.[child] AND tgt.[parent] = excluded.[parent]" in sql
    assert "WHEN MATCHED THEN UPDATE SET tgt.status = excluded.status" in sql and sql.endswith(";")
    sql, _ = mc.translate("""INSERT INTO mapping_information_requests (request_id, response) VALUES (?, ?)
        ON CONFLICT(request_id) DO UPDATE SET response = CASE WHEN excluded.response != '' THEN excluded.response
        ELSE mapping_information_requests.response END""")
    assert "tgt.response = CASE WHEN excluded.response != '' THEN excluded.response ELSE tgt.response END" in " ".join(sql.split())
    sql, _ = mc.translate("INSERT INTO canonical_mappings (mapping_id, mapping_type, is_confirmed) VALUES (?, 'APPROVED_ALIAS', 1) "
                          "ON CONFLICT(mapping_id) DO UPDATE SET is_confirmed = 1")
    assert "SELECT %s AS [mapping_id], 'APPROVED_ALIAS' AS [mapping_type], 1 AS [is_confirmed]" in sql


def test_every_upsert_in_the_code_translates():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    found = 0
    for f in list((root / "control_plane").rglob("*.py")) + list((root / "ui").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'(\"\"\"|")(\s*INSERT INTO[^"]*?ON CONFLICT\s*\([^)]*\)\s*DO UPDATE SET.*?)(\"\"\"|")\s*[,)]', text, re.S):
            body = m.group(2)
            if '" "' in body or "{" in body:
                continue
            sql, _ = mc.translate(body)
            assert sql.startswith("MERGE [") and "WHEN NOT MATCHED THEN INSERT" in sql, (f, body[:80])
            found += 1
    assert found >= 3


def test_insert_or_ignore_skips_a_duplicate_key_and_reports_no_row():
    raw = _RecConn()
    raw.cursor = lambda: _RecCursor(raw.log, fail=type("IntegrityError", (Exception,), {})(2627, b"Violation of PRIMARY KEY constraint"))
    conn = mc.MssqlConnection(_Pool(raw))
    cur = conn.execute("INSERT INTO t (a) VALUES (?) ON CONFLICT DO NOTHING", ("x",))
    assert cur.rowcount == 0 and raw.log[0][0] == "INSERT INTO t (a) VALUES (%s)"
    with pytest.raises(Exception):
        conn.execute("INSERT INTO t (a) VALUES (?)", ("x",))  # without ON CONFLICT DO NOTHING a duplicate is an error


@pytest.mark.parametrize("src,out", [
    ("SELECT * FROM t WHERE a = ? ORDER BY b DESC LIMIT 1", "SELECT * FROM t WHERE a = %s ORDER BY b DESC OFFSET 0 ROWS FETCH NEXT 1 ROWS ONLY"),
    ("SELECT * FROM t WHERE a = ? LIMIT 1", "SELECT TOP (1) * FROM t WHERE a = %s"),
    ("SELECT DISTINCT a FROM t LIMIT ?", "SELECT DISTINCT TOP (%s) a FROM t"),
    ("SELECT * FROM t ORDER BY a LIMIT ? OFFSET ?", "SELECT * FROM t ORDER BY a OFFSET %s ROWS FETCH NEXT %s ROWS ONLY"),
    ("BEGIN IMMEDIATE", "SELECT 1"),
    ("CREATE INDEX IF NOT EXISTS ix ON t (a)", "SELECT 1"),
    ("ALTER TABLE t ADD COLUMN c TEXT NOT NULL DEFAULT '{}'", "ALTER TABLE [t] ADD [c] NVARCHAR(MAX) COLLATE Latin1_General_100_CS_AS NOT NULL DEFAULT '{}'"),
    ("SELECT a || '.' || b FROM t WHERE c LIKE 'x||%' AND d = ?", "SELECT a + '.' + b FROM t WHERE c LIKE 'x||%%' AND d = %s"),
])
def test_statement_translation(src, out):
    assert mc.translate(src)[0] == out


def test_percent_is_left_alone_without_parameters_and_named_parameters_work():
    assert mc.translate("SELECT 1 FROM t WHERE a LIKE 'RULE:%'", has_params=False)[0] == "SELECT 1 FROM t WHERE a LIKE 'RULE:%'"
    assert mc.translate("SELECT * FROM t WHERE a = :mid", named=True)[0] == "SELECT * FROM t WHERE a = %(mid)s"


def test_url_and_database_kind(monkeypatch):
    from control_plane import db
    assert mc.parse_url("mssql://u%40x:p%23w@srv.database.windows.net:1433/xodusdb") == {
        "server": "srv.database.windows.net", "port": 1433, "user": "u@x", "password": "p#w", "database": "xodusdb"}
    with pytest.raises(ValueError):
        mc.parse_url("postgresql://u:p@h/db")
    monkeypatch.setenv("DATABASE_URL", "mssql://u:p@h:1433/db")
    assert db.database_kind() == "mssql" and db.using_postgres()  # a server database holds the data (files included)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert db.database_kind() == "postgresql"
    monkeypatch.delenv("DATABASE_URL")
    assert db.database_kind() == "sqlite" and not db.using_postgres()


def test_core_and_event_tables_translate(monkeypatch):
    from control_plane import db
    from control_plane.services import workflow_event_engine as wee
    raw = _RecConn()
    monkeypatch.setattr(db, "get_db_connection", lambda: mc.MssqlConnection(_Pool(raw)))
    monkeypatch.setattr(wee, "get_db_connection", lambda: mc.MssqlConnection(_Pool(raw)), raising=False)
    db.init_db()
    wee._ensure_event_table()
    ddl = [s for s, _ in raw.log if "CREATE TABLE" in s]
    names = {re.match(r"IF OBJECT_ID\(N'(\w+)'", s).group(1) for s in ddl}
    assert {"canonical_mappings", "mapping_suggestions", "workflow_events"} <= names
    assert all(" TEXT" not in s and "INTEGER" not in s for s in ddl)


def test_binary_values_are_sent_as_binary():
    raw = _RecConn()
    conn = mc.MssqlConnection(_Pool(raw))
    conn.execute("INSERT INTO stored_files (locator, content) VALUES (?, ?)", ("a", b"\x00\x01"))
    assert raw.log[0][1] == ("a", bytearray(b"\x00\x01")) and isinstance(raw.log[0][1][1], bytearray)
    assert mc.params_for_mssql([memoryview(b"x"), 1, None]) == (bytearray(b"x"), 1, None)


def test_table_definitions_run_once_and_column_lookups_are_cached_until_a_column_is_added():
    mc.reset_caches()
    raw = _RecConn()
    conn = mc.MssqlConnection(_Pool(raw))
    ddl = "CREATE TABLE IF NOT EXISTS t (a TEXT PRIMARY KEY)"
    conn.execute(ddl)
    conn.execute(ddl)
    look = "SELECT column_name FROM information_schema.columns WHERE table_schema = SCHEMA_NAME() AND table_name = ?"
    conn.execute(look, ("t",))
    conn.execute(look, ("t",))
    assert sum("CREATE TABLE" in s for s, _ in raw.log) == 1 and sum("information_schema" in s for s, _ in raw.log) == 1
    conn.execute("ALTER TABLE t ADD COLUMN b TEXT")
    conn.execute(look, ("t",))
    assert sum("information_schema" in s for s, _ in raw.log) == 2  # asked again after a column was added
    mc.reset_caches()


def test_reads_spend_no_round_trip_on_commit_or_rollback():
    class Raw(_RecConn):
        def __init__(self):
            super().__init__()
            self.ends = []

        def commit(self):
            self.ends.append("commit")

        def rollback(self):
            self.ends.append("rollback")
    raw = Raw()
    conn = mc.MssqlConnection(_Pool(raw))
    conn.execute("SELECT 1 FROM t WHERE a = ?", ("x",))
    conn.commit()
    conn.close()
    assert raw.ends == []
    conn = mc.MssqlConnection(_Pool(raw))
    conn.execute("UPDATE t SET a = ?", ("y",))
    conn.close()
    assert raw.ends == ["rollback"]  # an uncommitted change is still discarded on close
    conn = mc.MssqlConnection(_Pool(raw))
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()
    assert raw.ends == ["rollback", "commit"]


def test_a_request_reads_the_same_row_once_and_sees_its_own_writes():
    raw = _RecConn()
    with mc.request_cache():
        for _ in range(3):
            c = mc.MssqlConnection(_Pool(raw))
            c.execute("SELECT * FROM target_system_config WHERE id = ?", (1,))
            c.close()
        assert sum("target_system_config" in s for s, _ in raw.log) == 1
        c = mc.MssqlConnection(_Pool(raw))
        c.execute("UPDATE target_system_config SET a = ?", ("x",))
        c.execute("SELECT * FROM target_system_config WHERE id = ?", (1,))
        c.close()
        assert sum(s.startswith("SELECT * FROM target_system_config") for s, _ in raw.log) == 2  # read again after the write
    c = mc.MssqlConnection(_Pool(raw))
    c.execute("SELECT * FROM target_system_config WHERE id = ?", (1,))  # outside a request nothing is cached
    assert sum(s.startswith("SELECT * FROM target_system_config") for s, _ in raw.log) == 3
