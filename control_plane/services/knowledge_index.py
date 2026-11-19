"""Vectors for the knowledge chunks, retrieval with citations, and reviewers' feedback on what was retrieved.

Every active chunk gets one vector per embedder (the embedder's name is stored with it, so a changed embedder re-embeds instead of mixing).
Retrieval takes a short query (for example "Account field AccountNumber, canonical ACCT_NUM"), finds the closest chunks of the migration
and of the platform knowledge, and returns them with their citations. Retrieved text is evidence for a person to read, never an
instruction: nothing in a chunk is executed or followed, and every suggestion it supports still goes through the normal approval.

A reviewer can mark a retrieved chunk helpful or not helpful. Those verdicts are the training set of `retrieval_tuning`; they are
kept per migration (they are that firm's knowledge) and name the chunk, never any source-system value.

Vectors are stored as base64 text so the same tables work on SQLite, PostgreSQL and SQL Server; the search is a matrix product in numpy,
fine for tens of thousands of chunks. A larger corpus is the point to move to pgvector or the SQL vector type.
"""
import base64
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

_DDL = (
    "CREATE TABLE IF NOT EXISTS knowledge_vectors (chunk_id TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec TEXT NOT NULL, created_utc TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS knowledge_feedback (
        feedback_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, subject TEXT NOT NULL, query TEXT NOT NULL, chunk_id TEXT NOT NULL,
        verdict INTEGER NOT NULL, actor TEXT NOT NULL, created_utc TEXT NOT NULL)""",
)
SNIPPET = 260
MAX_QUERY = 400


def ensure_tables(conn: sqlite3.Connection) -> None:
    from control_plane.services import knowledge_chunks
    knowledge_chunks.ensure_table(conn)
    for ddl in _DDL:
        conn.execute(ddl)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")


def decode(text: str, dim: int) -> np.ndarray:
    v = np.frombuffer(base64.b64decode(text), dtype=np.float32)
    if v.shape[0] != dim:
        raise ValueError("A stored vector does not have the size it was saved with.")
    return v


def embed_pending(conn: sqlite3.Connection, embedder, migration_id: Optional[str] = None, batch: int = 64) -> int:
    """Embed the active chunks that have no vector from this embedder (new chunks, or all of them after the embedder changed). Returns how many."""
    ensure_tables(conn)
    sql = ("SELECT c.chunk_id, c.text FROM knowledge_chunks AS c LEFT JOIN knowledge_vectors AS v ON v.chunk_id = c.chunk_id "
           "WHERE c.status = 'ACTIVE' AND (v.chunk_id IS NULL OR v.model <> ?)")
    args: List[Any] = [embedder.name]
    if migration_id:
        sql += " AND (c.migration_id = ? OR c.scope = 'platform')"
        args.append(migration_id)
    rows = conn.execute(sql, args).fetchall()
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vectors = embedder.embed([r["text"] for r in part])
        for r, vec in zip(part, vectors):
            conn.execute("DELETE FROM knowledge_vectors WHERE chunk_id = ?", (r["chunk_id"],))
            conn.execute("INSERT INTO knowledge_vectors (chunk_id, model, dim, vec, created_utc) VALUES (?, ?, ?, ?, ?)",
                         (r["chunk_id"], embedder.name, embedder.dim, encode(vec), _now()))
    return len(rows)


def _corpus(conn: sqlite3.Connection, migration_id: str, embedder, entity: Optional[str]) -> Dict[str, Any]:
    rows = conn.execute("SELECT c.chunk_id, c.asset_id, c.asset_name, c.source, c.source_ref, c.citation, c.text, c.entities, c.scope, v.vec, v.dim "
                        "FROM knowledge_chunks AS c JOIN knowledge_vectors AS v ON v.chunk_id = c.chunk_id "
                        "WHERE c.status = 'ACTIVE' AND v.model = ? AND (c.migration_id = ? OR c.scope = 'platform') ORDER BY c.chunk_id",
                        (embedder.name, migration_id)).fetchall()
    if entity:
        # Chunks that name the entity come first in the candidate set; chunks that name no entity at all stay (a general rule applies to every entity).
        rows = [r for r in rows if not r["entities"] or entity in str(r["entities"]).split(",")]
    meta = [dict(r) for r in rows]
    matrix = np.vstack([decode(m.pop("vec"), m.pop("dim")) for m in meta]) if meta else np.zeros((0, embedder.dim), dtype=np.float32)
    return {"meta": meta, "matrix": matrix}


def _norm(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def retrieve(conn: sqlite3.Connection, migration_id: str, queries: Sequence[str], embedder, k: int = 3, entity: Optional[str] = None,
             min_score: float = 0.05) -> List[List[Dict[str, Any]]]:
    """For each query the closest chunks (best first) as {chunk_id, asset_name, citation, source, source_ref, snippet, score}. A migration's reviewed adapter,
    when there is one, is applied to the query vector first (see retrieval_tuning)."""
    from control_plane.services import retrieval_tuning
    ensure_tables(conn)
    embed_pending(conn, embedder, migration_id)
    corpus = _corpus(conn, migration_id, embedder, entity)
    if not len(corpus["meta"]) or not queries:
        return [[] for _ in queries]
    q = embedder.embed([str(x)[:MAX_QUERY] for x in queries])
    adapter = retrieval_tuning.active_adapter(conn, migration_id, embedder.name)
    if adapter is not None:
        q = _norm(q @ adapter)
    scores = q @ corpus["matrix"].T
    out: List[List[Dict[str, Any]]] = []
    for row in scores:
        top = np.argsort(-row)[:k]
        hits = []
        for i in top:
            if row[i] < min_score:
                continue
            m = corpus["meta"][int(i)]
            text = " ".join(str(m["text"]).split())
            hits.append({"chunk_id": m["chunk_id"], "asset_id": m["asset_id"], "asset_name": m["asset_name"], "citation": m["citation"], "source": m["source"],
                         "source_ref": m["source_ref"], "scope": m["scope"], "snippet": text[:SNIPPET] + ("…" if len(text) > SNIPPET else ""), "score": round(float(row[i]), 3)})
        out.append(hits)
    return out


# ---------------------------------------------------------------------------------------------------------------------- feedback
def record_feedback(conn: sqlite3.Connection, migration_id: str, subject: str, query: str, chunk_id: str, verdict: int, actor: str) -> str:
    """A reviewer's verdict (+1 helpful, -1 not helpful) on a retrieved chunk for a query. One verdict per person, query and chunk: a new one replaces it."""
    ensure_tables(conn)
    if verdict not in (1, -1):
        raise ValueError("The verdict is helpful or not helpful.")
    query = " ".join(str(query or "").split())[:MAX_QUERY]
    if not query or not str(subject or "").strip():
        raise ValueError("There is nothing to give feedback on.")
    chunk = conn.execute("SELECT migration_id, scope, status FROM knowledge_chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
    if not chunk or chunk["status"] != "ACTIVE" or (chunk["scope"] != "platform" and chunk["migration_id"] != migration_id):
        raise ValueError("That evidence is not part of this migration's knowledge.")
    conn.execute("DELETE FROM knowledge_feedback WHERE migration_id = ? AND query = ? AND chunk_id = ? AND actor = ?", (migration_id, query, chunk_id, actor))
    fid = "KF-" + uuid.uuid4().hex[:10].upper()
    conn.execute("INSERT INTO knowledge_feedback (feedback_id, migration_id, subject, query, chunk_id, verdict, actor, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (fid, migration_id, str(subject)[:200], query, chunk_id, verdict, actor, _now()))
    return fid


def purge_migration(conn: sqlite3.Connection, migration_id: str) -> Dict[str, int]:
    """Delete everything search keeps for one migration: its chunks, their vectors, the feedback reviewers gave and the tuned retrieval trained on it.
    Platform knowledge is not touched. The documents stay registered but are no longer searchable; they cannot be re-indexed from here."""
    from control_plane.services import retrieval_tuning
    ensure_tables(conn)
    retrieval_tuning.ensure_table(conn)
    ids = [r["chunk_id"] for r in conn.execute("SELECT chunk_id FROM knowledge_chunks WHERE migration_id = ? AND scope = 'firm'", (migration_id,)).fetchall()]
    for i in range(0, len(ids), 200):
        part = ids[i:i + 200]
        conn.execute(f"DELETE FROM knowledge_vectors WHERE chunk_id IN ({','.join('?' * len(part))})", part)
    chunks = conn.execute("DELETE FROM knowledge_chunks WHERE migration_id = ? AND scope = 'firm'", (migration_id,)).rowcount
    feedback = conn.execute("DELETE FROM knowledge_feedback WHERE migration_id = ?", (migration_id,)).rowcount
    models = conn.execute("DELETE FROM retrieval_models WHERE migration_id = ?", (migration_id,)).rowcount
    from control_plane.services import decision_model
    decision_model.ensure_table(conn)
    models += conn.execute("DELETE FROM decision_models WHERE migration_id = ?", (migration_id,)).rowcount
    return {"chunks": chunks, "feedback": feedback, "models": models}


def feedback_counts(conn: sqlite3.Connection, migration_id: str) -> Dict[str, int]:
    ensure_tables(conn)
    rows = conn.execute("SELECT verdict, COUNT(*) AS n FROM knowledge_feedback AS f WHERE migration_id = ? AND EXISTS "
                        "(SELECT 1 FROM knowledge_chunks AS c WHERE c.chunk_id = f.chunk_id AND c.status = 'ACTIVE') GROUP BY verdict", (migration_id,)).fetchall()
    by = {r["verdict"]: r["n"] for r in rows}
    return {"helpful": by.get(1, 0), "not_helpful": by.get(-1, 0)}
