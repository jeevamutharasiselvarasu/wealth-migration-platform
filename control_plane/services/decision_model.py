"""A small model of this migration's own mapping decisions: does a pending suggestion look like the ones reviewers approved?

It is a logistic regression on the CPU, trained in numpy from the mapping suggestions this migration's reviewers approved or
rejected (a text embedding of "entity, source field, canonical field" plus the agent's own confidence). It is advisory and nothing else:
it never approves, rejects or changes a mapping, and a reviewer sees it as "past decisions suggest: likely approved (82%)" beside the
suggestion. It is per migration (the decisions are that firm's), and it is used only if it clearly beats the obvious baseline.

Like the retrieval adapter it is a candidate until it has been checked on decisions held back from training (a fixed 20 per cent of
the suggestions, chosen by hash): it needs MIN_DECISIONS decisions with at least MIN_PER_CLASS of each kind and MIN_EVAL held out, and
its accuracy on the held-out ones must beat the best single threshold on the agent's confidence by MIN_GAIN, and by at least two
suggestions. Every candidate is stored with the hash of the decisions it saw and its scores; the active one can be retired.
"""
import base64
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

MIN_DECISIONS = 60
MIN_PER_CLASS = 15
MIN_EVAL = 15
MIN_GAIN = 0.05
STEPS = 400
LEARNING_RATE = 0.1
L2 = 1e-2

_DDL = """
CREATE TABLE IF NOT EXISTS decision_models (
    model_id TEXT PRIMARY KEY, migration_id TEXT NOT NULL, kind TEXT NOT NULL, base_model TEXT NOT NULL, status TEXT NOT NULL, data_hash TEXT NOT NULL,
    n_train INTEGER NOT NULL, n_eval INTEGER NOT NULL, base_acc REAL NOT NULL, tuned_acc REAL NOT NULL, params TEXT NOT NULL,
    created_by TEXT NOT NULL, created_utc TEXT NOT NULL, decided_utc TEXT
)
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(entity: Any, source: Any, canonical: Any) -> str:
    return f"{entity} {source} canonical {canonical or 'unresolved'}"


def _is_eval(key: str) -> bool:
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) % 5 == 0


def _decided(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    """The latest approved or rejected decision for each (entity, source field, canonical field)."""
    rows = conn.execute("SELECT entity_name, source_field, canonical_field, confidence_score, status, COALESCE(reviewed_utc, created_utc) AS decided FROM mapping_suggestions "
                        "WHERE migration_id = ? AND status IN ('APPROVED', 'REJECTED') ORDER BY decided", (migration_id,)).fetchall()
    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in rows:
        latest[(r["entity_name"], r["source_field"], str(r["canonical_field"] or ""))] = dict(r)
    return list(latest.values())


def _features(embedder, rows: Sequence[Dict[str, Any]], mu: float, sd: float) -> np.ndarray:
    emb = embedder.embed([_text(r["entity_name"], r["source_field"], r.get("canonical_field")) for r in rows]) if rows else np.zeros((0, embedder.dim), dtype=np.float32)
    conf = np.array([[(float(r.get("confidence_score") or 0) - mu) / sd] for r in rows], dtype=np.float32)
    return np.hstack([emb, conf, np.ones((len(rows), 1), dtype=np.float32)])


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    w = np.zeros(X.shape[1], dtype=np.float64)
    m, v = np.zeros_like(w), np.zeros_like(w)
    Xd, yd = X.astype(np.float64), y.astype(np.float64)
    for step in range(1, STEPS + 1):
        grad = Xd.T @ (_sigmoid(Xd @ w) - yd) / len(yd) + L2 * np.r_[w[:-1], 0.0]
        m = 0.9 * m + 0.1 * grad
        v = 0.999 * v + 0.001 * grad * grad
        w -= LEARNING_RATE * (m / (1 - 0.9 ** step)) / (np.sqrt(v / (1 - 0.999 ** step)) + 1e-8)
    return w.astype(np.float32)


def _baseline(train: Sequence[Dict[str, Any]], held: Sequence[Dict[str, Any]]) -> float:
    """Accuracy of the best single threshold on the agent's confidence (chosen on the training decisions), measured on the held-out ones."""
    cs = sorted({float(r["confidence_score"] or 0) for r in train}) or [0.0]
    cands = [-1.0] + [(a + b) / 2 for a, b in zip(cs, cs[1:])] + [2.0]
    ys = [r["status"] == "APPROVED" for r in train]
    best = max(cands, key=lambda c: sum((float(r["confidence_score"] or 0) >= c) == y for r, y in zip(train, ys)))
    return float(np.mean([(float(r["confidence_score"] or 0) >= best) == (r["status"] == "APPROVED") for r in held])) if held else 0.0


