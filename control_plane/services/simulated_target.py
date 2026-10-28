"""Simulated Simplicity target system, provisioned once into a local SQLite file.

The target tables are deliberately NOT a copy of the canonical model: tables are split, keys are
target-assigned surrogates, and names, types and code lists differ. The system is created from
platform_knowledge/simplicity_target_system.yaml exactly once per control plane; afterwards it is
read-only reference data (describe() only introspects). No migration data is ever written to it.
"""
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

import control_plane.db as cdb

SPEC_PATH = Path(__file__).resolve().parents[2] / "platform_knowledge" / "simplicity_target_system.yaml"
_IDENT = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SQL_TYPE = {"string": "TEXT", "date": "TEXT", "integer": "INTEGER", "number": "REAL"}

_DDL = """
CREATE TABLE IF NOT EXISTS target_system_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    spec_id TEXT NOT NULL,
    spec_version TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    provisioned_by TEXT NOT NULL,
    provisioned_utc TEXT NOT NULL
)
"""


def target_db_path() -> Path:
    return Path(cdb.DB_PATH).parent / "simulated_target.db"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def snapshot(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM target_system_snapshots ORDER BY provisioned_utc DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _ident(name: str) -> str:
    if not _IDENT.match(str(name)):
        raise ValueError(f"Invalid identifier in target specification: {name!r}")
    return name


def _build_ddl(spec: Dict[str, Any]) -> List[str]:
    statements: List[str] = []
    for list_name, codes in (spec.get("code_lists") or {}).items():
        _ident(list_name)
        statements.append(f"CREATE TABLE {list_name} (CODE TEXT PRIMARY KEY)")
        statements.extend(f"INSERT INTO {list_name} (CODE) VALUES ('{_ident(str(c))}')" for c in codes)
    for body in spec["entities"].values():
        for position, (table, fields) in enumerate(body["tables"].items()):
            cols = []
            for f in fields:
                name, sql_type = _ident(f["name"]), _SQL_TYPE.get(f.get("type", "string"))
                if sql_type is None:
                    raise ValueError(f"Unsupported column type {f.get('type')!r} for {table}.{name}")
                if f.get("generated"):
                    cols.append(f"{name} INTEGER PRIMARY KEY" if position == 0 else f"{name} INTEGER NOT NULL")
                else:
                    cols.append(f"{name} {sql_type}" + (" NOT NULL" if f.get("required") else ""))
            statements.append(f"CREATE TABLE {_ident(table)} ({', '.join(cols)})")
    return statements


def provision(conn: sqlite3.Connection, actor: str, spec_path: Optional[Path] = None) -> Dict[str, Any]:
    """One-time creation of the simulated target database. Refuses to run twice."""
    ensure_table(conn)
    if snapshot(conn) is not None:
        raise ValueError("The simulated target system is already provisioned; it is set once and read-only.")
    raw = Path(spec_path or SPEC_PATH).read_text(encoding="utf-8")
    spec = yaml.safe_load(raw)
    statements = _build_ddl(spec)
    path = target_db_path()
    if path.exists():
        path.unlink()  # orphan from a previous control plane; the snapshot row is the source of truth
    path.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(path)
    try:
        for stmt in statements:
            target.execute(stmt)
        target.commit()
    finally:
        target.close()
    row = {"snapshot_id": "TGTSYS-" + hashlib.sha256(raw.encode()).hexdigest()[:10].upper(), "spec_id": spec["spec_id"],
           "spec_version": str(spec["spec_version"]), "schema_hash": hashlib.sha256("\n".join(statements).encode()).hexdigest(),
           "provisioned_by": actor, "provisioned_utc": datetime.now(timezone.utc).isoformat()}
    conn.execute("INSERT INTO target_system_snapshots VALUES (:snapshot_id, :spec_id, :spec_version, :schema_hash, :provisioned_by, :provisioned_utc)", row)
    return row


def describe(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Introspect the provisioned database (read-only) and report drift against the specification."""
    path = target_db_path()
    if not path.is_file():
        return {"tables": [], "drift": ["Simulated target database file is missing."]}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables, actual = [], {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'SIMP_%' ORDER BY name"):
            cols = db.execute(f"PRAGMA table_info({_ident(name)})").fetchall()
            actual[name] = {c[1] for c in cols}
            tables.append({"table": name, "columns": [{"name": c[1], "type": c[2], "required": bool(c[3]) or bool(c[5])} for c in cols]})
    finally:
        db.close()
    drift = []
    for body in spec["entities"].values():
        for table, cols in body["table_columns"].items():
            if table not in actual:
                drift.append(f"{table} is in the specification but not in the target system.")
            else:
                drift.extend(f"{table}.{c} is in the specification but not in the target system." for c in cols if c not in actual[table])
    return {"tables": tables, "drift": drift}
