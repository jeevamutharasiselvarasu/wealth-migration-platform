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

class _Cursor:
    def __init__(self, cur):
        self._cur = cur
        self._names: List[str] = []
        self.rowcount = -1

    def execute(self, sql: str, params=None):
        named = isinstance(params, dict)
        has = bool(params)
        text, mode = translate(sql, named, has)
        args = (params if named else tuple(params)) if has else None
        try:
            self._cur.execute(text, args) if has else self._cur.execute(text)
            self.rowcount = self._cur.rowcount
        except Exception as exc:
            if mode == "ignore_duplicate" and getattr(exc, "args", None) and exc.args[0] in DUPLICATE_KEY:
                self.rowcount = 0
            else:
                raise
        self._names = [d[0] for d in self._cur.description] if self._cur.description else []
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
        self._pool, self._conn = pool, pool.get()

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
