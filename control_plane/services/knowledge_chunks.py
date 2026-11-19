"""Knowledge assets cut into searchable chunks, each with a citation back to where it came from.

Every kind of knowledge (an uploaded file, a web page, a Confluence page) is reduced to text, split into chunks of a few hundred words
along its natural structure (a heading's section, a PDF page, a sheet's group of rows) and stored with: the asset, where it came from,
a version (content hash or page version), a citation ("Sheet Accounts, rows 2-31", "Section: Custodian codes", "Page 3"), and the
canonical entities the text mentions. Nothing here calls a model: the chunks are the input of the embedding and retrieval step.

Spreadsheet and CSV content is read, not just the header row, so a data dictionary or mapping specification becomes searchable. That
also means any client data in such a file would be stored; the upload page says so, and the limits below keep one file from flooding
the store. Nothing is executed: formulas are read as their cached values, macros and embedded objects are ignored.
"""
import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from control_plane.services import document_text

MAX_CHARS = 1800            # one chunk of prose
OVERLAP = 150               # characters repeated at the start of the next piece of an oversized paragraph
ROW_CHARS = 1500            # one chunk of table rows
MAX_ROWS_PER_SHEET = 5000
MAX_CHUNKS = 400            # per asset; a larger asset is cut off and says so
MAX_BYTES = 20 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
TABLE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}
DOC_SUFFIXES = {".docx", ".pptx", ".pdf"}
SUPPORTED = TEXT_SUFFIXES | TABLE_SUFFIXES | DOC_SUFFIXES

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, scope TEXT NOT NULL, asset_id TEXT NOT NULL, asset_name TEXT NOT NULL,
    source TEXT NOT NULL, source_ref TEXT, asset_version TEXT, status TEXT NOT NULL, seq INTEGER NOT NULL, text TEXT NOT NULL,
    citation TEXT, entities TEXT, created_utc TEXT NOT NULL
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def version_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:12]


