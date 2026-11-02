"""Generated migration artifacts (ADF package zip, sign-off evidence) kept as part of the migration's own data.

Files live under App_Data/Storage/<migration>/ARTIFACTS/<kind>/ and each save is registered in the control plane with a version,
content hash, size and author. Download is a separate, optional step from the stored copy. Nothing is deployed anywhere.
"""
import hashlib
import json
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
    if "provenance_json" not in {r[1] for r in conn.execute("PRAGMA table_info(stored_artifacts)").fetchall()}:
        conn.execute("ALTER TABLE stored_artifacts ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'")


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


def save(conn: sqlite3.Connection, migration_id: str, kind: str, data: bytes, actor: str, root: Optional[Path] = None,
         provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
           "generated_by": actor, "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "provenance_json": json.dumps(provenance or {}, sort_keys=True)}
    conn.execute("INSERT INTO stored_artifacts (artifact_id, migration_id, kind, artifact_version, filename, rel_path, size_bytes, content_sha256, generated_by, generated_utc, provenance_json) "
                 "VALUES (:artifact_id, :migration_id, :kind, :artifact_version, :filename, :rel_path, :size_bytes, :content_sha256, :generated_by, :generated_utc, :provenance_json)", row)
    row["provenance"] = provenance or {}
    return row


def _rec(cols, r) -> Dict[str, Any]:
    d = dict(zip(cols, r))
    d["provenance"] = json.loads(d.pop("provenance_json", "{}") or "{}")
    return d


def latest(conn: sqlite3.Connection, migration_id: str, kind: str) -> Optional[Dict[str, Any]]:
    found = list_artifacts(conn, migration_id, kind)
    return found[0] if found else None


def list_artifacts(conn: sqlite3.Connection, migration_id: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_table(conn)
    sql, args = "SELECT * FROM stored_artifacts WHERE migration_id = ?", [migration_id]
    if kind:
        sql, args = sql + " AND kind = ?", args + [kind]
    cur = conn.execute(sql + " ORDER BY kind, artifact_version DESC", args)
    cols = [c[0] for c in cur.description]
    return [_rec(cols, r) for r in cur.fetchall()]


def read(conn: sqlite3.Connection, artifact_id: str, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Returns the stored record plus its bytes, verifying the file still matches the recorded hash."""
    ensure_table(conn)
    cur = conn.execute("SELECT * FROM stored_artifacts WHERE artifact_id = ?", (artifact_id,))
    r = cur.fetchone()
    if not r:
        return None
    rec = _rec([c[0] for c in cur.description], r)
    path = (root or STORAGE_ROOT) / rec["rel_path"]
    if not path.is_file():
        raise ValueError("The stored file is missing from storage.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != rec["content_sha256"]:
        raise ValueError("The stored file no longer matches its recorded hash.")
    rec["data"], rec["media_type"] = data, KINDS[rec["kind"]][2]
    return rec


def build_provenance(conn: sqlite3.Connection, migration_id: str, spec: Optional[Dict[str, Any]], approved: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Exactly what a generated package is built from: approved target mapping ids, the target model version, the latest validation run
    and the latest deployment contract package. Identifiers and hashes only."""
    def one(sql: str):
        try:
            r = conn.execute(sql, (migration_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return r
    run = one("SELECT run_id, status FROM validation_runs WHERE migration_id = ? ORDER BY executed_utc DESC LIMIT 1")
    contract = one("SELECT package_id, package_version, content_sha256 FROM deployment_packages WHERE migration_id = ? ORDER BY package_version DESC LIMIT 1")
    ids = sorted(m["target_mapping_id"] for m in approved if m.get("status") == "APPROVED")
    return {"approved_target_mapping_ids": ids, "approved_mapping_set_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
            "target_model_version": (spec or {}).get("spec_version"),
            "validation_run_id": run[0] if run else None, "validation_run_status": run[1] if run else None,
            "contract_package": ({"package_id": contract[0], "version": contract[1], "content_sha256": contract[2]} if contract else None)}


def package_reference(conn: sqlite3.Connection, migration_id: str, current: Dict[str, Any], evidence_run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """What a sign-off cites: the latest stored ADF package and whether it still matches the current approved mappings
    and the validation run being signed off. Identifiers and booleans only."""
    art = latest(conn, migration_id, "ADF_PACKAGE")
    if not art:
        return None
    prov = art["provenance"]
    return {"artifact_id": art["artifact_id"], "version": art["artifact_version"], "content_sha256": art["content_sha256"],
            "generated_by": art["generated_by"], "generated_utc": art["generated_utc"],
            "built_from": {k: prov.get(k) for k in ("approved_mapping_set_sha256", "target_model_version", "validation_run_id", "validation_run_status", "contract_package")},
            "approved_target_mapping_count": len(prov.get("approved_target_mapping_ids", [])),
            "mapping_set_matches_current": prov.get("approved_mapping_set_sha256") == current.get("approved_mapping_set_sha256"),
            "built_from_signed_off_validation_run": bool(evidence_run_id) and prov.get("validation_run_id") == evidence_run_id}


def staleness(art: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    """Plain-language reasons the stored package no longer reflects the current approved state (empty list = current)."""
    prov, reasons = art.get("provenance") or {}, []
    if not prov:
        return ["No provenance was recorded for this package (saved before provenance existed); regenerate it."]
    then, now = prov.get("approved_target_mapping_ids", []), current.get("approved_target_mapping_ids", [])
    if prov.get("approved_mapping_set_sha256") != current.get("approved_mapping_set_sha256"):
        reasons.append(f"The approved target mappings changed ({len(then)} when built, {len(now)} now).")
    if prov.get("target_model_version") != current.get("target_model_version"):
        reasons.append("The confirmed target model changed.")
    if prov.get("validation_run_id") != current.get("validation_run_id"):
        reasons.append(f"The validation run moved on (built after {prov.get('validation_run_id') or 'no run'}, latest is {current.get('validation_run_id') or 'none'}).")
    pc, cc = prov.get("contract_package") or {}, current.get("contract_package") or {}
    if pc.get("content_sha256") != cc.get("content_sha256"):
        reasons.append(f"The deployment contract changed (v{pc.get('version', '-')} when built, v{cc.get('version', '-')} now).")
    return reasons
