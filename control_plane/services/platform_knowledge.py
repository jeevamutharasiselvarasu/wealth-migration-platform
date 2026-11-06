"""Platform-level knowledge: what the platform itself knows, shared by every migration workspace.

Kept apart from workspace (firm) knowledge, which belongs to one migration and holds that firm's documents. Platform knowledge is
the built-in vocabulary and reference files under platform_knowledge/ (read by the inference services) plus documents the
Platform Admin or Domain SME adds here. Nothing here comes from, or is shown with, a migration's data.
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from control_plane.services import document_text

ROOT = Path(__file__).resolve().parents[2] / "platform_knowledge"
CATEGORIES = ("vocabulary", "canonical_contract", "mapping_notes", "domain_research", "other")
ALLOWED_SUFFIXES = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".docx", ".pptx", ".pdf"}
MAX_BYTES = 5 * 1024 * 1024

# Built-in files and what reads them. A file not listed here is still shown, as reference only.
BUILT_IN = {
    "field_vocabulary.yaml": ("vocabulary", "Target inference: names target columns from their spelling."),
    "source_vocabulary.yaml": ("vocabulary", "Source and target inference: recognises source column spellings."),
    "canonical_contract.md": ("canonical_contract", "The canonical model contract every mapping lands on."),
    "source_mapping_notes.md": ("mapping_notes", "Reference notes on mapping source systems to the canonical model."),
    "wealth_management_research.md": ("domain_research", "Wealth-management domain background for reviewers."),
}

_DDL = """CREATE TABLE IF NOT EXISTS platform_knowledge_assets (
    asset_id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, text TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    added_by TEXT NOT NULL, added_utc TEXT NOT NULL, status TEXT NOT NULL)"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def built_in(root: Path = ROOT) -> List[Dict[str, Any]]:
    out = []
    for path in sorted(root.glob("*")) if root.is_dir() else []:
        if not path.is_file():
            continue
        category, used_by = BUILT_IN.get(path.name, ("other", "Reference only."))
        out.append({"asset_id": "BUILTIN-" + path.name, "name": path.name, "category": category, "used_by": used_by,
                    "size_bytes": path.stat().st_size, "source": "built-in", "status": "ACTIVE"})
    return out


def add(conn: sqlite3.Connection, name: str, category: str, content: bytes, actor: str) -> Dict[str, Any]:
    """Add a platform document. Its text is extracted and stored; the original bytes are not kept."""
    ensure_table(conn)
    name = Path(str(name or "")).name.strip()
    suffix = Path(name).suffix.lower()
    if not name or suffix not in ALLOWED_SUFFIXES:
        raise ValueError("Platform knowledge must be Markdown, text, CSV, JSON, YAML, Word, PowerPoint or PDF.")
    if category not in CATEGORIES:
        raise ValueError("Pick a category.")
    if not content:
        raise ValueError("The file is empty.")
    if len(content) > MAX_BYTES:
        raise ValueError("Platform knowledge files are limited to 5 MB.")
    if suffix in {".docx", ".pptx", ".pdf"}:
        text = document_text.extract(suffix, content)
    else:
        text = content.decode("utf-8", errors="replace")
    asset_id = "PK-" + uuid.uuid4().hex[:8].upper()
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT INTO platform_knowledge_assets (asset_id, name, category, text, size_bytes, added_by, added_utc, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE')",
                 (asset_id, name, category, text, len(content), actor, when))
    return {"asset_id": asset_id, "name": name, "category": category, "size_bytes": len(content), "added_by": actor, "added_utc": when}


def retire(conn: sqlite3.Connection, asset_id: str) -> None:
    ensure_table(conn)
    if conn.execute("UPDATE platform_knowledge_assets SET status = 'RETIRED' WHERE asset_id = ? AND status = 'ACTIVE'", (asset_id,)).rowcount == 0:
        raise ValueError("No active platform knowledge asset with that id (built-in files cannot be retired here).")


def added(conn: sqlite3.Connection, include_retired: bool = False) -> List[Dict[str, Any]]:
    ensure_table(conn)
    sql = "SELECT asset_id, name, category, size_bytes, added_by, added_utc, status FROM platform_knowledge_assets"
    rows = conn.execute(sql + ("" if include_retired else " WHERE status = 'ACTIVE'") + " ORDER BY added_utc DESC").fetchall()
    return [{**dict(r), "source": "added", "used_by": "Shared reference for every migration."} for r in rows]


def list_all(conn: sqlite3.Connection, root: Path = ROOT) -> List[Dict[str, Any]]:
    return built_in(root) + added(conn)


def text_of(conn: sqlite3.Connection, asset_id: str, root: Path = ROOT) -> Optional[str]:
    if asset_id.startswith("BUILTIN-"):
        path = root / asset_id[len("BUILTIN-"):]
        return path.read_text(encoding="utf-8", errors="replace") if path.parent == root and path.is_file() else None
    ensure_table(conn)
    row = conn.execute("SELECT text FROM platform_knowledge_assets WHERE asset_id = ? AND status = 'ACTIVE'", (asset_id,)).fetchone()
    return row["text"] if row else None
