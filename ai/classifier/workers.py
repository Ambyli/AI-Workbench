"""Background job worker and in-memory job queue.

The classifier uses an async job pattern so that POST /assess and
POST /assess/compare can return a job ID immediately (202 Accepted) without
blocking the HTTP connection for the full duration of the LLM call.

How it works:
  1. main.py enqueues a tuple (job_id, type, data, request_id) into job_queue.
  2. job_worker() runs as a long-lived asyncio task (started at app startup).
  3. The worker pulls jobs one at a time, calls the appropriate runner, and
     persists the result (or error) to the shared SQLite registry via
     ``jobs_registry.set_result(...)`` / ``set_error(...)``.
  4. Callers poll GET /jobs/{job_id} until phase is "completed" or "failed".

Two job runners:
  _run_assess()   — single-image assessment via analysis.analyze_bgr().
  _run_compare()  — multi-example comparison via analysis + scoring modules,
                    with all examples analysed concurrently via asyncio.gather().

Prometheus metrics track queue depth, job counts, and processing durations.

Process flow position: started by main.lifespan() at startup; consumes from
job_queue which is populated by main.assess_document() and
main.assess_with_reference().

Registry injection: main.py hands the SqliteRegistry instance to this module
via ``set_registry(...)`` before starting the worker task. This keeps the
worker de-coupled from ``config.DB_PATH`` construction.
"""

import asyncio
import time
from typing import Optional

from prometheus_client import Counter, Gauge, Histogram

from common.jobs.sqlite import SqliteRegistry

from analysis import analyze_bgr, analyze_input, resolve_example, _bytes_to_bgr
from logger import logger
from middleware import request_id_var
from models import CompareRequest, ImageInput
from scoring import aggregate, combined_score, compute_similarity

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
# These are scraped by Prometheus (see prometheus.yml) and can be visualised
# in Grafana alongside LiteLLM metrics from the same Prometheus instance.

jobs_total = Counter(
    "classifier_jobs_total",
    "Total jobs by type and final status",
    ["type", "status"],  # type: assess|compare, status: pending|completed|failed
)
job_duration = Histogram(
    "classifier_job_duration_seconds",
    "End-to-end job processing time from dequeue to store write",
    ["type"],
)
job_queue_depth = Gauge(
    "classifier_job_queue_depth",
    "Number of jobs currently waiting in the in-memory queue",
)

# ---------------------------------------------------------------------------
# In-memory job queue
# ---------------------------------------------------------------------------
# Imported by main.py which enqueues jobs. Each item is a 4-tuple:
#   (job_id: str, job_type: str, job_data: dict|CompareRequest, req_id: str)
job_queue: asyncio.Queue = asyncio.Queue()


# ---------------------------------------------------------------------------
# Registry injection
# ---------------------------------------------------------------------------
# main.py calls set_registry() during lifespan startup so the worker can
# persist phase / result / error transitions. Kept module-level to preserve
# the "long-lived asyncio task with no explicit dependency wiring" shape.
_registry: Optional[SqliteRegistry] = None


def set_registry(registry: SqliteRegistry) -> None:
    """Hand the shared job registry to the worker module. Called once by
    main.lifespan() before the worker task is started."""
    global _registry
    _registry = registry


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------

async def _run_assess(job_data: dict) -> dict:
    """Execute a single-image assessment job.

    Decodes the stored image bytes, runs the full analysis pipeline, and
    returns the result dict that will be persisted to the job store.

    Args:
        job_data: dict with keys image_bytes, content_type, filename, criteria.

    Returns:
        Analysis result dict from analyze_bgr().
    """
    # Decode bytes → BGR numpy array (includes EXIF correction and magic check)
    bgr = await _bytes_to_bgr(job_data["image_bytes"])
    h, w = bgr.shape[:2]
    return await analyze_bgr(
        bgr, w, h,
        job_data["content_type"],
        len(job_data["image_bytes"]),
        job_data["criteria"],
    )


