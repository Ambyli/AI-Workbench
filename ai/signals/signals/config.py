"""Signals sidecar settings -- INFRASTRUCTURE ONLY.

Where the models are, how many threads each model may use, how big the
feature cache is, the log level. Never a routing threshold: what score
forces a route, how large a topic share must be -- all of that belongs to
the route-policy rulesets that read these signals
(ai/route-policy/rulesets/), never to this service.

Read once at import, like route-policy's config.py; the compose file sets
the values (ai/signals/docker-compose.signals.yml), and the unit tests use
the defaults (they never load a real model).
"""

from __future__ import annotations

import os
import pathlib


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"

# -- Models ---------------------------------------------------------------------
# models.yaml sits next to the package (/app/ai/signals/models.yaml in the
# image); the files it pins are baked into SIGNALS_MODELS_DIR at build time,
# one plain directory per lane (/models/pii, /models/embed, ...). The image
# never downloads anything at run time (HF_HUB_OFFLINE=1 in the compose file
# is belt and braces: nothing here imports huggingface_hub).
MODELS_FILE: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent / "models.yaml"
MODELS_DIR: pathlib.Path = pathlib.Path(os.environ.get("SIGNALS_MODELS_DIR", "/models").strip() or "/models")

# The model lanes, in load order -- the keys of an answer's `models` and of
# common.signals.TASK_MODELS (`nearest` runs on `embed`). Each ONNX lane is
# one onnxruntime session behind its own single-flight queue
# (scheduler.Lane); `language` (fastText, microseconds) runs inline on the
# event loop.
LANES = ("pii", "embed", "injection", "language")
ONNX_LANES = ("pii", "embed", "injection")

# Intra-op threads per onnxruntime session (inter-op is always 1). Measured
# on the box (ROUTE_POLICY_PHASE5_PLAN § 7.3): 2 per model is the fastest
# for short messages when all three run at once -- 4 each (12 threads)
# contend and short turns get slower. With SIGNALS_CPUSET's 8 cores that is
# 6 busy threads plus the event loop.
THREADS = {
    "pii": _env_int("SIGNALS_THREADS_PII", 2, 1),
    "embed": _env_int("SIGNALS_THREADS_EMBED", 2, 1),
    "injection": _env_int("SIGNALS_THREADS_INJECTION", 2, 1),
}

# sys.setswitchinterval for the process. The deadline is the event loop's
# asyncio.wait timer, and while three lane threads run Python (tokenising,
# post-processing) the loop waits for the GIL up to one interval per handoff:
# at CPython's 5 ms default a 2,000-character request answered at 101.5 ms
# against a 90 ms deadline (2026-10-20 end-to-end), past route-policy's cut,
# which then dropped every partial result. 1 ms keeps the overshoot small.
GIL_SWITCH_INTERVAL_S: float = 0.001

# -- Cache ------------------------------------------------------------------------
# The in-memory LRU of sha256(task, model, params, text) -> features (an
# entity summary, an injection score, a 384-d query embedding -- never the
# text). One shared LRU for every task; 20,000 entries is ~40 MB at worst.
# 0 disables caching (every call recomputes). Gone on restart, by design.
CACHE_ENTRIES: int = _env_int("SIGNALS_CACHE_ENTRIES", 20000, 0)

# Embedded example sets kept at once (nearest task), least recently used
# evicted first. A set is <= MAX_EXAMPLES x 384 floats (~1.5 MB).
EXAMPLE_SETS_MAX: int = 8

# Examples embedded per onnxruntime call while warming a set. Between two
# batches a waiting /signals job takes the embed model first.
EMBED_BATCH: int = 16

# -- Decoding (model post-processing, not routing) ----------------------------------
# A PII token counts towards a span only when its label's probability is at
# least this. It is how the span boundaries the LOCATION + digit rule reads
# were measured (ROUTE_POLICY_PHASE5_PLAN § 2.1), so it moves with the model,
# not with a ruleset; a ruleset's `min_score` filters the reported
# max_score on top of it.
PII_TOKEN_MIN_PROB: float = 0.5

# -- Requests -------------------------------------------------------------------------
# The request caps (characters, texts, examples) and the limits on k,
# max_tokens and deadline_ms are the wire's (common.signals); this one is
# the sidecar's own: a body larger than this is refused (413) before it is
# read -- the wire's character caps in UTF-8 at worst, plus a full example
# set and the JSON around them.
MAX_BODY_BYTES: int = 4_000_000

# How long shutdown waits for the model lanes to stop.
DRAIN_S: float = 5.0
