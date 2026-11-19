"""Teaching retrieval a firm's own vocabulary from reviewers' feedback, on the CPU, with nothing leaving the server.

What is tuned is small and honest: a linear map (an "adapter") applied to the query vector before it is compared with the frozen chunk
vectors, so "owner" can come to land near the chunks that explain "household" for this firm. The embedding model's own weights are not
changed. Training data is only what reviewers marked helpful or not helpful (a query and a chunk, never a source-system value),
kept per migration. Training is gradient descent on a contrastive loss over all of the migration's chunks, held close to the identity
so a thin training set cannot do much damage.

A tuned adapter is a candidate, not a change. It is evaluated on feedback held back from training (a fixed 20 per cent of the
queries) and compared with the untuned retrieval on the same held-out queries; it becomes the active adapter only when it needs at
least MIN_POSITIVE helpful verdicts to start, MIN_EVAL of them held out, and it finds the helpful chunk in the top three at least
MIN_GAIN (and at least two held-out queries) more often than the untuned retrieval. Every candidate is stored with the hash of the feedback it was trained on and its
scores, and the active one can be retired to go back to untuned retrieval at any time.
"""
import base64
import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

MIN_POSITIVE = 30
MIN_EVAL = 6
MIN_GAIN = 0.02
TOP_K = 3
EPOCHS = 200
LEARNING_RATE = 0.05
TEMPERATURE = 0.1
PULL_TO_IDENTITY = 1e-3
NEGATIVE_WEIGHT = 0.5