# ------------------------------------------------------------------------------------------------------------------------ chunking
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _split_long(text: str) -> List[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + MAX_CHARS)
        if end < len(text):  # cut at the last sentence or space so a word is not split
            cut = max(text.rfind(". ", start, end), text.rfind("\n", start, end), text.rfind(" ", start + MAX_CHARS // 2, end))
            end = cut + 1 if cut > start + MAX_CHARS // 2 else end
        out.append(text[start:end].strip())
        start = max(end - OVERLAP, start + 1) if end < len(text) else end
    return [o for o in out if o]


def chunk_text(text: str, label: str = "Section") -> List[Dict[str, str]]:
    """Prose to chunks along headings and paragraphs: each chunk starts with its heading so it makes sense alone."""
    sections: List[tuple] = []
    heading, body = "", []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        m = _HEADING.match(line)
        if m:
            if "".join(body).strip():
                sections.append((heading, "\n".join(body)))
            heading, body = m.group(2).strip(), []
        else:
            body.append(line)
    if "".join(body).strip():
        sections.append((heading, "\n".join(body)))
    out: List[Dict[str, str]] = []
    part = 0
    for heading, body in sections:
        head = f"{heading}\n" if heading else ""
        room = max(MAX_CHARS - len(head), 400)
        buf = ""
        pieces: List[str] = []
        for para in (p.strip() for p in re.split(r"\n\s*\n", body)):
            if not para:
                continue
            for piece in _split_long(para) if len(para) > room else [para]:
                if buf and len(buf) + len(piece) + 2 > room:
                    pieces.append(buf)
                    buf = ""
                buf = f"{buf}\n\n{piece}" if buf else piece
        if buf:
            pieces.append(buf)
        for piece in pieces:
            part += 1
            out.append({"text": (head + piece).strip(), "citation": f"{label}: {heading}" if heading else f"Part {part}"})
    return out


def chunk_pages(pages: List[str]) -> List[Dict[str, str]]:
    """A PDF: each page's text, split further only when a page is long; the citation is the page number."""
    out: List[Dict[str, str]] = []
    for number, page in enumerate(pages, 1):
        for piece in _split_long(str(page or "").strip()):
            if piece:
                out.append({"text": piece, "citation": f"Page {number}"})
    return out


def _row_line(header: List[str], row: List[Any]) -> str:
    cells = [f"{h}={str(v).strip()}" for h, v in zip(header, row) if str(v).strip() and str(v).strip().lower() not in ("nan", "none")]
    return "; ".join(cells)


def chunk_rows(sheet: str, header: List[str], rows: Iterable[List[Any]], first_row: int = 2) -> List[Dict[str, str]]:
    """Table rows to chunks of "COLUMN=value; COLUMN=value" lines. Each chunk names the sheet and columns and cites its row range."""
    cols = [str(h).strip() or f"column{i + 1}" for i, h in enumerate(header)]
    lead = f"Sheet {sheet}. Columns: {', '.join(cols)}.\n"
    out: List[Dict[str, str]] = []
    lines: List[str] = []
    start = first_row
    size = len(lead)

    def flush(end: int) -> None:
        if lines:
            out.append({"text": lead + "\n".join(lines), "citation": f"Sheet {sheet}, rows {start}-{end}"})
    n = first_row
    for n, row in enumerate(rows, first_row):
        line = _row_line(cols, list(row))
        if not line:
            continue
        if lines and size + len(line) > ROW_CHARS:
            flush(n - 1)
            lines, start, size = [], n, len(lead)
        lines.append(line[:ROW_CHARS])
        size += len(line) + 1
    flush(n)
    return out


def _read_table(filename: str, content: bytes) -> List[Dict[str, Any]]:
    """[{"sheet", "header", "rows", "cut"}]: CSV/TSV is one sheet; a workbook has one per sheet (cached values, never formulas)."""
    suffix = "." + filename.rsplit(".", 1)[-1].lower()
    if suffix in (".csv", ".tsv"):
        reader = csv.reader(io.StringIO(content.decode("utf-8-sig", errors="replace")), delimiter="\t" if suffix == ".tsv" else ",")
        header = next(reader, [])
        rows = []
        for i, r in enumerate(reader):
            if i >= MAX_ROWS_PER_SHEET:
                return [{"sheet": filename, "header": header, "rows": rows, "cut": True}]
            rows.append(r)
        return [{"sheet": filename, "header": header, "rows": rows, "cut": False}]
    import pandas as pd
    try:
        frames = pd.read_excel(io.BytesIO(content), sheet_name=None, dtype=str, keep_default_na=False, nrows=MAX_ROWS_PER_SHEET + 1)
    except Exception as exc:
        raise ValueError(f"The workbook could not be read: {type(exc).__name__}.") from exc
    out = []
    for name, frame in frames.items():
        rows = frame.values.tolist()
        out.append({"sheet": str(name), "header": [str(c) for c in frame.columns], "rows": rows[:MAX_ROWS_PER_SHEET], "cut": len(rows) > MAX_ROWS_PER_SHEET})
    return out


def chunks_for_file(filename: str, content: bytes) -> Dict[str, Any]:
    """{"chunks": [{"text", "citation"}], "truncated": bool, "note": str} for an uploaded file. Raises ValueError for what cannot be read."""
    suffix = "." + str(filename).rsplit(".", 1)[-1].lower() if "." in str(filename) else ""
    if suffix not in SUPPORTED:
        raise ValueError("Supported files: Markdown, text, JSON, CSV/TSV, Excel, Word, PowerPoint and PDF.")
    if len(content) > MAX_BYTES:
        raise ValueError("Knowledge files are limited to 20 MB.")
    if not content:
        raise ValueError("The file is empty.")
    chunks: List[Dict[str, str]] = []
    note = ""
    cut = False
    if suffix in (".md", ".txt", ".yaml", ".yml"):
        chunks = chunk_text(content.decode("utf-8-sig", errors="replace"))
    elif suffix == ".json":
        try:
            chunks = chunk_text(json.dumps(json.loads(content.decode("utf-8-sig")), ensure_ascii=False, indent=2), "Part")
        except ValueError as exc:
            raise ValueError("The JSON file is not valid.") from exc
    elif suffix in TABLE_SUFFIXES:
        for sheet in _read_table(filename, content):
            chunks += chunk_rows(sheet["sheet"], sheet["header"], sheet["rows"])
            cut = cut or sheet["cut"]
        if cut:
            note = f"Only the first {MAX_ROWS_PER_SHEET} rows of a sheet are read."
    elif suffix == ".pdf":
        chunks = chunk_pages(document_text.pdf_pages(content))
    else:
        chunks = chunk_text(document_text.extract(suffix, content))
    if len(chunks) > MAX_CHUNKS:
        chunks, cut = chunks[:MAX_CHUNKS], True
        note = (note + " " if note else "") + f"Only the first {MAX_CHUNKS} chunks are kept."
    if not chunks:
        raise ValueError("The file has no readable text.")
    return {"chunks": chunks, "truncated": cut, "note": note}


def chunks_for_text(text: str, label: str = "Section") -> Dict[str, Any]:
    """The same for text already extracted (a web page, a Confluence page)."""
    chunks = chunk_text(text, label)[:MAX_CHUNKS]
    if not chunks:
        raise ValueError("There is no readable text.")
    return {"chunks": chunks, "truncated": False, "note": ""}


# ------------------------------------------------------------------------------------------------------------------------- the store
def _entities_in(text: str, names: Iterable[str]) -> str:
    low = text.lower()
    return ",".join(sorted(n for n in names if re.search(rf"\b{re.escape(n.lower())}s?\b", low)))


def index_asset(conn: sqlite3.Connection, *, migration_id: str, scope: str, asset_id: str, asset_name: str, source: str, source_ref: str,
                version: str, chunks: List[Dict[str, str]], entity_names: Iterable[str] = ()) -> int:
    """Store an asset's chunks, replacing any it had (a re-import or a new page version). Returns how many chunks were stored."""
    ensure_table(conn)
    if scope not in ("firm", "platform"):
        raise ValueError("scope must be firm or platform")
    conn.execute("DELETE FROM knowledge_chunks WHERE asset_id = ?", (asset_id,))
    now = datetime.now(timezone.utc).isoformat()
    names = list(entity_names)
    for seq, c in enumerate(chunks, 1):
        conn.execute("INSERT INTO knowledge_chunks (chunk_id, migration_id, scope, asset_id, asset_name, source, source_ref, asset_version, status, seq, text, citation, entities, created_utc) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?)",
                     ("KC-" + uuid.uuid4().hex[:12].upper(), migration_id, scope, asset_id, asset_name, source, source_ref, version, seq, c["text"], c.get("citation"),
                      _entities_in(c["text"], names) or None, now))
    return len(chunks)


def retire_asset(conn: sqlite3.Connection, asset_id: str) -> int:
    """An asset taken out of use: its chunks stop being found (and are no longer embedded or retrieved)."""
    ensure_table(conn)
    return conn.execute("UPDATE knowledge_chunks SET status = 'RETIRED' WHERE asset_id = ? AND status = 'ACTIVE'", (asset_id,)).rowcount


def stats(conn: sqlite3.Connection, migration_id: str) -> Dict[str, Dict[str, Any]]:
    """{asset_id: {"chunks", "chars", "version", "source"}} of the migration's active chunks. The sizes are added up here rather than in SQL, because
    the length function has a different name on SQL Server (LEN) than on SQLite and PostgreSQL (LENGTH)."""
    ensure_table(conn)
    out: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute("SELECT asset_id, text, asset_version, source FROM knowledge_chunks WHERE migration_id = ? AND status = 'ACTIVE'", (migration_id,)).fetchall():
        s = out.setdefault(r["asset_id"], {"chunks": 0, "chars": 0, "version": r["asset_version"], "source": r["source"]})
        s["chunks"] += 1
        s["chars"] += len(r["text"] or "")
        s["version"] = max(str(s["version"] or ""), str(r["asset_version"] or "")) or None
    return out


def chunks_of(conn: sqlite3.Connection, asset_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute("SELECT seq, text, citation, entities, asset_version FROM knowledge_chunks WHERE asset_id = ? AND status = 'ACTIVE' ORDER BY seq LIMIT ?",
                                          (asset_id, limit)).fetchall()]
