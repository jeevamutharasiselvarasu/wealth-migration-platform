import os as _os

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _postgres_isolation(request, monkeypatch):
    """When XODUS_TEST_PG_URL is set, run each test against a freshly emptied PostgreSQL schema instead of its SQLite file.
    Tests that build their own SQLite connection or database fixture stay on SQLite, because they inspect that file directly."""
    import inspect
    url = _os.environ.get("XODUS_TEST_PG_URL")
    uses_sqlite_fixture = ("sqlite3.connect" in inspect.getsource(request.function) or "db" in request.fixturenames
                           or request.node.get_closest_marker("sqlite_only") is not None)
    if not url or uses_sqlite_fixture:
        yield
        return
    import psycopg
    from control_plane import pg_compat
    pg_compat.reset_pools()
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
        admin.execute("DROP SCHEMA public CASCADE")
        admin.execute("CREATE SCHEMA public")
    monkeypatch.setenv("DATABASE_URL", url)
    yield
    pg_compat.reset_pools()
