"""Azure SQL adapter that lets the control-plane code keep its sqlite3-style calls (the platform's own tables in the XODUS database).

Used when DATABASE_URL is mssql://user:password@server[:port]/database. The SQL user should have DEFAULT_SCHEMA = xodus_app
(deploy/xodus/01_app_schema.sql), so the platform's unqualified table names land in their own schema, apart from the data plane.

What is translated (only what the app uses):
- `?` and `:name` placeholders (and literal `%` when parameters are passed);
- CREATE TABLE IF NOT EXISTS: TEXT -> NVARCHAR(MAX), or a bounded NVARCHAR for key and unique columns (index keys are limited to
  900 bytes), INTEGER -> BIGINT, REAL -> FLOAT, BYTEA/BLOB -> VARBINARY(MAX); text columns are case-sensitive like SQLite/PostgreSQL;
- ALTER TABLE ... ADD COLUMN; CREATE INDEX IF NOT EXISTS (skipped: the tables are small and many indexed columns are unbounded);
- INSERT ... ON CONFLICT (keys) DO UPDATE SET ... -> MERGE; INSERT ... ON CONFLICT DO NOTHING -> a duplicate key is ignored (rowcount 0);
- a trailing LIMIT n [OFFSET m] -> TOP (n), or OFFSET/FETCH when the query is ordered; BEGIN IMMEDIATE -> a no-op.
Rows can be read by index, by column name and with dict(row), like sqlite3.Row. Connections come from a small pool.
"""
import contextvars
import queue
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

COLLATION = "Latin1_General_100_CS_AS"
DUPLICATE_KEY = (2627, 2601)


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


# ---- translation ----

def _split_top(text: str, sep: str = ",") -> List[str]:
    """Split on sep outside parentheses and quotes."""
    out, depth, cur, quote = [], 0, [], False
    for ch in text:
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        if ch == sep and depth == 0 and not quote:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def _matching_paren(text: str, start: int) -> int:
    depth, quote = 0, False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "'":
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unbalanced parentheses in SQL.")


def _col_type(kind: str, bounded: Optional[int]) -> str:
    k = kind.upper()
    if k in ("TEXT", "VARCHAR", "CHAR", "STRING"):
        return (f"NVARCHAR({bounded})" if bounded else "NVARCHAR(MAX)") + f" COLLATE {COLLATION}"
    if k in ("INTEGER", "INT", "BIGINT"):
        return "BIGINT"
    if k in ("REAL", "FLOAT", "DOUBLE", "NUMERIC"):
        return "FLOAT"
    if k in ("BYTEA", "BLOB"):
        return "VARBINARY(MAX)"
    if k in ("BOOLEAN", "BOOL"):
        return "BIT"
    raise ValueError(f"Unsupported column type {kind}.")


