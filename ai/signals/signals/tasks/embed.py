"""Topic: ``intfloat/multilingual-e5-small`` embeddings and nearest-example votes.

Every text -- a query or an example -- is embedded as ``"query: " + text``
(e5's convention for symmetric tasks such as kNN), truncated to its first
``max_tokens`` tokens, mean-pooled over the attention mask and
L2-normalised, so a dot product is a cosine similarity.

``nearest`` turns one query vector and an embedded example set into the
features route-policy's ``nearest_examples`` evaluator reads: the top-k
neighbours (example ids, routes and similarities -- never example text) and,
per route, its **share** of the top-k similarity mass, its best similarity
and its neighbour count. The share is what the evaluator thresholds: e5's
similarities are compressed (a bare "ok" still reaches 0.82), the share of
the winning route separates a real match from noise (plan § 2.2).
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Generator, List, Sequence

from .. import config

QUERY_PREFIX = "query: "
WARM_UP_TEXT = "Hello, this is a short warm-up sentence for the signals sidecar."


def nearest(vectors: Any, ids: Sequence[str], routes: Sequence[str], query: Any, k: int) -> Dict[str, Any]:
    """Top-``k`` neighbours of ``query`` in an example set, and each route's share.

    ``vectors`` is [n, dim] and ``query`` [dim], both L2-normalised. Shares
    use the similarities clipped at 0 (a negative cosine is no evidence) and
    sum to 1 over the routes present in the top k; ``routes`` is ordered by
    share, then best similarity.
    """
    import numpy as np

    n = 0 if vectors is None else int(vectors.shape[0])
    if n == 0:
        return {"neighbours": [], "routes": {}}
    sims = np.asarray(vectors, dtype=np.float32) @ np.asarray(query, dtype=np.float32)
    k = max(1, min(int(k), n))
    order = np.argsort(-sims, kind="stable")[:k]
    neighbours = [{"id": ids[j], "route": routes[j], "similarity": round(float(sims[j]), 4)} for j in order]
    mass: Dict[str, float] = {}
    best: Dict[str, float] = {}
    count: Dict[str, int] = {}
    for j in order:
        route, sim = routes[j], float(sims[j])
        mass[route] = mass.get(route, 0.0) + max(0.0, sim)
        best[route] = max(best.get(route, -1.0), sim)
        count[route] = count.get(route, 0) + 1
    total = sum(mass.values())
    shares = {route: (mass[route] / total if total > 0 else 0.0) for route in mass}
    ordered = sorted(mass, key=lambda r: (-shares[r], -best[r], r))
    return {
        "neighbours": neighbours,
        "routes": {r: {"share": round(shares[r], 4), "best": round(best[r], 4), "count": count[r]} for r in ordered},
    }


class Embedder:
    """The e5 session and tokenizer. Used by one lane worker thread at a time."""

    lane = "embed"

    def __init__(self, session: Any, tokenizer: Any, *, max_tokens: int) -> None:
        from ..runtime import input_names

        self.session = session
        self.tokenizer = tokenizer
        self.max_tokens = int(max_tokens)
        self._inputs = input_names(session)
        self._truncation = None
        pad_id = tokenizer.token_to_id("<pad>")
        tokenizer.enable_padding(pad_id=1 if pad_id is None else pad_id, pad_token="<pad>")

    @classmethod
    def load(cls, root: pathlib.Path, spec: Any, threads: int) -> "Embedder":
        from tokenizers import Tokenizer

        from ..runtime import ort_session

        return cls(ort_session(root / spec.model, threads), Tokenizer.from_file(str(root / "tokenizer.json")),
                   max_tokens=spec.tokens["max"])

    def _truncate(self, max_tokens: int) -> None:
        max_tokens = max(8, min(int(max_tokens), self.max_tokens))
        if self._truncation != max_tokens:
            self.tokenizer.enable_truncation(max_tokens)
            self._truncation = max_tokens

    def embed(self, texts: Sequence[str], max_tokens: int) -> Any:
        """[len(texts), dim] float32, L2-normalised. One onnxruntime call; a
        batch is padded to its longest text and pooled over the mask."""
        import numpy as np

        from ..runtime import feed

        self._truncate(max_tokens)
        encodings = self.tokenizer.encode_batch([QUERY_PREFIX + t for t in texts])
        ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
        hidden = self.session.run(None, feed(self._inputs, ids, mask))[0]
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(1) / np.maximum(weights.sum(1), 1.0)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.maximum(norms, 1e-12)).astype(np.float32)

    def scan(self, text: str, max_tokens: int) -> Generator[None, None, Any]:
        """One text -> its query vector [dim]: one step, nothing to yield
        (``yield from ()`` only makes it a generator, like every scan)."""
        yield from ()
        return self.embed([text], max_tokens)[0]

    def embed_set(self, texts: Sequence[str], batch: int = config.EMBED_BATCH) -> Generator[None, None, Any]:
        """An example set -> [n, dim], one batch per step. Examples are
        embedded at the model's full window: they are short, and a set's
        vectors must not depend on one request's max_tokens."""
        import numpy as np

        parts: List[Any] = []
        for i in range(0, len(texts), max(1, batch)):
            parts.append(self.embed(texts[i:i + batch], self.max_tokens))
            yield None
        if not parts:
            return np.zeros((0, 0), dtype=np.float32)
        return np.concatenate(parts, axis=0)

    def warm_up(self) -> None:
        vector = self.embed([WARM_UP_TEXT], 64)
        if vector.ndim != 2 or vector.shape[0] != 1 or vector.shape[1] < 8:
            raise RuntimeError(f"embed: output shape {vector.shape}, expected (1, dim)")
