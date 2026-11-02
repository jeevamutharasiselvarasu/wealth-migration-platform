"""Generated migration artifacts (ADF package zip, sign-off evidence) kept as part of the migration's own data.

Files live under App_Data/Storage/<migration>/ARTIFACTS/<kind>/ and each save is registered in the control plane with a version,
content hash, size and author. Download is a separate, optional step from the stored copy. Nothing is deployed anywhere.
"""
import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

KINDS = {"ADF_PACKAGE": ("adf_package", "zip", "application/zip"), "SIGNOFF_EVIDENCE": ("signoff_evidence", "json", "application/json")}
STORAGE_ROOT = Path("App_Data/Storage")

_DDL = """CREATE TABLE IF NOT EXISTS stored_artifacts (
    artifact_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, kind TEXT NOT NULL, artifact_version INTEGER NOT NULL,
    filename TEXT NOT NULL, rel_path TEXT NOT NULL, size_bytes INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
    generated_by TEXT NOT NULL, generated_utc TEXT NOT NULL, UNIQUE (migration_id, kind, artifact_version))"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


def save(conn: sqlite3.Connection, migration_id: str, kind: str, data: bytes, actor: str, root: Optional[Path] = None) -> Dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"Unsupported artifact kind: {kind}")
    ensure_table(conn)
    prefix, ext, _ = KINDS[kind]
    last = conn.execute("SELECT MAX(artifact_version) FROM stored_artifacts WHERE migration_id = ? AND kind = ?", (migration_id, kind)).fetchone()[0] or 0
    version = last + 1
    filename = f"{prefix}_{_safe(migration_id)}_v{version}.{ext}"
    rel = f"{_safe(migration_id)}/ARTIFACTS/{prefix}/{filename}"
    path = (root or STORAGE_ROOT) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = {"artifact_id": "ART-" + uuid.uuid4().hex[:8].upper(), "migration_id": migration_id, "kind": kind, "artifact_version": version,
           "filename": filename, "rel_path": rel, "size_bytes": len(data), "content_sha256": hashlib.sha256(data).hexdigest(),
           "generated_by": actor, "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    conn.execute("INSERT INTO stored_artifacts (artifact_id, migration_id, kind, artifact_version, filename, rel_path, size_bytes, content_sha256, generated_by, generated_utc) "
                 "VALUES (:artifact_id, :migration_id, :kind, :artifact_version, :filename, :rel_path, :size_bytes, :content_sha256, :generated_by, :generated_utc)", row)
    return row


def list_artifacts(conn: sqlite3.Connection, migration_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_table(conn)
    sql, args = "SELECT * FROM stored_artifacts WHERE migration_id = ?", [migration_id]
    if kind:
        sql, args = sql + " AND kind = ?", args + [kind]
    cur = conn.execute(sql + " ORDER BY kind, artifact_version DESC", args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def read(conn: sqlite3.Connection, artifact_id: str, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Returns the stored record plus its bytes, verifying the file still matches the recorded hash."""
    ensure_table(conn)
    cur = conn.execute("SELECT * FROM stored_artifacts WHERE artifact_id = ?", (artifact_id,))
    r = cur.fetchone()
    if not r:
        return None
    rec = dict(zip([c[0] for c in cur.description], r))
    path = (root or STORAGE_ROOT) / rec["rel_path"]
    if not path.is_file():
        raise ValueError("The stored file is missing from storage.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != rec["content_sha256"]:
        raise ValueError("The stored file no longer matches its recorded hash.")
    rec["data"], rec["media_type"] = data, KINDS[rec["kind"]][2]
    return rec
