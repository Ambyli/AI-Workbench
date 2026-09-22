"""Job runners — the classifier's actual work, decoupled from queueing.

Each runner takes the JSON-safe payload dict that main.py stored at enqueue
time (via the ``build_*_payload`` helpers here) and returns the result dict
that ends up in ``GET /jobs/{id}``'s ``result`` field. Nothing in this module
knows about the registry, the worker pool, or Prometheus — that is workers.py.

  build_assess_payload()  ↔  run_assess()   — single-document assessment via
                                              analysis.analyze_document()
  build_compare_payload() ↔  run_compare()  — multi-example comparison via analysis + scoring,
                                              with all examples analysed concurrently.

Payloads round-trip through JSON on disk (``common.jobs.payloads``), so file
bytes are base64 and Pydantic models are dumped to dicts and re-validated here.
The uploaded file may be a JPEG/PNG, a PDF, a .txt, or a .docx — the bytes are
stored verbatim and the kind is detected when the worker loads them.

Process flow position: called by workers.handle_job() after a WorkerPool
worker has claimed the job.
"""

import asyncio
import base64
from typing import Any, Optional

from analysis import analyze_document, analyze_input, load_document_bytes, resolve_example
from models import CompareRequest, CriterionInput
from scoring import aggregate, combined_score, compute_similarity


# ---------------------------------------------------------------------------
# Payload builders (called by main.py at enqueue time)
# ---------------------------------------------------------------------------

def build_assess_payload(
    file_bytes: bytes,
    content_type: Optional[str],
    filename: str,
    criteria: list[CriterionInput],
    ocr: str = "auto",
) -> dict[str, Any]:
    """Serialise a POST /assess submission for the payload store.

    The bytes are stored under ``file_b64``; ``run_assess`` still accepts the
    older ``image_b64`` key so a job queued by a previous container version
    survives the upgrade.
    """
    return {
        "file_b64": base64.b64encode(file_bytes).decode("ascii"),
        "content_type": content_type or "application/octet-stream",
        "filename": filename,
        "criteria": [c.model_dump() for c in criteria],
        "ocr": ocr,
    }


def build_compare_payload(request: CompareRequest) -> dict[str, Any]:
    """Serialise a POST /assess/compare body for the payload store."""
    return {"request": request.model_dump()}


# ---------------------------------------------------------------------------
# Runners (called by workers.handle_job)
# ---------------------------------------------------------------------------

async def run_assess(payload: dict[str, Any]) -> dict:
    """Execute a single-document assessment job from its stored payload.

    Decodes the file bytes, loads them into a Document (kind detection, PDF
    page rendering, text extraction), runs the full analysis pipeline, and
    returns the result dict that will be persisted to the job store.
    """
    # "image_b64" is the pre-document-support key — still read so jobs queued
    # by an older container finish after an upgrade.
    encoded = payload.get("file_b64") or payload["image_b64"]
    file_bytes = base64.b64decode(encoded)
    criteria = [CriterionInput.model_validate(c) for c in payload["criteria"]]
    doc = load_document_bytes(
        file_bytes, payload.get("filename"), payload.get("content_type")
    )
    return await analyze_document(doc, criteria, payload.get("ocr", "auto"))


async def run_compare(payload: dict[str, Any]) -> dict:
    """Execute a comparison job against one or more reference examples.

    Analyses the subject image and all reference examples concurrently using
    asyncio.gather().  Examples with pre_generated_analysis skip the LLM call
    entirely (resolve_example handles this).  After gathering all analyses,
    computes per-example similarity and combined scores, then aggregates.
    """
    request = CompareRequest.model_validate(payload["request"])

    # Analyse the subject document and all examples concurrently, every one
    # under the same OCR policy so their text layers are comparable.
    # Pre-generated examples resolve instantly; live examples hit the LLM in parallel.
    input_task = analyze_input(request.image, request.criteria, request.ocr)
    example_tasks = [
        resolve_example(ex, request.criteria, request.ocr) for ex in request.examples
    ]
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
        "ocr": request.ocr,
        "input_analysis": input_analysis,
        "example_results": example_results,
        "aggregate": {
            "method": request.aggregation,
            "combined_score": agg["score"],
            "combined_verdict": agg["verdict"],
            "per_example_combined_scores": combined_scores,
        },
    }
