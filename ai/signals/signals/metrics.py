"""Prometheus metrics for the signals sidecar, served at GET /metrics.

Scraped by the litellm stack's prometheus (ai/litellm/prometheus.yml, job
"signals"). HTTP request counts and latencies per handler come from
prometheus-fastapi-instrumentator (app.py); these are the domain metrics.

Label values are bounded and never come from a request: ``task`` is one of
the four task names, ``lane`` one of the four model lanes, ``status`` one of
the seven task statuses, and the remaining labels are fixed lists below.
No metric ever carries text, a text id or a request id.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_MS_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 10.0)

requests_total = Counter(
    "signals_requests_total",
    "POST /signals requests by outcome",
    # ok        -- answered (whatever each task's status)
    # rejected  -- 413 (over a request cap) or 422 (malformed)
    # error     -- 500 (a bug; logged with the exception type only)
    ["outcome"],
)
request_seconds = Histogram(
    "signals_request_seconds",
    "Server time of one answered POST /signals, from parsed body to response",
    buckets=_MS_BUCKETS,
)
tasks_total = Counter(
    "signals_tasks_total",
    "Task results by task and status (ok | partial | deadline | skipped | warming | error | unavailable)",
    ["task", "status"],
)
task_seconds = Histogram(
    "signals_task_seconds",
    "From the request's arrival until a task's last text was ready (tasks that finished or were cut "
    "with results)",
    ["task"],
    buckets=_MS_BUCKETS,
)
cache_total = Counter(
    "signals_cache_total",
    "Feature-cache lookups by task and result (hit | miss)",
    ["task", "result"],
)
cache_entries = Gauge("signals_cache_entries", "Entries in the feature cache")
text_seconds = Histogram(
    "signals_inference_seconds",
    "Model compute for one text (all its windows), by lane",
    ["lane"],
    buckets=_MS_BUCKETS,
)
queue_depth = Gauge(
    "signals_queue_depth",
    "Jobs waiting for a lane's model, by lane and queue",
    # foreground -- a request's job (skipped when its deadline passes before it starts)
    # background -- a PII job finishing after its response, or an example set being embedded
    ["lane", "queue"],
)
jobs_total = Counter(
    "signals_jobs_total",
    "Lane jobs by how they ended",
    # finished  -- every text done (in time or, for PII, after the response)
    # stopped   -- a non-PII job cut at its deadline
    # skipped   -- never started: its deadline passed in the queue
    # parked    -- a late PII job / a warming handed the model to a waiting request
    # replaced  -- a parked late PII job dropped for a newer one (one per lane)
    ["lane", "outcome"],
)
example_sets = Gauge("signals_example_sets", "Embedded example sets held, by state", ["state"])
warmings_total = Counter(
    "signals_example_set_warmings_total",
    "Example-set embeddings finished, by outcome (ok | failed | evicted)",
    ["outcome"],
)
model_ready = Gauge("signals_model_ready", "1 when the lane's model is loaded and warmed", ["lane"])
model_load_seconds = Gauge("signals_model_load_seconds", "How long the lane's model took to load and warm",
                           ["lane"])
