"""Background job worker.

Pulls jobs from the in-memory asyncio queue, executes analysis, and
persists results (or errors) to the SQLite job store.
"""

import asyncio
import time

from prometheus_client import Counter, Gauge, Histogram

import store
from analysis import analyze_bgr, analyze_input, resolve_example, _bytes_to_bgr
from logger import logger
from middleware import request_id_var
from models import CompareRequest, CriterionInput, ImageInput
from scoring import aggregate, combined_score, compute_similarity

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

jobs_total = Counter(
    "classifier_jobs_total",
    "Total jobs by type and final status",
    ["type", "status"],
)
job_duration = Histogram(
    "classifier_job_duration_seconds",
    "Job processing time in seconds",
    ["type"],
)
job_queue_depth = Gauge(
    "classifier_job_queue_depth",
    "Number of jobs currently waiting in the queue",
)

# ---------------------------------------------------------------------------
# Queue (imported by main.py to enqueue jobs)
# ---------------------------------------------------------------------------

job_queue: asyncio.Queue = asyncio.Queue()


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------

async def _run_assess(job_data: dict) -> dict:
    bgr = await _bytes_to_bgr(job_data["image_bytes"])
    h, w = bgr.shape[:2]
    return await analyze_bgr(
        bgr, w, h,
        job_data["content_type"],
        len(job_data["image_bytes"]),
        job_data["criteria"],
    )


async def _run_compare(request: CompareRequest) -> dict:
    input_task = analyze_input(request.image, request.criteria)
    example_tasks = [resolve_example(ex, request.criteria) for ex in request.examples]

    results = await asyncio.gather(input_task, *example_tasks)
    input_analysis = results[0]
    example_analyses = results[1:]

    input_overall = input_analysis["llm_assessment"].get("overall_score", 5)
    example_results = []
    combined_scores = []

    for i, (example, analysis) in enumerate(zip(request.examples, example_analyses)):
        similarity = compute_similarity(
            analysis["llm_assessment"],
            input_analysis["llm_assessment"],
        )
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
    """Long-running coroutine — started at app startup, cancelled at shutdown."""
    logger.info("job_worker: started")
    while True:
        job_id, job_type, job_data, req_id = await job_queue.get()
        request_id_var.set(req_id)
        logger.info("job_worker: picked up job_id=%s type=%s queue_remaining=%d",
                    job_id, job_type, job_queue.qsize())
        job_queue_depth.set(job_queue.qsize())

        start = time.monotonic()
        try:
            await store.update_job(job_id, "processing")

            if job_type == "assess":
                result = await _run_assess(job_data)
            else:
                result = await _run_compare(job_data)

            await store.update_job(job_id, "completed", result=result)
            elapsed = time.monotonic() - start
            jobs_total.labels(type=job_type, status="completed").inc()
            job_duration.labels(type=job_type).observe(elapsed)
            logger.info("job_worker: job_id=%s completed in %.2fs", job_id, elapsed)

        except Exception as exc:
            elapsed = time.monotonic() - start
            await store.update_job(job_id, "failed", error=str(exc))
            jobs_total.labels(type=job_type, status="failed").inc()
            job_duration.labels(type=job_type).observe(elapsed)
            logger.error("job_worker: job_id=%s failed after %.2fs: %s", job_id, elapsed, exc)

        finally:
            job_queue.task_done()
            job_queue_depth.set(job_queue.qsize())