def _split(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    train, held = [], []
    for r in rows:
        (held if _is_eval(f"{r['entity_name']}|{r['source_field']}|{r['canonical_field']}") else train).append(r)
    return train, held


def readiness(conn: sqlite3.Connection, migration_id: str) -> Dict[str, Any]:
    rows = _decided(conn, migration_id)
    train, held = _split(rows)
    approved = sum(r["status"] == "APPROVED" for r in rows)
    rejected = len(rows) - approved
    ready = len(rows) >= MIN_DECISIONS and min(approved, rejected) >= MIN_PER_CLASS and len(held) >= MIN_EVAL
    return {"decisions": len(rows), "approved": approved, "rejected": rejected, "eval": len(held), "min_decisions": MIN_DECISIONS, "min_per_class": MIN_PER_CLASS,
            "min_eval": MIN_EVAL, "ready": ready}


def train(conn: sqlite3.Connection, migration_id: str, actor: str, embedder) -> Dict[str, Any]:
    ensure_table(conn)
    r = readiness(conn, migration_id)
    if not r["ready"]:
        raise ValueError(f"Not enough decisions to learn from: {r['decisions']} ({r['approved']} approved, {r['rejected']} rejected, {r['eval']} held out). "
                         f"Needs {MIN_DECISIONS}, at least {MIN_PER_CLASS} of each kind and {MIN_EVAL} held out.")
    rows = _decided(conn, migration_id)
    tr, held = _split(rows)
    confs = np.array([float(x["confidence_score"] or 0) for x in tr])
    mu, sd = float(confs.mean()), float(confs.std()) or 1.0
    w = _fit(_features(embedder, tr, mu, sd), np.array([x["status"] == "APPROVED" for x in tr], dtype=np.float32))
    pred = _sigmoid(_features(embedder, held, mu, sd) @ w) >= 0.5
    tuned = float(np.mean(pred == np.array([x["status"] == "APPROVED" for x in held])))
    base = _baseline(tr, held)
    needed = max(MIN_GAIN, 2.0 / len(held))
    wins = tuned >= base + needed
    digest = hashlib.sha256(("|".join(f"{x['entity_name']}\t{x['source_field']}\t{x['canonical_field']}\t{x['status']}" for x in rows) + embedder.name).encode("utf-8")).hexdigest()[:16]
    mid, now = "DM-" + uuid.uuid4().hex[:10].upper(), _now()
    if wins:
        conn.execute("UPDATE decision_models SET status = 'RETIRED', decided_utc = ? WHERE migration_id = ? AND kind = 'MAPPING' AND status = 'ACTIVE'", (now, migration_id))
    conn.execute("INSERT INTO decision_models (model_id, migration_id, kind, base_model, status, data_hash, n_train, n_eval, base_acc, tuned_acc, params, created_by, created_utc, decided_utc) "
                 "VALUES (?, ?, 'MAPPING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (mid, migration_id, embedder.name, "ACTIVE" if wins else "REJECTED", digest, len(tr), len(held), base, tuned,
                  json.dumps({"mu": mu, "sd": sd, "w": base64.b64encode(w.tobytes()).decode("ascii")}), actor, now, now))
    return {"model_id": mid, "status": "ACTIVE" if wins else "REJECTED", "base_acc": base, "tuned_acc": tuned, "n_train": len(tr), "n_eval": len(held), "min_gain": needed, "data_hash": digest}


def _active(conn: sqlite3.Connection, migration_id: str, embedder) -> Optional[Dict[str, Any]]:
    ensure_table(conn)
    row = conn.execute("SELECT params FROM decision_models WHERE migration_id = ? AND kind = 'MAPPING' AND base_model = ? AND status = 'ACTIVE' ORDER BY created_utc DESC LIMIT 1",
                       (migration_id, embedder.name)).fetchone()
    if not row:
        return None
    p = json.loads(row["params"])
    w = np.frombuffer(base64.b64decode(p["w"]), dtype=np.float32)
    return {"mu": p["mu"], "sd": p["sd"], "w": w} if w.size == embedder.dim + 2 else None


def likelihood(conn: sqlite3.Connection, migration_id: str, embedder, suggestions: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """{'Entity|SOURCE_FIELD': probability the suggestion is approved} for each pending suggestion, or {} when no model has earned its place."""
    model = _active(conn, migration_id, embedder)
    if not model or not suggestions:
        return {}
    probs = _sigmoid(_features(embedder, list(suggestions), model["mu"], model["sd"]) @ model["w"])
    return {f"{s['entity_name']}|{str(s['source_field']).upper()}": round(float(p), 2) for s, p in zip(suggestions, probs)}


def list_models(conn: sqlite3.Connection, migration_id: str) -> List[Dict[str, Any]]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute("SELECT model_id, kind, base_model, status, data_hash, n_train, n_eval, base_acc, tuned_acc, created_by, created_utc FROM decision_models "
                                          "WHERE migration_id = ? ORDER BY created_utc DESC", (migration_id,)).fetchall()]


def retire(conn: sqlite3.Connection, migration_id: str, model_id: str) -> None:
    ensure_table(conn)
    if conn.execute("UPDATE decision_models SET status = 'RETIRED', decided_utc = ? WHERE model_id = ? AND migration_id = ? AND status = 'ACTIVE'", (_now(), model_id, migration_id)).rowcount == 0:
        raise ValueError("That is not the active decision model of this migration.")