def translate_create_table(sql: str) -> str:
    m = re.match(r"\s*CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", sql, re.I | re.S)
    name = m.group(1)
    open_at = m.end() - 1
    body = sql[open_at + 1:_matching_paren(sql, open_at)]
    parts = _split_top(body)
    keyed: List[List[str]] = []  # column groups that form an index key (primary key or unique)
    for p in parts:
        w = p.split()
        head = w[0].upper()
        if head in ("PRIMARY", "UNIQUE"):
            keyed.append([c.strip() for c in p[p.index("(") + 1:p.rindex(")")].split(",")])
        elif len(w) > 2 and re.search(r"\b(PRIMARY\s+KEY|UNIQUE)\b", p, re.I):
            keyed.append([w[0]])
    width: Dict[str, int] = {}
    for group in keyed:
        n = max(64, 450 // max(1, len(group)))  # 900 bytes per index key, 2 bytes per character
        for c in group:
            width[c] = min(width.get(c, n), n)
    cols = []
    for p in parts:
        w = p.split()
        if w[0].upper() in ("PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"):
            cols.append(p)
            continue
        rest = " ".join(w[2:])
        cols.append(f"[{w[0]}] {_col_type(w[1], width.get(w[0]))}" + (f" {rest}" if rest else ""))
    return f"IF OBJECT_ID(N'{name}', N'U') IS NULL CREATE TABLE [{name}] ({', '.join(cols)})"


def translate_alter(sql: str) -> str:
    m = re.match(r"\s*ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)\s+(\w+)(.*)$", sql, re.I | re.S)
    return f"ALTER TABLE [{m.group(1)}] ADD [{m.group(2)}] {_col_type(m.group(3), None)}{m.group(4).rstrip().rstrip(';')}"


def translate_upsert(sql: str) -> str:
    """INSERT INTO t (cols) VALUES (vals) ON CONFLICT (keys) DO UPDATE SET a = excluded.a, ... -> MERGE (same placeholder order)."""
    m = re.match(r"\s*INSERT INTO\s+(\w+)\s*\(", sql, re.I | re.S)
    table = m.group(1)
    c_open = m.end() - 1
    c_close = _matching_paren(sql, c_open)
    cols = [c.strip() for c in sql[c_open + 1:c_close].split(",")]
    v = re.compile(r"\s*VALUES\s*\(", re.I).match(sql, c_close + 1)
    v_close = _matching_paren(sql, v.end() - 1)
    vals = _split_top(sql[v.end():v_close])
    tail = sql[v_close + 1:]
    k = re.match(r"\s*ON CONFLICT\s*\(([^)]*)\)\s*DO UPDATE SET\s*(.*)$", tail, re.I | re.S)
    keys = [c.strip() for c in k.group(1).split(",")]
    sets = re.sub(rf"\b{table}\.", "tgt.", k.group(2).strip().rstrip(";"))
    sets = ", ".join(f"tgt.{a.strip()}" if re.match(r"^\w+\s*=", a.strip()) else a for a in _split_top(sets))
    using = ", ".join(f"{val} AS [{col}]" for col, val in zip(cols, vals))
    on = " AND ".join(f"tgt.[{c}] = excluded.[{c}]" for c in keys)
    return (f"MERGE [{table}] AS tgt USING (SELECT {using}) AS excluded ON {on} "
            f"WHEN MATCHED THEN UPDATE SET {sets} "
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(f'[{c}]' for c in cols)}) VALUES ({', '.join(f'excluded.[{c}]' for c in cols)});")


def translate_limit(sql: str) -> str:
    m = re.search(r"\s+LIMIT\s+(\?|\d+|:\w+)(?:\s+OFFSET\s+(\?|\d+|:\w+))?\s*;?\s*$", sql, re.I)
    if not m:
        return sql
    head, n, off = sql[:m.start()], m.group(1), m.group(2)
    if re.search(r"\bORDER\s+BY\b", head, re.I):
        return f"{head} OFFSET {off or 0} ROWS FETCH NEXT {n} ROWS ONLY"
    if off:
        return f"{head} ORDER BY (SELECT NULL) OFFSET {off} ROWS FETCH NEXT {n} ROWS ONLY"
    return re.sub(r"^\s*SELECT\s+(DISTINCT\s+)?", lambda x: f"SELECT {x.group(1) or ''}TOP ({n}) ", head, count=1, flags=re.I)


_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def _concat(sql: str) -> str:
    """|| (string concatenation) -> + outside string literals; both give NULL when a part is NULL."""
    parts = sql.split("'")
    return "'".join(p.replace("||", "+") if i % 2 == 0 else p for i, p in enumerate(parts))


def translate(sql: str, named: bool = False, has_params: bool = True) -> Tuple[str, str]:
    """(T-SQL, mode). mode is "ignore_duplicate" for INSERT ... ON CONFLICT DO NOTHING, else "plain"."""
    s = sql.strip()
    up = s.upper()
    mode = "plain"
    if up.startswith("BEGIN"):
        return "SELECT 1", mode
    if up.startswith("CREATE TABLE IF NOT EXISTS"):
        s = translate_create_table(s)
    elif re.match(r"CREATE (UNIQUE )?INDEX IF NOT EXISTS", up):
        return "SELECT 1", mode
    elif up.startswith("ALTER TABLE") and " ADD COLUMN " in up:
        s = translate_alter(s)
    elif up.startswith("INSERT") and re.search(r"ON CONFLICT\s*\([^)]*\)\s*DO UPDATE", s, re.I):
        s = translate_upsert(s)
    elif up.startswith("INSERT") and re.search(r"ON CONFLICT\s+DO NOTHING", s, re.I):
        s, mode = re.sub(r"\s*ON CONFLICT\s+DO NOTHING\s*;?\s*$", "", s, flags=re.I), "ignore_duplicate"
    else:
        s = translate_limit(s)
    s = _concat(s)
    if has_params:
        s = s.replace("%", "%%")
        s = _NAMED.sub(r"%(\1)s", s) if named else s.replace("?", "%s")
    return s, mode