_DDL = """
CREATE TABLE IF NOT EXISTS retrieval_models (
    model_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, base_model TEXT NOT NULL, dim INTEGER NOT NULL, status TEXT NOT NULL, data_hash TEXT NOT NULL,
    n_train INTEGER NOT NULL, n_eval INTEGER NOT NULL, base_hit REAL NOT NULL, tuned_hit REAL NOT NULL, weights TEXT NOT NULL,
    created_by TEXT NOT NULL, created_utc TEXT NOT NULL, decided_utc TEXT
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_eval(query: str) -> bool:
    """A query is held out for evaluation or used for training, decided by its own hash so the split never changes between runs."""
    return int(hashlib.sha1(query.encode("utf-8")).hexdigest()[:8], 16) % 5 == 0


def _unit(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


def _dataset(conn: sqlite3.Connection, migration_id: str, embedder) -> Dict[str, Any]:
    from control_plane.services import knowledge_index as ki
    ki.ensure_tables(conn)
    ki.embed_pending(conn, embedder, migration_id)
    corpus = ki._corpus(conn, migration_id, embedder, None)
    index = {m["chunk_id"]: i for i, m in enumerate(corpus["meta"])}
    rows = conn.execute("SELECT query, chunk_id, verdict FROM knowledge_feedback WHERE migration_id = ? ORDER BY query, chunk_id, verdict, feedback_id", (migration_id,)).fetchall()
    pairs = [(r["query"], index[r["chunk_id"]], r["verdict"], r["chunk_id"]) for r in rows if r["chunk_id"] in index]
    queries = sorted({p[0] for p in pairs})
    qvec = dict(zip(queries, embedder.embed(queries))) if queries else {}
    digest = hashlib.sha256(("|".join(f"{q}\t{c}\t{v}" for q, _i, v, c in pairs) + "|" + embedder.name).encode("utf-8")).hexdigest()[:16]
    return {"C": corpus["matrix"], "pairs": pairs, "qvec": qvec, "hash": digest}


def _split(data: Dict[str, Any]) -> Dict[str, Any]:
    out = {"train_pos": [], "train_neg": [], "eval_pos": []}
    for q, i, v, _c in data["pairs"]:
        if _is_eval(q):
            if v == 1:
                out["eval_pos"].append((q, i))
        else:
            out["train_pos" if v == 1 else "train_neg"].append((q, i))
    return out


def readiness(conn: sqlite3.Connection, migration_id: str, embedder) -> Dict[str, Any]:
    data = _dataset(conn, migration_id, embedder)
    s = _split(data)
    positives = len(s["train_pos"]) + len(s["eval_pos"])
    ready = positives >= MIN_POSITIVE and len(s["eval_pos"]) >= MIN_EVAL and len(s["train_pos"]) > 0
    return {"positives": positives, "negatives": len(s["train_neg"]), "train_positives": len(s["train_pos"]), "eval_positives": len(s["eval_pos"]),
            "min_positive": MIN_POSITIVE, "min_eval": MIN_EVAL, "ready": ready, "data_hash": data["hash"]}


def _hit_rate(Q: np.ndarray, idx: List[int], C: np.ndarray, W: Optional[np.ndarray]) -> float:
    if not len(idx):
        return 0.0
    q = _unit(Q if W is None else Q @ W)
    top = np.argsort(-(q @ C.T), axis=1)[:, :TOP_K]
    return float(np.mean([i in row for i, row in zip(idx, top)]))


def _train(Q: np.ndarray, pos: List[int], Qn: np.ndarray, neg: List[int], C: np.ndarray, dim: int) -> np.ndarray:
    W = np.eye(dim, dtype=np.float64)
    I = W.copy()
    Cd = C.astype(np.float64)
    Qd, Qnd = Q.astype(np.float64), Qn.astype(np.float64)
    m, v = np.zeros_like(W), np.zeros_like(W)
    onehot = np.zeros((len(pos), len(Cd)))
    onehot[np.arange(len(pos)), pos] = 1.0
    for step in range(1, EPOCHS + 1):
        u = Qd @ W
        nu = np.linalg.norm(u, axis=1, keepdims=True)
        nu[nu == 0] = 1.0
        qn = u / nu
        logits = qn @ Cd.T / TEMPERATURE
        logits -= logits.max(axis=1, keepdims=True)
        p = np.exp(logits)
        p /= p.sum(axis=1, keepdims=True)
        dqn = ((p - onehot) / len(pos)) @ Cd / TEMPERATURE
        du = (dqn - qn * (qn * dqn).sum(axis=1, keepdims=True)) / nu
        grad = Qd.T @ du + 2 * PULL_TO_IDENTITY * (W - I)
        if len(neg):  # a chunk marked not helpful is pushed away from its query
            un = Qnd @ W
            nn = np.linalg.norm(un, axis=1, keepdims=True)
            nn[nn == 0] = 1.0
            qnn = un / nn
            dqn_n = NEGATIVE_WEIGHT * Cd[[i for i in neg]] / len(neg)
            du_n = (dqn_n - qnn * (qnn * dqn_n).sum(axis=1, keepdims=True)) / nn
            grad += Qnd.T @ du_n
        m = 0.9 * m + 0.1 * grad
        v = 0.999 * v + 0.001 * grad * grad
        W -= LEARNING_RATE * (m / (1 - 0.9 ** step)) / (np.sqrt(v / (1 - 0.999 ** step)) + 1e-8)
    return W.astype(np.float32)


def tune(conn: sqlite3.Connection, migration_id: str, actor: str, embedder) -> Dict[str, Any]:
    """Train a candidate adapter from this migration's feedback, evaluate it on held-out queries, and activate it only if it beats untuned retrieval."""
    ensure_table(conn)
    data = _dataset(conn, migration_id, embedder)
    s = _split(data)
    ready = readiness(conn, migration_id, embedder)
    if not ready["ready"]:
        raise ValueError(f"Not enough feedback to tune: {ready['positives']} helpful verdict(s) ({MIN_POSITIVE} needed, {ready['eval_positives']} of them held out; "
                         f"{MIN_EVAL} needed). Mark more retrieved evidence helpful or not helpful first.")
    C, qvec = data["C"], data["qvec"]
    Qt = np.vstack([qvec[q] for q, _i in s["train_pos"]])
    Qnt = np.vstack([qvec[q] for q, _i in s["train_neg"]]) if s["train_neg"] else np.zeros((0, embedder.dim), dtype=np.float32)
    Qe = np.vstack([qvec[q] for q, _i in s["eval_pos"]])
    eval_idx = [i for _q, i in s["eval_pos"]]
    W = _train(Qt, [i for _q, i in s["train_pos"]], Qnt, [i for _q, i in s["train_neg"]], C, embedder.dim)
    base_hit, tuned_hit = _hit_rate(Qe, eval_idx, C, None), _hit_rate(Qe, eval_idx, C, W)
    needed = max(MIN_GAIN, 2.0 / len(eval_idx))  # at least two more held-out queries found, so one lucky hit on a small set cannot promote it
    wins = tuned_hit >= base_hit + needed
    model_id = "RM-" + uuid.uuid4().hex[:10].upper()
    now = _now()
    if wins:
        conn.execute("UPDATE retrieval_models SET status = 'RETIRED', decided_utc = ? WHERE migration_id = ? AND base_model = ? AND status = 'ACTIVE'", (now, migration_id, embedder.name))
    conn.execute("INSERT INTO retrieval_models (model_id, migration_id, base_model, dim, status, data_hash, n_train, n_eval, base_hit, tuned_hit, weights, created_by, created_utc, decided_utc) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (model_id, migration_id, embedder.name, embedder.dim, "ACTIVE" if wins else "REJECTED", data["hash"], len(s["train_pos"]), len(s["eval_pos"]),
                  base_hit, tuned_hit, base64.b64encode(W.tobytes()).decode("ascii"), actor, now, now))
    return {"model_id": model_id, "status": "ACTIVE" if wins else "REJECTED", "base_hit": base_hit, "tuned_hit": tuned_hit, "n_train": len(s["train_pos"]),
            "n_eval": len(s["eval_pos"]), "data_hash": data["hash"], "min_gain": needed}


def active_adapter(conn: sqlite3.Connection, migration_id: str, base_model: str) -> Optional[np.ndarray]:
    ensure_table(conn)
    row = conn.execute("SELECT weights, dim FROM retrieval_models WHERE migration_id = ? AND base_model = ? AND status = 'ACTIVE' ORDER BY created_utc DESC LIMIT 1",
                       (migration_id, base_model)).fetchone()
    if not row:
        return None
    w = np.frombuffer(base64.b64decode(row["weights"]), dtype=np.float32)
    return w.reshape(row["dim"], row["dim"]) if w.size == row["dim"] ** 2 else None


def list_models(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute("SELECT model_id, base_model, status, data_hash, n_train, n_eval, base_hit, tuned_hit, created_by, created_utc, decided_utc "
                                          "FROM retrieval_models WHERE migration_id = ? ORDER BY created_utc DESC", (migration_id,)).fetchall()]


def retire(conn: sqlite3.Connection, migration_id: str, model_id: str) -> None:
    """Go back to untuned retrieval."""
    ensure_table(conn)
    if conn.execute("UPDATE retrieval_models SET status = 'RETIRED', decided_utc = ? WHERE model_id = ? AND migration_id = ? AND status = 'ACTIVE'",
                    (_now(), model_id, migration_id)).rowcount == 0:
        raise ValueError("That is not the active tuned retrieval of this migration.")
