"""The sidecar's only state: features by text hash, and embedded example sets.

``FeatureCache`` is an in-memory LRU of ``sha256(task, model, params,
text) -> features``. The key is a digest, so the text itself is never kept;
the value is what a task computed from it -- an entity summary (types,
counts, scores), an injection score, a query embedding -- never a span or
an offset. It is what makes a long history cheap: every message route-policy
re-sends on the next turn is a hit, and the PII job that outlived a
response (scheduler.py) fills it for exactly that next call.

``ExampleSets`` keeps the embedded example sets of the nearest task, keyed
by the caller's fingerprint (route-policy's ``common.signals.
fingerprint_examples`` over the examples and its pinned model id), at most
``config.EXAMPLE_SETS_MAX`` of them. Each set also remembers the digest of
the items it was embedded from (the same function over the items and the
model id this copy runs), so one fingerprint sent with different items is
embedded again rather than answered from the wrong set. A set the sidecar
has not seen is embedded in the background on the embed lane; until it is
ready the task answers ``warming``.

Both are thread-safe: the event loop reads them, the lane workers write.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

WARMING = "warming"
READY = "ready"


def text_key(*parts: str) -> bytes:
    """sha256 over ``parts`` joined by NUL: the cache key of one text's
    features. The parts are the task, the model id, the parameters that
    change the result, and the text last."""
    digest = hashlib.sha256()
    for i, part in enumerate(parts):
        if i:
            digest.update(b"\0")
        digest.update(part.encode("utf-8", "surrogatepass"))
    return digest.digest()


class FeatureCache:
    """A bounded LRU of digest -> features. ``max_entries`` 0 keeps nothing."""

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max(0, int(max_entries))
        self._data: "OrderedDict[bytes, Any]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: bytes) -> Optional[Any]:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: bytes, value: Any) -> None:
        if self.max_entries == 0 or value is None:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def __contains__(self, key: bytes) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


@dataclass
class ExampleSet:
    """One embedded example set. ``vectors`` is [n, dim] float32,
    L2-normalised, row i the embedding of example ``ids[i]``."""

    fingerprint: str
    digest: str
    state: str = WARMING
    ids: List[str] = field(default_factory=list)
    routes: List[str] = field(default_factory=list)
    vectors: Any = None


class ExampleSets:
    """At most ``max_sets`` example sets by fingerprint, least recently used
    evicted first (a warming set last). Thread-safe."""

    def __init__(self, max_sets: int) -> None:
        self.max_sets = max(1, int(max_sets))
        self._sets: "OrderedDict[str, ExampleSet]" = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, fingerprint: str, digest: str) -> Tuple[ExampleSet, bool]:
        """(the set, whether the caller must start warming it).

        A known fingerprint whose digest matches returns its set -- ready or
        still warming -- and False. An unknown fingerprint, or one sent with
        different items, registers a new warming set and returns it with
        True: the caller submits the warming job.
        """
        with self._lock:
            found = self._sets.get(fingerprint)
            if found is not None and found.digest == digest:
                self._sets.move_to_end(fingerprint)
                return found, False
            entry = ExampleSet(fingerprint=fingerprint, digest=digest)
            self._sets[fingerprint] = entry
            self._sets.move_to_end(fingerprint)
            self._evict()
            return entry, True

    def _evict(self) -> None:
        while len(self._sets) > self.max_sets:
            victim = next((fp for fp, s in self._sets.items() if s.state == READY), None)
            if victim is None:
                victim = next(iter(self._sets))
            del self._sets[victim]

    def ready(self, entry: ExampleSet, ids: List[str], routes: List[str], vectors: Any) -> bool:
        """Publish a finished warming. False when the entry was evicted or
        replaced meanwhile (the vectors are then dropped)."""
        with self._lock:
            if self._sets.get(entry.fingerprint) is not entry:
                return False
            entry.ids, entry.routes, entry.vectors = list(ids), list(routes), vectors
            entry.state = READY
            return True

    def discard(self, entry: ExampleSet) -> None:
        """Forget a set whose warming failed, so the next request tries again."""
        with self._lock:
            if self._sets.get(entry.fingerprint) is entry:
                del self._sets[entry.fingerprint]

    def states(self) -> Dict[str, int]:
        with self._lock:
            out = {WARMING: 0, READY: 0}
            for entry in self._sets.values():
                out[entry.state] = out.get(entry.state, 0) + 1
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._sets)