async def _run_compare(request: CompareRequest) -> dict:
    """Execute a comparison job against one or more reference examples.

    Analyses the subject image and all reference examples concurrently using
    asyncio.gather().  Examples with pre_generated_analysis skip the LLM call
    entirely (resolve_example handles this).  After gathering all analyses,
    computes per-example similarity and combined scores, then aggregates.

    Args:
        request: The original CompareRequest from the HTTP body.

    Returns:
        Full comparison result dict including per-example breakdowns and
        an aggregate verdict.
    """
    # Analyse the subject image and all examples concurrently.
    # Pre-generated examples resolve instantly; live examples hit the LLM in parallel.
    input_task = analyze_input(request.image, request.criteria)
    example_tasks = [resolve_example(ex, request.criteria) for ex in request.examples]
    results = await asyncio.gather(input_task, *example_tasks)

    input_analysis = results[0]
    example_analyses = results[1:]

    # Use the assessment (weighted combined) for scoring and similarity
    input_overall = input_analysis["assessment"].get("overall_score", 5)

    example_results = []
    combined_scores = []

    for i, (example, analysis) in enumerate(zip(request.examples, example_analyses)):
        # Compute similarity across all criteria
        similarity = compute_similarity(
            analysis["assessment"],
            input_analysis["assessment"],
        )
        # Blend quality score with similarity score using the example's weight
        cs = combined_score(input_overall, similarity["similarity_score"], example.weight)
        combined_scores.append(cs["score"])
        example_results.append({
            "index": i,
            "weight": example.weight,
            "pre_generated": example.pre_generated_analysis is not None,
            "example_analysis": analysis,
            "similarity": similarity,
            "combined_score": cs["score"],
            "combined_verdict": cs["verdict"],
        })

    # Collapse per-example scores into a single aggregate verdict
    agg = aggregate(combined_scores, request.aggregation)
    return {
        "status": "ok",
        "criteria": [c.model_dump() for c in request.criteria],
        "aggregation": request.aggregation,
        "input_analysis": input_analysis,
        "example_results": example_results,
        "aggregate": {
            "method": request.aggregation,
            "combined_score": agg["score"],
            "combined_verdict": agg["verdict"],
            "per_example_combined_scores": combined_scores,
        },
    }


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

async def job_worker() -> None:
    """Long-running coroutine that processes jobs from the in-memory queue.

    Started as an asyncio Task by main.lifespan() at app startup and cancelled
    at shutdown. Runs forever — one job at a time — updating job state in the
    shared SQLite registry at each transition: pending → processing →
    completed | failed.

    The correlation ID from the originating HTTP request is restored into the
    ContextVar before processing so that all log lines for the job carry the
    same [request_id] prefix as the original request.
    """
    if _registry is None:
        raise RuntimeError(
            "job_worker started before set_registry() was called; "
            "main.lifespan must hand the SqliteRegistry to workers.py first"
        )

    logger.info("job_worker: started")
    while True:
        # Block until a job is available in the queue
        job_id, job_type, job_data, req_id = await job_queue.get()

        # Restore the correlation ID so logs are traceable to the originating request
        request_id_var.set(req_id)
        logger.info("job_worker: picked up job_id=%s type=%s queue_remaining=%d",
                    job_id, job_type, job_queue.qsize())
        job_queue_depth.set(job_queue.qsize())

        start = time.monotonic()
        try:
            # Mark as processing before the expensive LLM call
            await _registry.set_phase(job_id, "processing")

            # Dispatch to the correct runner
            if job_type == "assess":
                result = await _run_assess(job_data)
            else:
                result = await _run_compare(job_data)

            # Persist result and record metrics — set_result also flips phase → "completed"
            await _registry.set_result(job_id, result)
            elapsed = time.monotonic() - start
            jobs_total.labels(type=job_type, status="completed").inc()
            job_duration.labels(type=job_type).observe(elapsed)
            logger.info("job_worker: job_id=%s completed in %.2fs", job_id, elapsed)

        except Exception as exc:
            # Persist error and record metrics — never let an exception kill the worker.
            # set_error also flips phase → "failed".
            elapsed = time.monotonic() - start
            await _registry.set_error(job_id, str(exc))
            jobs_total.labels(type=job_type, status="failed").inc()
            job_duration.labels(type=job_type).observe(elapsed)
            logger.error("job_worker: job_id=%s failed after %.2fs: %s", job_id, elapsed, exc)

        finally:
            # Always release the queue slot so qsize() stays accurate
            job_queue.task_done()
            job_queue_depth.set(job_queue.qsize())
