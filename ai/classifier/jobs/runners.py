"""Job runners — the classifier's actual work, decoupled from queueing.

Each runner takes the JSON-safe payload dict ``jobs.payloads`` built at
enqueue time and returns the result dict that ends up in ``GET /jobs/{id}``'s
``result`` field. Nothing in this module knows about the registry, the worker
pool, or Prometheus — that is ``jobs.queue``.

  run_assess()  — single-document assessment via analysis.analyze_document()
  run_compare() — multi-example comparison via analysis + compare.scoring,
                  with all examples analysed concurrently.
  run_locate()  — the same pipeline with the judgement removed: no weighted
                  score, no verdict, no dependency resolution.

`run_compare` is also where the two compare-only region options land, because
they are the only work that needs BOTH sides of the comparison in one place:

  regions.diff      change detection (``compare.diff.detect_changes``) between
                    the subject and each LIVE single-image example, filed
                    under a synthetic ``_diff:e{i}`` criterion and rendered as
                    ``diff-e{i}-p0.<fmt>``. Diff regions never enter
                    ``compute_weighted_score`` or ``compute_similarity`` —
                    they are not criteria, and the aggregate score with `diff`
                    on must equal the one with it off.
  regions.examples  the examples' OWN regions and layers, written into the
                    subject's artifact directory under an ``e{i}.`` prefix
                    because an example has no job of its own.

The uploaded file may be a JPEG/PNG, a PDF, a .txt, or a .docx — the bytes are
stored verbatim and the kind is detected when the worker loads them.

Process flow position: called by ``jobs.queue.ClassifierQueue.handle_job``
after a WorkerPool worker has claimed the job.
"""

import asyncio
import base64
from typing import Any, Optional

from common.vision import Region, rescale_region

from analysis import (
    analyze_document,
    analyze_input,
    load_document_bytes,
    resolve_example,
)
from analysis.geometry import working_page_images
from api.schemas import CompareRequest, CriterionInput, RegionsOptions
from compare.diff import detect_changes
from compare.scoring import aggregate, combined_score, compute_similarity
from config import DIFF_MAX_REGIONS, INLINE_REGIONS_MAX
from jobs.payloads import _regions_from_payload
from logger import logger
from metrics import diff_jobs
from regions.artifacts import write_compare_artifacts
from regions.collect import diff_criterion_name

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
    regions = _regions_from_payload(payload)
    doc = load_document_bytes(
        file_bytes,
        payload.get("filename"),
        payload.get("content_type"),
        keep_source=bool(regions and regions.enabled),
    )
    return await analyze_document(
        doc,
        criteria,
        payload.get("ocr", "auto"),
        regions=regions,
        job_id=payload.get("job_id"),
    )


