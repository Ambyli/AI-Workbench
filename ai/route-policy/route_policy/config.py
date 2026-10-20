"""Service settings -- INFRASTRUCTURE ONLY.

Where the database is, how big the pool is, how often the binding snapshot is
refreshed, where the baked-in rulesets live, where the document classifier
is and how long a classification may take, the log level. Never a routing
rule: what an evaluator matches, which route it votes for, the cooldown, the
combiner -- all of that is in a ruleset (``ai/route-policy/rulesets/``),
versioned in route-policy-db, never in an environment variable
(ROUTE_POLICY_PLAN § 8).

Read once at import, like the classifier's config.py; the compose file sets
the values (ai/route-policy/docker-compose.route-policy.yml), and the unit
tests set them before importing anything (unit-tests/route-policy/conftest.py).
"""

from __future__ import annotations

import os
import pathlib
from typing import Optional
from urllib.parse import quote


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"

# -- route-policy-db ----------------------------------------------------------
# HOST and PORT are fixed by the compose file (service DNS on ai_shared, the
# container port); the credentials come from the `## Route policy` block of
# .env. An empty host means "no database": the service refuses to start (it
# seeds its rulesets there), and the unit tests use that to keep a test run
# off any real database.
DB_HOST: str = os.environ.get("ROUTE_POLICY_DB_HOST", "").strip()
DB_PORT: int = _env_int("ROUTE_POLICY_DB_PORT", 5432, 1)
DB_USER: str = os.environ.get("ROUTE_POLICY_DB_USER", "route_policy")
DB_PASSWORD: str = os.environ.get("ROUTE_POLICY_DB_PASSWORD", "route_policy")
DB_NAME: str = os.environ.get("ROUTE_POLICY_DB_NAME", "route_policy")


def build_dsn(host: str, port: int, user: str, password: str, name: str) -> Optional[str]:
    """``postgresql://user:password@host:port/name``, user / password / name
    percent-quoted (a password with ``@`` or ``/`` must not break the URL);
    None when there is no host."""
    if not host:
        return None
    auth = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
    return f"postgresql://{auth}@{host}:{port}/{quote(name, safe='')}"


DB_DSN: Optional[str] = build_dsn(DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME)

# The pool's ceiling. Every /decide and /observe write borrows one connection
# for a few milliseconds; the default covers bursts from both LiteLLM copies
# during a swap with room to spare. Too small never errors: a write waits for
# a connection -- in the background once it outlasts the budget below.
DB_POOL_MAX: int = _env_int("ROUTE_POLICY_DB_POOL_MAX", 10, 1)

# -- Database waits on the request path ------------------------------------------
# LiteLLM awaits /decide for every `auto` turn within its own 0.25 s
# (ROUTE_POLICY_DECIDE_TIMEOUT_S) and falls back to local-private past it, so
# no request here may wait on the database for long (service.py).
#
# How long /decide (and /observe) waits for its row to be written before it
# answers anyway. A write that finishes in time behaves as if it were
# synchronous (a duplicate decision_id replays the FIRST stored decision); a
# slower one -- a hung database, an exhausted pool -- keeps running in the
# background, counted in route_policy_writes_deferred_total. 0 = never wait.
DECISION_WRITE_BUDGET_S: float = _env_float("ROUTE_POLICY_DECISION_WRITE_BUDGET_S", 0.05, 0.0)

# Background tasks allowed in flight at once: decision and outcome writes,
# and /decide's after-the-answer work (shadow rulesets, analysis requests).
# Past it a write is DROPPED (the decision is still returned;
# route_policy_errors_total{kind="decision_write_dropped"}, or
# "shadow_dropped"): a database that is down for minutes must not pile up
# tasks and connections without bound.
WRITES_MAX_IN_FLIGHT: int = 256

# How long shutdown waits for background writes -- and, separately, for the
# running classifications of the analysis queue -- before cancelling them.
WRITES_DRAIN_S: float = 5.0

# The database READS on the /decide path -- the binding refresh every
# ROUTE_POLICY_BINDING_REFRESH_S, and compiling a stored version this copy has
# not cached -- get this long. Past it the refresh finishes in the background
# and the request decides from the last snapshot; a version fetch answers 503
# (LiteLLM falls back to local-private) and lands in the cache for the next
# request. Not an environment variable: it only trades a few milliseconds of
# freshness for latency. The tests raise it (unit-tests/route-policy/conftest.py).
DB_READ_BUDGET_S: float = 0.1

# -- Ruleset cache --------------------------------------------------------------
# How old the in-memory binding snapshot may get before a /decide re-reads
# the `bindings` table (one small query). Ruleset versions are immutable, so
# a compiled ruleset is cached forever; only "which version does `auto` use"
# can change -- at startup today, through the write API in phase 6. A copy
# that cannot reach the database keeps deciding from its last snapshot.
BINDING_REFRESH_S: float = _env_float("ROUTE_POLICY_BINDING_REFRESH_S", 10.0, 0.0)

# -- Baked-in rulesets ----------------------------------------------------------
# ai/route-policy/rulesets/, next to this package -- /app/ai/route-policy/
# rulesets in the image (Dockerfile). Seeded into route-policy-db at startup.
RULESETS_DIR: pathlib.Path = pathlib.Path(__file__).resolve().parent.parent / "rulesets"

# -- The analysis queue (llm_classifier) -------------------------------------------
# Background classifications go through the document classifier's POST
# /assess (route_policy/analysis/classifier_client.py), by Docker DNS on
# ai_shared, no auth -- the same way LiteLLM reaches route-policy. Empty
# (only possible outside the compose file, whose ${...:-} falls back to the
# default) runs no analysis queue: requests are not classified and every
# llm_classifier abstains ("no verdict"). The unit tests use that.
CLASSIFIER_URL: str = os.environ.get("ROUTE_POLICY_CLASSIFIER_URL", "http://classifier:8000").strip().rstrip("/")

# One classification, from the locality check to the result: the job may wait
# behind document jobs in the classifier's queue, then makes one model call
# per criterion (concurrently, up to the model's max_concurrent). Past it the
# job is cancelled and deleted, and the analysis is stored as `timeout`.
CLASSIFY_TIMEOUT_S: float = _env_float("ROUTE_POLICY_CLASSIFY_TIMEOUT_S", 180.0, 1.0)

# How often GET /jobs/{id} is polled while a classification runs. Also the
# resolution of analyses.queue_ms.
CLASSIFY_POLL_S: float = _env_float("ROUTE_POLICY_CLASSIFY_POLL_S", 1.0, 0.1)

# Requests that may WAIT for a worker (a running one does not count). Past
# it a request is dropped and counted (route_policy_analysis_dropped_total):
# that turn gets no verdict. Each holds one rendered transcript in memory.
ANALYSIS_QUEUE_MAX: int = _env_int("ROUTE_POLICY_ANALYSIS_QUEUE_MAX", 64, 1)

# Workers, i.e. classifier jobs route-policy has open at once. The
# classifier's own per-model max_concurrent bounds the model calls inside
# them; this keeps route-policy from flooding the classifier's FIFO, which
# document jobs share.
ANALYSIS_CONCURRENCY: int = _env_int("ROUTE_POLICY_ANALYSIS_CONCURRENCY", 2, 1)

# How long GET /models (and a model host's locality verdict) is cached.
CLASSIFIER_MODELS_TTL_S: float = 60.0

# -- API caps -------------------------------------------------------------------
# GET /chats/{chat_id}/decisions: default and largest `limit`.
CHAT_DECISIONS_DEFAULT_LIMIT: int = 100
CHAT_DECISIONS_MAX_LIMIT: int = 1000
