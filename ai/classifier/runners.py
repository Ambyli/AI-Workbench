"""Job runners — the classifier's actual work, decoupled from queueing.

Each runner takes the JSON-safe payload dict that main.py stored at enqueue
time (via the ``build_*_payload`` helpers here) and returns the result dict
that ends up in ``GET /jobs/{id}``'s ``result`` field. Nothing in this module
knows about the registry, the worker pool, or Prometheus — that is workers.py.

  build_assess_payload()  ↔  run_assess()   — single-image assessment via analysis.analyze_bgr()
  build_compare_payload() ↔  run_compare()  — multi-example comparison via analysis + scoring,
                                              with all examples analysed concurrently.

Payloads round-trip through JSON on disk (``common.jobs.payloads``), so image
bytes are base64 and Pydantic models are dumped to dicts and re-validated here.

Process flow position: called by workers.handle_job() after a WorkerPool
worker has claimed the job.
"""

import asyncio
import base64
from typing import Any, Optional

from analysis import analyze_bgr, analyze_input, resolve_example, _bytes_to_bgr
from models import CompareRequest, CriterionInput
from scoring import aggregate, combined_score, compute_similarity


# ---------------------------------------------------------------------------
# Payload builders (called by main.py at enqueue time)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Runners (called by workers.handle_job)
# ---------------------------------------------------------------------------

async def run_assess(payload: dict[str, Any]) -> dict:
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


async def run_compare(payload: dict[str, Any]) -> dict:
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
