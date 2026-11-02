"""Create (once) and introspect the simulated target database. Standalone: no platform imports."""
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict

import yaml

SEED_PATH = Path(__file__).resolve().parent / "seed.yaml"
DEFAULT_DB = Path(__file__).resolve().parent / "data" / "target.db"
_IDENT = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SQL = {"string": "TEXT", "date": "DATE", "integer": "INTEGER", "number": "REAL"}
_BACK = {"TEXT": "string", "DATE": "date", "INTEGER": "integer", "REAL": "number"}


def db_path() -> Path:
    return Path(os.environ.get("SIMULATED_TARGET_DB", DEFAULT_DB))


def _ident(name: str) -> str:
    if not _IDENT.match(str(name)):
        raise ValueError(f"Invalid identifier in seed: {name!r}")
    return name


def bootstrap(path: Path | None = None) -> bool:
    """Create the database from the seed if it does not exist. Returns True when created; never alters an existing one."""
    path = Path(path or db_path())
    if path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = yaml.safe_load(SEED_PATH.read_text(encoding="utf-8"))
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE TARGET_META (KEY TEXT PRIMARY KEY, VALUE TEXT)")
        conn.executemany("INSERT INTO TARGET_META VALUES (?, ?)", [("system", seed["system"]), ("version", str(seed["version"]))])
        conn.execute("CREATE TABLE DATA_DICTIONARY (TABLE_NAME TEXT, COLUMN_NAME TEXT, DESCRIPTION TEXT, CODE_LIST TEXT, GENERATED INTEGER, PRIMARY KEY (TABLE_NAME, COLUMN_NAME))")
        conn.execute("CREATE TABLE TABLE_DICTIONARY (TABLE_NAME TEXT PRIMARY KEY, DESCRIPTION TEXT)")
        for name, codes in (seed.get("code_lists") or {}).items():
            conn.execute(f"CREATE TABLE {_ident(name)} (CODE TEXT PRIMARY KEY)")
            conn.executemany(f"INSERT INTO {name} VALUES (?)", [(_ident(str(c)),) for c in codes])
        for table, body in seed["tables"].items():
            cols = []
            for c in body["columns"]:
                sql = f"{_ident(c['name'])} {_SQL[c.get('type', 'string')]}"
                if c.get("generated"):
                    sql += " NOT NULL"
                elif c.get("required"):
                    sql += " NOT NULL"
                cols.append(sql)
                conn.execute("INSERT INTO DATA_DICTIONARY VALUES (?, ?, ?, ?, ?)",
                             (table, c["name"], c.get("description", ""), c.get("code_list"), 1 if c.get("generated") else 0))
            conn.execute(f"CREATE TABLE {_ident(table)} ({', '.join(cols)})")
            conn.execute("INSERT INTO TABLE_DICTIONARY VALUES (?, ?)", (table, body.get("description", "")))
        conn.commit()
    finally:
        conn.close()
    return True


def read_schema(path: Path | None = None) -> Dict[str, Any]:
    """Introspect the live database (read-only) and return its schema with the data dictionary."""
    path = Path(path or db_path())
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = {r["KEY"]: r["VALUE"] for r in conn.execute("SELECT * FROM TARGET_META")}
        dd = {(r["TABLE_NAME"], r["COLUMN_NAME"]): dict(r) for r in conn.execute("SELECT * FROM DATA_DICTIONARY")}
        tdesc = {r["TABLE_NAME"]: r["DESCRIPTION"] for r in conn.execute("SELECT * FROM TABLE_DICTIONARY")}
        code_lists = {}
        tables = []
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            name = r["name"]
            if name in {"TARGET_META", "DATA_DICTIONARY", "TABLE_DICTIONARY"}:
                continue
            if name not in tdesc:  # a reference (code list) table
                code_lists[name] = [x[0] for x in conn.execute(f"SELECT CODE FROM {_ident(name)} ORDER BY CODE")]
                continue
            columns = []
            for c in conn.execute(f"PRAGMA table_info({_ident(name)})"):
                d = dd.get((name, c["name"]), {})
                columns.append({"name": c["name"], "type": _BACK.get((c["type"] or "").upper(), "string"),
                                "required": bool(c["notnull"]) and not d.get("GENERATED"), "generated": bool(d.get("GENERATED")),
                                "description": d.get("DESCRIPTION") or "", "code_list": d.get("CODE_LIST")})
            tables.append({"name": name, "description": tdesc.get(name, ""), "columns": columns})
        return {"system": meta.get("system", "unknown"), "version": meta.get("version", ""), "tables": tables, "code_lists": code_lists}
    finally:
        conn.close()
