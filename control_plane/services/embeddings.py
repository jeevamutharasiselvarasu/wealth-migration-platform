"""Text to vectors, on this server: no text leaves the host.

Two embedders sit behind one small interface (`name`, `dim`, `embed(texts) -> unit-length float32 rows`):

* `HashingEmbedder` (always available, no download, no model): words and word pairs hashed into a fixed number of buckets, weighted by
  how often they occur, then normalised. Two texts that share vocabulary get similar vectors; it does not know that "owner" and
  "household" mean nearly the same thing. It is the baseline that works offline and in tests, and the learned adapter in
  `retrieval_tuning` is what teaches it the firm's own vocabulary.
* `FastEmbedder` (needs the `fastembed` package and a small open model such as BAAI/bge-small-en-v1.5, run with ONNX on the CPU):
  a real semantic model. It is used only when the package is installed and a model is named, so nothing is downloaded behind anyone's
  back. For a container image the model files are fetched once at build time (see the Dockerfile note), so the running container
  needs no internet access.

`get_embedder()` picks one from the environment: XODUS_EMBEDDER = auto (default: fastembed when it and XODUS_EMBEDDING_MODEL are
present, otherwise hashing), hashing, or fastembed. The embedder's name is stored with every vector, so changing it re-embeds
everything rather than mixing two kinds of vector.
"""
import hashlib
import math
import os
import re
from typing import List, Optional

import numpy as np

HASH_DIM = 384
_WORD = re.compile(r"[A-Za-z]+|\d+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


class EmbedderUnavailable(RuntimeError):
    pass


def _tokens(text: str) -> List[str]:
    """Lower-case words; ACCT_NUM gives acct, num and AccountNumber gives account, number, so a column name matches the prose that explains it."""
    out: List[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", str(text or "")):
        if not chunk:
            continue
        parts = _CAMEL.findall(chunk) or [chunk]
        out += [p.lower() for p in parts]
    return [t for t in out if len(t) > 1 or t.isdigit()]


def _bucket(token: str, dim: int) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "big") % dim


class HashingEmbedder:
    def __init__(self, dim: int = HASH_DIM) -> None:
        self.dim = dim
        self.name = f"hashing-v1-{dim}"

    def embed(self, texts: List[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            toks = _tokens(text)
            counts = {}
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
            for a, b in zip(toks, toks[1:]):  # word pairs carry a little order
                counts[a + " " + b] = counts.get(a + " " + b, 0) + 0.5
            for token, n in counts.items():
                out[row, _bucket(token, self.dim)] += 1.0 + math.log(n) if n >= 1 else n
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


class FastEmbedder:
    def __init__(self, model: str) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbedderUnavailable("The fastembed package is not installed (pip install fastembed).") from exc
        try:
            self._model = TextEmbedding(model_name=model)
        except Exception as exc:  # a model that is not on disk and cannot be downloaded
            raise EmbedderUnavailable(f"The embedding model {model} could not be loaded: {type(exc).__name__}.") from exc
        self.name = f"fastembed:{model}"
        self.dim = int(np.asarray(next(iter(self._model.embed(["dimension probe"])))).shape[0])

    def embed(self, texts: List[str]) -> np.ndarray:
        rows = np.asarray(list(self._model.embed(list(texts))), dtype=np.float32).reshape(len(texts), -1)
        norms = np.linalg.norm(rows, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return rows / norms


_cached: Optional[object] = None


def get_embedder(refresh: bool = False):
    """The embedder this server uses (see the module note). Falls back to hashing when a semantic model was asked for but cannot be loaded."""
    global _cached
    if _cached is not None and not refresh:
        return _cached
    choice = os.environ.get("XODUS_EMBEDDER", "auto").strip().lower()
    model = os.environ.get("XODUS_EMBEDDING_MODEL", "").strip()
    embedder = None
    if choice in ("auto", "fastembed") and model:
        try:
            embedder = FastEmbedder(model)
        except EmbedderUnavailable:
            if choice == "fastembed":
                raise
    if embedder is None:
        embedder = HashingEmbedder()
    _cached = embedder
    return embedder
