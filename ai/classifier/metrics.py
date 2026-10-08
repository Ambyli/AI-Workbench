"""Prometheus metrics for the classifier's job pipeline.

Kept in their own module so both the HTTP layer (api/, which counts
submissions) and the worker glue (jobs/queue.py, which records outcomes and
durations) import the same objects without either importing the other.

The artifact gauges are refreshed by ai/classifier/regions/sweeper.py — on
every sweep, and whenever a directory is written, cached into, or deleted — so
"how much disk are the region layers holding?" is answerable from Prometheus
rather than by exec-ing into the container.

The reference gauges (``classifier_references{status}``, ``_reference_bytes``,
``_reference_dirs``) are refreshed by ai/classifier/references/store.py on every
create, finish, fail and delete; the creation outcomes and the generated
descriptions are counted by jobs/runners.py.

``llm_bbox_attempts`` (llm/boxes.py) and ``llm_usage_write_errors``
(llm/usage.py) live here for the same reason: each is produced in one module
and read in none, so a counter defined next to its producer would be
invisible to anyone looking for "what does this service measure".

Scraped by Prometheus (see prometheus.yml) and visualised in Grafana alongside
LiteLLM metrics from the same Prometheus instance.
"""

from prometheus_client import Counter, Gauge, Histogram

jobs_total = Counter(
    "classifier_jobs_total",
    "Total jobs by type and final status",
    ["type", "status"],  # type: assess, status: pending|completed|failed
)
job_duration = Histogram(
    "classifier_job_duration_seconds",
    "End-to-end job processing time from claim to store write",
    ["type"],
)
job_queue_depth = Gauge(
    "classifier_job_queue_depth",
    "Number of jobs currently waiting in phase=pending",
)
jobs_in_flight = Gauge(
    "classifier_jobs_in_flight",
    "Number of jobs currently being processed by a worker (<= CLASSIFIER_MAX_CONCURRENT)",
)
artifact_bytes = Gauge(
    "classifier_artifact_bytes",
    "Total bytes held in CLASSIFIER_ARTIFACT_DIR across all jobs",
)
artifact_dirs = Gauge(
    "classifier_artifact_dirs",
    "Number of per-job artifact directories currently on disk",
)
# References (references/store.py refreshes the gauges on every create,
# finish, fail and delete; jobs/runners.py counts the outcomes).
references_by_status = Gauge(
    "classifier_references",
    "Stored reference examples by status",
    ["status"],  # pending | ready | failed
)
reference_bytes = Gauge(
    "classifier_reference_bytes",
    "Total bytes held in CLASSIFIER_REFERENCE_DIR (never swept; bounded by "
    "CLASSIFIER_REFERENCE_MAX_COUNT)",
)
reference_dirs = Gauge(
    "classifier_reference_dirs",
    "Number of reference directories currently on disk",
)
references_created_total = Counter(
    "classifier_references_created_total",
    "Reference creation jobs by outcome",
    ["outcome"],  # ready | failed
)
reference_describe_total = Counter(
    "classifier_reference_describe_total",
    "Generated reference descriptions by outcome",
    # ok      — the model described the page
    # failed  — the call failed; the reference was readied without one
    ["outcome"],
)
reference_calls_total = Counter(
    "classifier_reference_calls_total",
    "Vision-model calls made on behalf of references (analysis/references.py, "
    "analysis/llm_eval.py)",
    # selection — one `references: "auto"` selection call per item (any outcome)
    # scoring   — one reference-guided scoring call (any outcome)
    ["kind", "outcome"],  # outcome: ok | failed
)
# Model usage records (llm/usage.py): a row that could not be written. The
# model call it describes is unaffected — accounting never fails a call — so
# this counter is the only sign that GET /jobs/{id}/usage is missing calls.
llm_usage_write_errors = Counter(
    "classifier_llm_usage_write_errors_total",
    "Vision-model calls whose llm_calls usage row could not be written "
    "(the call itself is unaffected)",
)
llm_bbox_attempts = Counter(
    "classifier_llm_bbox_attempts_total",
    "LLM enforcement-loop attempts by outcome",
    # accepted         — the box validated AND the crop confirmed it
    # rejected_invalid — the numbers were not a usable box
    # rejected_verify  — the box was usable but the crop did not show it
    # exhausted        — counted once per criterion that ran out of attempts
    ["outcome"],
)