# ---- connection ----

def _param(value):
    """pymssql sends bytes as text; binary values (stored files, packages) must be bytearray to reach a VARBINARY column."""
    return bytearray(value) if isinstance(value, (bytes, memoryview)) else value


def params_for_mssql(params):
    if isinstance(params, dict):
        return {k: _param(v) for k, v in params.items()}
    return tuple(_param(v) for v in params)


# Table definitions run once per process: the app calls CREATE TABLE IF NOT EXISTS (and its column checks) on most requests, which
# is free on a local file but a network round trip each on Azure SQL. A definition that ran is not sent again; column lookups are
# remembered until a column is added.
_applied_ddl: set = set()
_catalogue: Dict[Any, List[Tuple]] = {}
_cache_lock = threading.Lock()
_CATALOGUE_SQL = re.compile(r"^\s*SELECT\s+(column_name|1)\s+FROM\s+information_schema\.(columns|tables)\b", re.I)


def _is_ddl(sql: str) -> bool:
    u = sql.lstrip().upper()
    return u.startswith("CREATE TABLE IF NOT EXISTS") or u.startswith("CREATE INDEX IF NOT EXISTS") or u.startswith("CREATE UNIQUE INDEX IF NOT EXISTS")


def reset_caches() -> None:
    with _cache_lock:
        _applied_ddl.clear()
        _catalogue.clear()


class _Cached:
    """Stands in for a pymssql cursor when the answer is already known."""
    def __init__(self, rows, description):
        self._rows, self.description, self.rowcount = list(rows), description, -1

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, size=1):
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def close(self):
        pass


# Per-request read cache: within one request an identical read (same SQL, same parameters) is answered from memory. Any write in
# the request empties it, so a request always sees its own changes. The app opens it per request (request_cache()).
_request_reads: contextvars.ContextVar = contextvars.ContextVar("xodus_request_reads", default=None)


class request_cache:
    """with request_cache(): ... ; reads repeated inside the block are served once."""
    def __enter__(self):
        self._token = _request_reads.set({})
        return self

    def __exit__(self, *exc):
        _request_reads.reset(self._token)
        return False


_READ = re.compile(r"^\s*(SELECT|WITH)\b", re.I)


class _Cursor:
    def __init__(self, cur, owner=None):
        self._owner = owner  # the connection, told when a statement may have changed something
        self._cur = cur
        self._names: List[str] = []
        self.rowcount = -1

    def execute(self, sql: str, params=None):
        if _is_ddl(sql) and sql in _applied_ddl:
            self._names, self.rowcount = [], -1
            return self
        key = (sql, tuple(params) if isinstance(params, (list, tuple)) else None) if _CATALOGUE_SQL.match(sql) else None
        if key and key in _catalogue:
            rows, desc = _catalogue[key]
            self._cur, self._names, self.rowcount = _Cached(rows, desc), [d[0] for d in desc] if desc else [], -1
            return self
        reads = _request_reads.get()
        is_read = bool(_READ.match(sql))
        if self._owner is not None and not is_read:
            self._owner._dirty = True
        if reads is not None and not is_read:
            reads.clear()
        rkey = None
        if reads is not None and is_read and not isinstance(params, dict):
            try:
                rkey = (sql, tuple(params) if params else ())
                hash(rkey)
            except TypeError:
                rkey = None
            if rkey is not None and rkey in reads:
                rows, desc = reads[rkey]
                self._cur, self._names, self.rowcount = _Cached(rows, desc), [d[0] for d in desc] if desc else [], -1
                return self
        named = isinstance(params, dict)
        has = bool(params)
        text, mode = translate(sql, named, has)
        args = params_for_mssql(params) if has else None
        try:
            self._cur.execute(text, args) if has else self._cur.execute(text)
            self.rowcount = self._cur.rowcount
        except Exception as exc:
            if mode == "ignore_duplicate" and getattr(exc, "args", None) and exc.args[0] in DUPLICATE_KEY:
                self.rowcount = 0
            else:
                raise
        self._names = [d[0] for d in self._cur.description] if self._cur.description else []
        if _is_ddl(sql):
            with _cache_lock:
                _applied_ddl.add(sql)
        elif re.match(r"\s*ALTER\s+TABLE", sql, re.I):
            with _cache_lock:
                _catalogue.clear()
        elif key:
            rows = self._cur.fetchall()
            with _cache_lock:
                _catalogue[key] = (rows, self._cur.description)
            self._cur = _Cached(rows, self._cur.description)
        elif rkey is not None and self._cur.description:
            rows = self._cur.fetchall()
            reads[rkey] = (rows, self._cur.description)
            self._cur = _Cached(rows, self._cur.description)
        return self

    def executemany(self, sql: str, seq):
        for p in seq:
            self.execute(sql, p)
        return self

    def _wrap(self, values):
        return Row(values, self._names) if values is not None else None

    def fetchone(self):
        return self._wrap(self._cur.fetchone()) if self._names else None

    def fetchall(self):
        return [Row(v, self._names) for v in self._cur.fetchall()] if self._names else []

    def fetchmany(self, size=None):
        return [Row(v, self._names) for v in self._cur.fetchmany(size or 1)] if self._names else []

    @property
    def description(self):
        return self._cur.description

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        self._cur.close()


