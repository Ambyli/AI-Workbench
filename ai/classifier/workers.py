"""Background job workers, durable job queue, and on-disk payload store.

The classifier uses an async job pattern so that POST /assess and
POST /assess/compare can return a job ID immediately (202 Accepted) without
blocking the HTTP connection for the full duration of the LLM call.

How it works:
  1. main.py registers a job row in the shared SQLite registry (phase
     "staging"), writes the job's payload to PAYLOAD_DIR/<job_id>.json,
     flips the row to "pending", and calls notify_new_job().
  2. MAX_CONCURRENT copies of job_worker() run as long-lived asyncio tasks
     (started at app startup). Each loops on ``registry.claim_next()``, which
     atomically moves the oldest "pending" row to "processing" — so at most
     MAX_CONCURRENT jobs are ever in flight and no two workers take the same
     job. Idle workers sleep on an asyncio.Event that notify_new_job() sets,
     with WORKER_POLL_INTERVAL_S as a fallback poll for rows written by
     another process or left behind by a crash.
  3. The worker reads the payload file, calls the appropriate runner, and
     persists the result (or error) via ``set_result(...)`` / ``set_error(...)``.
     The payload file is deleted once the job reaches a terminal phase.
  4. Callers poll GET /jobs/{job_id} until phase is "completed" or "failed".

Why the DB is the queue, not an asyncio.Queue: an in-memory queue loses
every pending job on restart and can't be shared across processes. With the
registry as the queue, ``recover_interrupted()`` at startup requeues rows a
previous process left in "processing", and the payload on disk means the
work can actually be redone.

Two job runners:
  _run_assess()   — single-image assessment via analysis.analyze_bgr().
  _run_compare()  — multi-example comparison via analysis + scoring modules,
                    with all examples analysed concurrently via asyncio.gather().

Prometheus metrics track queue depth, in-flight jobs, job counts, and
processing durations.

Process flow position: started by main.lifespan() at startup; consumes rows
that main.assess_document() and main.assess_with_reference() enqueue.

Registry injection: main.py hands the SqliteRegistry instance to this module
via ``set_registry(...)`` before starting the worker tasks. This keeps the
worker de-coupled from ``config.DB_PATH`` construction.
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from prometheus_client import Counter, Gauge, Histogram

from common.jobs.model import JobBase
from common.jobs.sqlite import SqliteRegistry

from analysis import analyze_bgr, analyze_input, resolve_example, _bytes_to_bgr
from config import MAX_CONCURRENT, PAYLOAD_DIR, WORKER_POLL_INTERVAL_S
from logger import logger
from middleware import request_id_var
from models import CompareRequest, CriterionInput
from scoring import aggregate, combined_score, compute_similarity

# Phases a job can never leave. Payload files for these are garbage.
TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})

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

# ---------------------------------------------------------------------------
# Registry injection
# ---------------------------------------------------------------------------
# main.py calls set_registry() during lifespan startup so the workers can
# claim rows and persist phase / result / error transitions.
_registry: Optional[SqliteRegistry] = None


def set_registry(registry: SqliteRegistry) -> None:
    """Hand the shared job registry to the worker module. Called once by
    main.lifespan() before the worker tasks are started."""
    global _registry
    _registry = registry


def _require_registry() -> SqliteRegistry:
    if _registry is None:
        raise RuntimeError(
            "workers used before set_registry() was called; "
            "main.lifespan must hand the SqliteRegistry to workers.py first"
        )
    return _registry


# ---------------------------------------------------------------------------
# Wake-up signal
# ---------------------------------------------------------------------------
# Producers in this process set the event after flipping a row to "pending"
# so an idle worker claims it immediately instead of waiting for the poll.
_wake = asyncio.Event()


def notify_new_job() -> None:
    """Wake idle workers. Cheap and idempotent — call after every enqueue."""
    _wake.set()


# ---------------------------------------------------------------------------
# Payload store — one JSON file per queued job
# ---------------------------------------------------------------------------
# Image bytes and CompareRequests are too large to want in the jobs table's
# metadata JSON, and keeping them only in memory would make the queue lose
# work on restart. So each job's input lives at PAYLOAD_DIR/<job_id>.json
# from enqueue until the job hits a terminal phase.

def _payload_path(job_id: str) -> Path:
    return Path(PAYLOAD_DIR) / f"{job_id}.json"


def build_assess_payload(
    image_bytes: bytes,
    content_type: Optional[str],
    filename: str,
    criteria: list[CriterionInput],
) -> dict[str, Any]:
    """Serialise a POST /assess submission for the payload store."""
    return {
        "image_b64": base64.b64encode(image_bytes).decode("ascii"),
        "content_type": content_type or "application/octet-stream",
        "filename": filename,
        "criteria": [c.model_dump() for c in criteria],
    }


def build_compare_payload(request: CompareRequest) -> dict[str, Any]:
    """Serialise a POST /assess/compare body for the payload store."""
    return {"request": request.model_dump()}


async def write_payload(job_id: str, payload: dict[str, Any]) -> None:
    """Persist ``payload`` for ``job_id``. Atomic via tmp-file + rename so a
    worker can never read a half-written file."""
    path = _payload_path(job_id)

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)

    await asyncio.to_thread(_write)


async def read_payload(job_id: str) -> Optional[dict[str, Any]]:
    """Load the payload for ``job_id``, or ``None`` if the file is gone."""
    path = _payload_path(job_id)

    def _read() -> Optional[dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None

    return await asyncio.to_thread(_read)


async def delete_payload(job_id: str) -> None:
    """Remove the payload file for ``job_id``. Missing file is not an error."""
    path = _payload_path(job_id)

    def _unlink() -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    await asyncio.to_thread(_unlink)


async def sweep_orphan_payloads() -> int:
    """Delete payload files whose job row is gone or already terminal.

    Runs once at startup. Orphans appear when a job is deleted via
    DELETE /jobs/{id} while still pending, or when the process died between
    ``set_result`` and ``delete_payload``. Returns how many files were removed.
    """
    registry = _require_registry()
    root = Path(PAYLOAD_DIR)
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.glob("*.json"):
        job_id = path.stem
        job = await registry.get(job_id)
        if job is None or job.phase in TERMINAL_PHASES:
            await delete_payload(job_id)
            removed += 1
    # Half-written temp files from a crash mid-write are always garbage.
    for tmp in root.glob("*.json.tmp"):
        tmp.unlink(missing_ok=True)
    return removed


# ---------------------------------------------------------------------------
# Queue bookkeeping
# ---------------------------------------------------------------------------

async def refresh_queue_depth() -> int:
    """Re-read the pending count from the registry into the gauge."""
    counts = await _require_registry().count_by_phase()
    pending = counts.get("pending", 0)
    job_queue_depth.set(pending)
    return pending


async def recover_interrupted() -> int:
    """Requeue jobs a previous process left mid-flight.

    Called once at startup before any worker runs. Every row still in
    "processing" (or stuck in "staging" — payload written but the process
    died before the flip) goes back to "pending" so the new workers redo it.
    A job whose payload file is missing will fail fast when claimed.
    """
    registry = _require_registry()
    requeued = await registry.reset_phase("processing", "pending")
    requeued += await registry.reset_phase("staging", "pending")
    return requeued


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------

async def _run_assess(payload: dict[str, Any]) -> dict:
    """Execute a single-image assessment job from its stored payload.

    Decodes the image bytes, runs the full analysis pipeline, and returns
    the result dict that will be persisted to the job store.
    """
    image_bytes = base64.b64decode(payload["image_b64"])
    criteria = [CriterionInput.model_validate(c) for c in payload["criteria"]]
    # Decode bytes → BGR numpy array (includes EXIF correction and magic check)
    bgr = await _bytes_to_bgr(image_bytes)
    h, w = bgr.shape[:2]
    return await analyze_bgr(
        bgr, w, h,
        payload["content_type"],
        len(image_bytes),
        criteria,
    )


async def _run_compare(payload: dict[str, Any]) -> dict:
    """Execute a comparison job against one or more reference examples.

    Analyses the subject image and all reference examples concurrently using
    asyncio.gather().  Examples with pre_generated_analysis skip the LLM call
    entirely (resolve_example handles this).  After gathering all analyses,
    computes per-example similarity and combined scores, then aggregates.
    """
    request = CompareRequest.model_validate(payload["request"])

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

async def _process(job: JobBase, worker_idx: int) -> None:
    """Run one already-claimed job to a terminal phase. Never raises."""
    registry = _require_registry()
    job_id = job.job_id
    job_type = job.metadata.get("type", "assess")

    # Restore the correlation ID so logs are traceable to the originating request
    request_id_var.set(job.metadata.get("request_id", "-"))
    logger.info("job_worker[%d]: claimed job_id=%s type=%s", worker_idx, job_id, job_type)

    jobs_in_flight.inc()
    start = time.monotonic()
    try:
        payload = await read_payload(job_id)
        if payload is None:
            raise RuntimeError(
                "payload missing — the job's input file was removed or never "
                "written (usually a restart between enqueue and payload write)"
            )

        # Dispatch to the correct runner
        if job_type == "assess":
            result = await _run_assess(payload)
        else:
            result = await _run_compare(payload)

        # Persist result and record metrics — set_result also flips phase → "completed"
        await registry.set_result(job_id, result)
        elapsed = time.monotonic() - start
        jobs_total.labels(type=job_type, status="completed").inc()
        job_duration.labels(type=job_type).observe(elapsed)
        logger.info("job_worker[%d]: job_id=%s completed in %.2fs", worker_idx, job_id, elapsed)

    except Exception as exc:
        # Persist error and record metrics — never let an exception kill the worker.
        # set_error also flips phase → "failed".
        elapsed = time.monotonic() - start
        try:
            await registry.set_error(job_id, str(exc))
        except Exception as db_exc:  # pragma: no cover — DB unavailable
            logger.error("job_worker[%d]: could not record failure for job_id=%s: %s",
                         worker_idx, job_id, db_exc)
        jobs_total.labels(type=job_type, status="failed").inc()
        job_duration.labels(type=job_type).observe(elapsed)
        logger.error("job_worker[%d]: job_id=%s failed after %.2fs: %s",
                     worker_idx, job_id, elapsed, exc)

    finally:
        jobs_in_flight.dec()
        await delete_payload(job_id)


async def job_worker(worker_idx: int = 0) -> None:
    """Long-running coroutine that claims and processes jobs from the registry.

    MAX_CONCURRENT copies are started as asyncio Tasks by main.lifespan() and
    cancelled at shutdown. Each iteration atomically claims the oldest
    "pending" row (``claim_next`` flips it to "processing" in the same
    transaction), so the number of running workers is the hard cap on
    simultaneous jobs. When nothing is pending the worker waits on the wake
    event, falling back to a WORKER_POLL_INTERVAL_S poll.
    """
    registry = _require_registry()
    logger.info("job_worker[%d]: started", worker_idx)
    while True:
        try:
            job = await registry.claim_next("pending", "processing")
        except Exception as exc:
            # A transient DB error must not kill the worker — back off and retry.
            logger.error("job_worker[%d]: claim failed: %s", worker_idx, exc)
            await asyncio.sleep(WORKER_POLL_INTERVAL_S)
            continue

        if job is None:
            # Nothing waiting. Sleep until a producer wakes us or the poll fires.
            try:
                await asyncio.wait_for(_wake.wait(), timeout=WORKER_POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            _wake.clear()
            continue

        try:
            await refresh_queue_depth()
        except Exception:  # metrics are best-effort
            pass
        await _process(job, worker_idx)
        try:
            await refresh_queue_depth()
        except Exception:
            pass


def start_workers() -> list[asyncio.Task]:
    """Spawn MAX_CONCURRENT worker tasks. Returns the tasks so the caller
    can cancel them at shutdown."""
    tasks = [asyncio.create_task(job_worker(i), name=f"classifier-worker-{i}")
             for i in range(MAX_CONCURRENT)]
    logger.info("started %d job worker(s) (CLASSIFIER_MAX_CONCURRENT=%d)",
                len(tasks), MAX_CONCURRENT)
    return tasks
