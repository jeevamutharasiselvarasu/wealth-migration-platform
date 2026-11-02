"""PostgreSQL adapter that lets the control-plane code keep its sqlite3-style calls.

- `?` placeholders are translated to `%s` (and literal `%` is escaped), so existing SQL runs unchanged.
- Rows can be read by index, by column name, and with dict(row), like sqlite3.Row.
- Connections come from a small pool; close() returns the connection (rolling back anything uncommitted).
Only the SQL features the app uses are supported (standard SQL, ON CONFLICT). SQLite-only syntax is not translated.
"""
import re
import threading
from typing import Any, Dict, Optional

import psycopg
from psycopg_pool import ConnectionPool


class Row(tuple):
    """A tuple that also answers row["column"] and keys()."""
    def __new__(cls, values, names):
        obj = super().__new__(cls, values)
        obj._names = tuple(names)
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return super().__getitem__(self._names.index(key))
            except ValueError:
                raise IndexError(f"No item with that key: {key}") from None
        return super().__getitem__(key)

    def keys(self):
        return list(self._names)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(values, names)


_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def translate(sql: str, named: bool = False) -> str:
    if sql.strip().upper().startswith("BEGIN"):  # SQLite's BEGIN IMMEDIATE: PostgreSQL opens the transaction on the first statement
        return "SELECT 1"
    sql = sql.replace("%", "%%")
    return _NAMED.sub(r"%(\1)s", sql) if named else sql.replace("?", "%s")


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params=None):
        named = isinstance(params, dict)
        self._cur.execute(translate(sql, named), (params if named else tuple(params)) if params else None)
        return self

    def executemany(self, sql: str, seq):
        self._cur.executemany(translate(sql), [tuple(p) for p in seq])
        return self

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)


class PgConnection:
    def __init__(self, pool: ConnectionPool):
        self._pool, self._conn = pool, pool.getconn()

    def execute(self, sql: str, params=None) -> _Cursor:
        return _Cursor(self._conn.cursor()).execute(sql, params)

    def executemany(self, sql: str, seq) -> _Cursor:
        return _Cursor(self._conn.cursor()).executemany(sql, seq)

    def cursor(self) -> _Cursor:
        return _Cursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            try:
                conn.rollback()  # discard anything uncommitted (reads leave a transaction open); same outcome as SQLite's close()
            except Exception:
                pass
            self._pool.putconn(conn)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


_pools: Dict[str, ConnectionPool] = {}
_lock = threading.Lock()


def _pool_for(url: str) -> ConnectionPool:
    with _lock:
        pool = _pools.get(url)
        if pool is None:
            pool = ConnectionPool(url, min_size=1, max_size=10, max_lifetime=600, open=False, check=ConnectionPool.check_connection,
                                  kwargs={"row_factory": _row_factory})
            pool.open(wait=True, timeout=30)
            _pools[url] = pool
        return pool


def connect(url: str) -> PgConnection:
    return PgConnection(_pool_for(url))


def reset_pools() -> None:
    """Close every pool (tests, or after the database was replaced)."""
    with _lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
