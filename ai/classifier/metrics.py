"""Prometheus metrics for the classifier's job pipeline.

Kept in their own module so both the HTTP layer (main.py, which counts
submissions) and the worker glue (workers.py, which records outcomes and
durations) import the same objects without either importing the other.

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