class MssqlConnection:
    def __init__(self, pool: "_Pool"):
        self._pool, self._conn, self._dirty = pool, pool.get(), False

    def execute(self, sql: str, params=None) -> _Cursor:
        return _Cursor(self._conn.cursor(), self).execute(sql, params)

    def executemany(self, sql: str, seq) -> _Cursor:
        return _Cursor(self._conn.cursor(), self).executemany(sql, seq)

    def cursor(self) -> _Cursor:
        return _Cursor(self._conn.cursor(), self)

    # Reads only (the most common case on a page): nothing to commit or roll back, so no round trip is spent on it.
    def commit(self) -> None:
        if self._dirty:
            self._conn.commit()
            self._dirty = False

    def rollback(self) -> None:
        if self._dirty:
            self._conn.rollback()
            self._dirty = False

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            try:
                if self._dirty:
                    conn.rollback()  # discard anything uncommitted, as SQLite's close() does
            except Exception:
                self._pool.discard(conn)
                return
            self._pool.put(conn)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def parse_url(url: str) -> Dict[str, Any]:
    u = urlparse(url)
    if u.scheme not in ("mssql", "sqlserver") or not u.hostname or not u.path.strip("/"):
        raise ValueError("DATABASE_URL must look like mssql://user:password@server.database.windows.net:1433/database")
    return {"server": u.hostname, "port": u.port or 1433, "user": unquote(u.username or ""), "password": unquote(u.password or ""),
            "database": u.path.strip("/")}


class _Pool:
    def __init__(self, url: str, size: int = 8, idle_check_s: int = 60):
        self._cfg, self._idle, self._q = parse_url(url), idle_check_s, queue.LifoQueue(maxsize=size)

    def _new(self):
        import pymssql
        c = self._cfg
        return pymssql.connect(server=c["server"], port=c["port"], user=c["user"], password=c["password"], database=c["database"],
                               login_timeout=20, timeout=120, tds_version="7.4", autocommit=False)

    def get(self):
        while True:
            try:
                conn, used = self._q.get_nowait()
            except queue.Empty:
                return self._new()
            if time.time() - used < self._idle:
                return conn
            try:  # idle for a while: make sure Azure did not drop it
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchall()
                return conn
            except Exception:
                self.discard(conn)

    def put(self, conn) -> None:
        try:
            self._q.put_nowait((conn, time.time()))
        except queue.Full:
            self.discard(conn)

    @staticmethod
    def discard(conn) -> None:
        try:
            conn.close()
        except Exception:
            pass


_pools: Dict[str, _Pool] = {}
_lock = threading.Lock()


def connect(url: str) -> MssqlConnection:
    with _lock:
        pool = _pools.get(url) or _pools.setdefault(url, _Pool(url))
    return MssqlConnection(pool)