async def run_compare(payload: dict[str, Any]) -> dict:
    """Execute a comparison job against one or more reference examples.

    Analyses the subject image and all reference examples concurrently using
    asyncio.gather().  Examples with pre_generated_analysis skip the LLM call
    entirely (resolve_example handles this).  After gathering all analyses,
    computes per-example similarity and combined scores, then aggregates.

    With `regions.diff` or `regions.examples` set there is a second pass after
    the scoring is finished and frozen: change detection against each live
    example, and the examples' own layers. Both are strictly additive — the
    aggregate score, every similarity, and every verdict are identical with
    them on and off, which is the property the compare-level test asserts.
    """
    request = CompareRequest.model_validate(payload["request"])
    job_id = payload.get("job_id")
    options = request.regions
    want_regions = bool(options and options.enabled)
    want_diff = bool(want_regions and options.diff)
    want_example_layers = bool(want_regions and options.examples)
    # The pixels and Region objects the second pass needs. Filled by
    # analyze_document; empty dicts cost nothing when neither option is set.
    want_capture = want_diff or want_example_layers
    subject_capture: dict[str, Any] = {}
    example_captures: list[dict[str, Any]] = [{} for _ in request.examples]

    # An example analysed for its OWN layers runs the same region collection
    # the subject does, minus the two compare-only options — an example is
    # not itself a comparison, and recursing would be meaningless.
    example_regions = (
        options.model_copy(update={"diff": False, "examples": False})
        if want_example_layers
        else None
    )

    # Analyse the subject document and all examples concurrently, every one
    # under the same OCR policy so their text layers are comparable.
    # Pre-generated examples resolve instantly; live examples hit the LLM in parallel.
    # Layers are the subject's unless `regions.examples` is set: rendering both
    # sides doubles the artifact volume, and the subject is what a caller is
    # usually looking at.
    input_task = analyze_input(
        request.image,
        request.criteria,
        request.ocr,
        regions=request.regions,
        job_id=job_id,
        capture=subject_capture if want_capture else None,
    )
    example_tasks = [
        resolve_example(
            ex,
            request.criteria,
            request.ocr,
            regions=example_regions,
            capture=capture if want_capture else None,
        )
        for ex, capture in zip(request.examples, example_captures)
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

    # ── Second pass: change detection and example layers ──────────────────
    # Everything above this line is finished and is not touched again. The
    # diff regions are filed under a synthetic `_diff:e{i}` criterion that
    # nothing scores, so `aggregate` below sees exactly what it would have
    # seen with the options off.
    diff_regions: dict[str, list[Region]] = {}
    diff_notes: list[str] = []
    if want_diff:
        diff_regions = await _run_diffs(
            request, subject_capture, example_captures, example_results, diff_notes
        )

    if job_id and want_capture:
        renderable = [
            {
                "index": i,
                "document": capture.get("document"),
                "geometries": capture.get("geometries") or {},
                "region_map": capture.get("region_map") or {},
                "localizations": capture.get("localizations") or {},
                "criteria": request.criteria,
            }
            for i, capture in enumerate(example_captures)
            if want_example_layers and capture.get("document") is not None
        ]
        artifacts, diff_per_criterion, example_artifacts = await asyncio.to_thread(
            write_compare_artifacts,
            job_id,
            diff_regions,
            subject_capture.get("document"),
            subject_capture.get("geometries") or {},
            renderable,
            options,
            diff_notes,
        )
        if artifacts is not None:
            input_analysis["artifacts"] = artifacts
            for name, block in diff_per_criterion.items():
                index = int(name.rsplit("e", 1)[-1])
                if 0 <= index < len(example_results):
                    example_results[index].setdefault("diff", {})["artifacts"] = block
            for index, entry in example_artifacts.items():
                analysis = example_results[index]["example_analysis"]
                if isinstance(analysis, dict):
                    analysis["artifacts"] = entry["artifacts"]
                    _attach_example_artifacts(analysis, entry["per_criterion"])

    # Collapse per-example scores into a single aggregate verdict
    agg = aggregate(combined_scores, request.aggregation)
    result = {
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
    # Mirror the subject's artifact index at the top level so a caller does
    # not have to dig through input_analysis to find the layer URLs. Only
    # present when regions were requested.
    if input_analysis.get("artifacts") is not None:
        result["artifacts"] = input_analysis["artifacts"]
        result["page_geometry"] = input_analysis.get("page_geometry", [])
    return result


async def run_locate(payload: dict[str, Any]) -> dict:
    """Execute a locate job: find the features, report no judgement.

    Returns the same ``image_info`` / ``document_info`` / ``artifacts`` /
    ``page_geometry`` blocks an assess job does, with ``features`` where
    ``assessment`` would be and no ``verdict`` at all.

    ``regions.llm_boxes`` is honoured here exactly as on `/assess`: it costs a
    presence-only scoring call plus the loop's calls, and it is the only way
    an `llm` feature produces geometry. ``analyze_document(scoring=False)``
    makes that call and then strips its judgement.
    """
    file_bytes = base64.b64decode(payload.get("file_b64") or payload["image_b64"])
    features = [CriterionInput.model_validate(c) for c in payload["criteria"]]
    options = RegionsOptions.model_validate(payload.get("regions") or {})
    options.enabled = True

    doc = load_document_bytes(
        file_bytes,
        payload.get("filename"),
        payload.get("content_type"),
        keep_source=True,  # a native PDF's text hits need the file re-opened
    )
    result = await analyze_document(
        doc,
        features,
        payload.get("ocr", "auto"),
        regions=options,
        job_id=payload.get("job_id"),
        scoring=False,
    )
    result["status"] = "ok"
    return result


def _single_image_page(capture: dict[str, Any]) -> Optional[int]:
    """The index of the one image-bearing page, or None if there is not exactly one.

    Change detection compares two pictures. A multi-page PDF has no single
    "the image", and picking page 0 of each would silently diff a cover sheet
    against a cover sheet — so the honest answer for anything but a
    one-image document is "not applicable", said out loud in the note.
    """
    document = capture.get("document")
    if document is None:
        return None
    pages = [p.index for p in document.pages if p.image_bgr is not None]
    return pages[0] if len(pages) == 1 else None


async def _run_diffs(
    request: CompareRequest,
    subject_capture: dict[str, Any],
    example_captures: list[dict[str, Any]],
    example_results: list[dict[str, Any]],
    notes: list[str],
) -> dict[str, list[Region]]:
    """Diff the subject against every live single-image example.

    Writes ``example_results[i]["diff"]`` in place and returns the regions in
    the SUBJECT's ORIGINAL page pixels, keyed by the synthetic criterion name
    so ``write_compare_artifacts`` can file and render them.

    Three ways an example is skipped, each with its own note, because
    "aligned: false" and "we never looked" are different facts:
      * ``pre_generated_analysis`` — a stored analysis is JSON, not pixels;
      * either side is not a single-image document;
      * the alignment itself failed (that one comes back from
        ``detect_changes`` as ``aligned: false`` with its own note).
    """
    subject_page = _single_image_page(subject_capture)
    subject_geometries = subject_capture.get("geometries") or {}
    regions_by_criterion: dict[str, list[Region]] = {}

    if subject_page is None or subject_page not in subject_geometries:
        note = (
            "regions.diff was requested but the SUBJECT is not a single-image "
            "document (change detection compares two pictures), so no diff was "
            "run for any example."
        )
        logger.info("run_compare: %s", note)
        notes.append(note)
        for entry in example_results:
            entry["diff"] = {"aligned": False, "inliers": 0, "regions": [], "note": note}
        return regions_by_criterion

    geometry = subject_geometries[subject_page]
    subject_working = dict(working_page_images(subject_capture["document"]))

    for index, (example, capture) in enumerate(zip(request.examples, example_captures)):
        if example.pre_generated_analysis is not None:
            note = (
                f"Example {index} was supplied as pre_generated_analysis, which "
                "carries no pixels — change detection needs the image, so this "
                "example was not diffed. Send it as data + type to diff it."
            )
            example_results[index]["diff"] = {
                "aligned": False, "inliers": 0, "regions": [], "note": note
            }
            notes.append(note)
            continue

        example_page = _single_image_page(capture)
        if example_page is None:
            note = (
                f"Example {index} is not a single-image document, so it was not "
                "diffed against the subject."
            )
            example_results[index]["diff"] = {
                "aligned": False, "inliers": 0, "regions": [], "note": note
            }
            notes.append(note)
            continue

        reference = dict(working_page_images(capture["document"])).get(example_page)
        outcome = await asyncio.to_thread(
            detect_changes, reference, subject_working.get(subject_page)
        )
        diff_jobs.labels(aligned="true" if outcome.aligned else "false").inc()

        # Working → original page pixels, the same transform every other
        # region source goes through.
        regions = [
            rescale_region(
                Region(
                    page=geometry.page,
                    kind=item["kind"],
                    points=item["points"],
                    label=diff_criterion_name(index),
                    score=item["score"],
                    source="diff",
                    attrs=dict(item["attrs"]),
                ),
                geometry.working_scale,
                geometry,
            )
            for item in outcome.regions
        ]
        if regions:
            regions_by_criterion[diff_criterion_name(index)] = regions

        block = outcome.as_dict(DIFF_MAX_REGIONS)
        block["regions"] = [r.as_dict() for r in regions[:INLINE_REGIONS_MAX]]
        block["regions_truncated"] = len(regions) > INLINE_REGIONS_MAX
        block["changes"] = {
            kind: sum(1 for r in regions if r.attrs.get("change") == kind)
            for kind in ("added", "removed", "changed")
        }
        example_results[index]["diff"] = block

    return regions_by_criterion


def _attach_example_artifacts(
    analysis: dict[str, Any], per_criterion: dict[str, Any]
) -> None:
    """Point an example's per-criterion results at its own layer files.

    The example was analysed with no job id of its own, so
    ``attach_regions`` left every ``artifacts`` block None. Now that the
    files exist under the subject's job, fill them in — otherwise a caller
    reading ``example_analysis`` sees regions with no way to look at them.
    """
    target = analysis.get("assessment", {}).get("per_criterion_scores")
    if not isinstance(target, dict):
        return
    for name, block in per_criterion.items():
        entry = target.get(name)
        if isinstance(entry, dict):
            entry["artifacts"] = block
