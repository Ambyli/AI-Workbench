"""Prometheus metrics for the classifier's job pipeline.

Kept in their own module so both the HTTP layer (api/, which counts
submissions) and the worker glue (jobs/queue.py, which records outcomes and
durations) import the same objects without either importing the other.

The artifact gauges are refreshed by ai/classifier/regions/sweeper.py — on
every sweep, and whenever a directory is written, cached into, or deleted — so
"how much disk are the region layers holding?" is answerable from Prometheus
rather than by exec-ing into the container.

``llm_bbox_attempts`` (llm/boxes.py) and ``diff_jobs`` (compare/diff.py) live
here for
the same reason: they are produced in one module and read in none, so a
counter defined next to its producer would be invisible to anyone looking for
"what does this service measure".

Scraped by Prometheus (see prometheus.yml) and visualised in Grafana alongside
LiteLLM metrics from the same Prometheus instance.
"""

from prometheus_client import Counter, Gauge, Histogram

jobs_total = Counter(
    "classifier_jobs_total",
    "Total jobs by type and final status",
    ["type", "status"],  # type: assess|compare, status: pending|completed|failed
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
llm_bbox_attempts = Counter(
    "classifier_llm_bbox_attempts_total",
    "LLM enforcement-loop attempts by outcome",
    # accepted         — the box validated AND the crop confirmed it
    # rejected_invalid — the numbers were not a usable box
    # rejected_verify  — the box was usable but the crop did not show it
    # exhausted        — counted once per criterion that ran out of attempts
    ["outcome"],
)
diff_jobs = Counter(
    "classifier_diff_jobs_total",
    "Change-detection runs by whether the two images could be aligned",
    ["aligned"],  # "true" | "false"
)
