"""The core document analysis pipeline — the nine steps, in order.

Everything a request asks for is normalised into a
``common.documents.Document`` (pages that carry an image, a text layer, or
both) before any criterion runs, so nothing below this line branches on the
uploaded file type. This module is the ORCHESTRATION; each step's actual work
lives in a sibling module, which is what keeps this file readable:

  1. Validate the first page image's dimensions        analysis.loading
  2. Decide whether OCR is needed, and run it          analysis.ocr
  3. Resize page images to <=1000px                    analysis.geometry
  4. Run CV detectors for type="cv" criteria           analysis.cv_eval
     (worst page wins on a multi-page document)
  4.5 Ask the open-vocabulary detector about the       analysis.detector_eval
     criteria no OpenCV detector can answer, and
     about where the LLM's presence criteria are
  5. Run deterministic text matching                   analysis.text_eval
  6. One LLM call for type="llm" criteria, with ONE    analysis.llm_eval,
     page image plus the extracted text                llm.prompts, llm.client
  6.5 The bounding-box enforcement loop, when          llm.boxes
     `regions.llm_boxes` is set — extra calls,
     never a change to a score
  7. Merge, clamp, resolve dependencies, weight        llm.validate,
                                                       analysis.weighting
  8. Build the result dict (image_info + document_info)
  9. Regions: rescale, write the artifact directory,   regions.artifacts,
     attach a bounded inline copy                      regions.collect

  analyze_document() — the pipeline itself.
  analyze_upload()   — thin wrapper for multipart UploadFile inputs.
  analyze_input()    — thin wrapper for DocumentInput (JSON body) inputs.
  resolve_example()  — return a pre-generated analysis or analyse live; used
                       in /assess/compare to avoid redundant calls.
  _feature_entry()   — strip the judgement out of a result, for `/locate`.

Step 9 is strictly additive: it reads the results the scoring paths already
produced and only ADDS keys, so turning regions on can never change a score or
a verdict.

One-image-per-prompt constraint: the vision model (muse-glimmer) is served
WITHOUT ``--limit-mm-per-prompt``, so vLLM accepts at most ONE image per
request. A 12-page PDF therefore contributes exactly one page image to the
prompt (plus all of its text) — see ``analysis.llm_eval._pick_prompt_page``.

Process flow position: the top of the analysis package. Called by
``jobs.runners`` (run_assess, run_compare, run_locate) after the job is
dequeued.
"""

import asyncio

from fastapi import HTTPException, UploadFile

from common.documents import Document, apply_ocr
from common.vision import Region

from analysis.cv_eval import _run_cv_criterion
from analysis.detector_eval import _run_detector, _score_from_detector
from analysis.geometry import _page_geometries, _resized_page_images
from analysis.llm_eval import _document_text_for_prompt, _pick_prompt_page
from analysis.loading import (
    _load_bytes_from_input,
    _validate_image_dimensions,
    load_document_bytes,
    validate_content_type,
)
from analysis.ocr import _needs_ocr, get_ocr_engine
from analysis.text_eval import _evaluate_text_criterion
from analysis.weighting import (
    _skipped_result,
    apply_dependencies,
    compute_weighted_score,
)
from api.schemas import CriterionInput, DocumentInput, ExampleInput, RegionsOptions
from config import OCR_ENGINE, OCR_MIN_NATIVE_CHARS, TEXT_CHAR_BUDGET
from cv import get_detector
# Imported as a module, not by name: `detector_client.is_configured()` reads
# DETECTOR_URL at call time, which is what lets a test point it at a stub.
from detector import client as detector_client
from llm import boxes as llm_boxes
from llm.client import call_vllm, encode_image_to_base64
from llm.prompts import build_llm_prompt
from llm.validate import validate_and_clamp
from logger import logger
from regions.artifacts import write_region_artifacts
from regions.collect import attach_regions

async def analyze_document(
    doc: Document,
    criteria: list[CriterionInput],
    ocr_mode: str = "auto",
    *,
    regions: RegionsOptions | None = None,
    job_id: str | None = None,
    scoring: bool = True,
    capture: dict | None = None,
) -> dict:
    """Run the full assessment pipeline on a loaded Document.

    This is the central function that all entry points (analyze_upload,
    analyze_input, locate.run_locate) ultimately call.

    Pipeline steps:
      1. Validate the first page image's dimensions (image-bearing kinds only).
      2. Run OCR when it is needed and available (see _needs_ocr).
      3. Resize page images to ≤1000px on the long side.
      4. Run CV detectors for type="cv" criteria — worst page wins; criteria
         are SKIPPED when the document has no page images (txt/docx).
      4.5 Run the open-vocabulary detector for type="detector" criteria and —
         when `regions.detector` is set — for cv criteria with no OpenCV
         detector (scored from the boxes, no LLM call) and for llm presence
         criteria (localised only; the model still scores them). Degrades to
         the pre-detector behaviour when it is off or unreachable.
      5. Run deterministic text matching for type="text" criteria.
      6. One LLM call for type="llm" criteria (plus any cv fallbacks), with
         ONE page image and the extracted document text.
      6.5 When `regions.llm_boxes` is set: the bounding-box enforcement loop
         (llm/boxes.py) for every llm presence criterion the model scored at
         or above LLM_BBOX_PRESENCE_MIN. Extra calls, extra regions, extra
         `localization` — never a changed score.
      7. Merge all three, clamp, resolve dependencies, compute the weighted score.
      8. Assemble the result dict (image_info kept for compatibility, plus the
         richer document_info).
      9. When regions were asked for: rescale what the detectors and matchers
         located, write the artifact directory, and attach a bounded inline
         copy. This step only ADDS keys — it cannot move a score.

    Args:
        doc:      Loaded document (from load_document_bytes).
        criteria: List of CriterionInput objects defining what to assess.
        ocr_mode: "auto" | "always" | "never" — see models.CompareRequest.ocr.
        regions:  Region/layer options, or None for "no regions" (the
                  default). Writing the artifact directory additionally needs
                  ``job_id``; without one the regions are still collected and
                  returned inline, just not written to disk.
        job_id:   The job the artifact directory is named after.
        scoring:  False for `/locate`: no weighted score, no verdict — the
                  result carries ``features`` instead of ``assessment``. The
                  scoring call itself is skipped too, UNLESS
                  ``regions.llm_boxes`` is set and the document has a page
                  image: the enforcement loop needs a presence score to gate
                  on, so the call is made and its judgement then dropped.
        capture:  An out-parameter, for the compare flow. When given it is
                  filled with ``document`` / ``geometries`` / ``region_map`` /
                  ``localizations`` — the objects ``run_compare`` needs to
                  render an example's layers or diff it against the subject,
                  and which the JSON result cannot carry (numpy pixels, and
                  Regions rather than dicts).

    Returns:
        dict with image_info, document_info, and either assessment + verdict
        (scoring) or features (locate); plus artifacts + page_geometry when
        regions were requested.
    """
    want_regions = bool(regions and regions.enabled)
    logger.info(
        "analyze_document: kind=%s pages=%d ocr=%s scoring=%s regions=%s criteria=%s",
        doc.kind,
        len(doc.pages),
        ocr_mode,
        scoring,
        (",".join(regions.layers) or "json-only") if want_regions else "off",
        [f"{c.name}({c.type})" for c in criteria],
    )

    # Step 1 — reject page images that are too small for reliable assessment.
    # Only the first image-bearing page is checked: a document is rejected for
    # being a thumbnail, not for having one small trailing page.
    first_image_page = next((p for p in doc.pages if p.image_bgr is not None), None)
    if first_image_page is not None:
        _validate_image_dimensions(first_image_page.width, first_image_page.height)

    # Step 2 — OCR. Blocking (ONNX inference), so it runs in a worker thread
    # to keep the event loop free for other concurrent jobs.
    # The needs-check comes first so a native PDF / txt / docx never pays the
    # ~1s cost of loading the ONNX models it has no use for.
    ocr_ran = False
    ocr_pages = 0
    if _needs_ocr(doc, criteria, ocr_mode):
        engine = get_ocr_engine()
        if engine is None:
            logger.warning(
                "analyze_document: OCR wanted but unavailable (engine=%s) — text "
                "criteria on image-only pages will fail",
                OCR_ENGINE,
            )
        else:
            effective_mode = "always" if ocr_mode == "always" else "auto"
            logger.info("analyze_document: running OCR (mode=%s)", effective_mode)
            ocr_pages = await asyncio.to_thread(
                apply_ocr,
                doc,
                engine,
                mode=effective_mode,
                min_native_chars=OCR_MIN_NATIVE_CHARS,
            )
            ocr_ran = True
            logger.info("analyze_document: OCR filled %d page(s)", ocr_pages)

    # Step 3 — one resized copy of each page image, shared by CV and the LLM
    working_images = _resized_page_images(doc)

    # Step 4/5/6 — partition criteria by evaluation path.
    # type="cv"       → OpenCV detector (deferred to 4.5 if no detector matches)
    # type="text"     → deterministic text match, no tokens
    # type="detector" → the open-vocabulary detector service, explicitly asked for
    # type="llm"      → the vision model
    cv_criteria = [c for c in criteria if c.type == "cv"]
    text_criteria = [c for c in criteria if c.type == "text"]
    explicit_detector = [c for c in criteria if c.type == "detector"]
    llm_criteria = [c for c in criteria if c.type == "llm"]

    # Is the open-vocabulary detector in play at all? Two ways in: a
    # criterion that names it outright (type="detector" — honoured whether or
    # not layers were asked for, because the caller chose the path), or
    # `regions.detector`, which is what re-routes the IMPLICIT uses: a cv
    # criterion with no OpenCV detector, and localisation for llm presence
    # criteria. Either way it needs DETECTOR_URL — an unconfigured service is
    # the same as one that is off.
    want_detector = detector_client.is_configured() and (
        bool(explicit_detector) or (want_regions and bool(regions and regions.detector))
    )

    # The coordinate frames every region is expressed in. Built once, here,
    # because the CV, text and detector paths all need them — and left as
    # None when nothing wants geometry, which is what switches the whole
    # locate pass off downstream. The detector needs them even with layers
    # off: its boxes come back in working pixels and have to be rescaled
    # before they mean anything.
    geometries = (
        _page_geometries(doc, working_images)
        if (want_regions or want_detector)
        else None
    )
    region_map: dict[str, list[Region]] = {}

    # Step 4 — CV detectors
    _cv_per_criterion: dict = {}
    cv_fallbacks: list[CriterionInput] = []
    for c in cv_criteria:
        detector = get_detector(c.name)
        if not detector:
            # No registered OpenCV detector. Held back rather than sent
            # straight to the LLM: step 4.5 gives the open-vocabulary
            # detector first refusal, and only what it cannot answer (or
            # everything, when it is off or unreachable) reaches the model.
            logger.info(
                "analyze_document: no CV detector for '%s' — deferring to step 4.5",
                c.name,
            )
            cv_fallbacks.append(c)
            continue
        if not working_images:
            # .txt / .docx have no rendered surface — not a failure, just not
            # applicable, so the criterion is excluded from the weighting.
            logger.info(
                "analyze_document: '%s' skipped — document has no page images", c.name
            )
            _cv_per_criterion[c.name] = _skipped_result(
                f"Skipped - document has no page images ({doc.kind} documents are "
                "text-only, so OpenCV criteria cannot be evaluated)."
            )
            continue
        logger.info("analyze_document: CV detector running for '%s'", c.name)
        _cv_per_criterion[c.name], cv_regions = _run_cv_criterion(
            detector, c.name, working_images, geometries
        )
        if want_regions:
            region_map[c.name] = cv_regions

    # Compute CV overall verdict and score immediately after all detectors have run,
    # then build cv_assessment with overall_verdict and overall_score first so they
    # appear at the top of the dict. SKIPPED entries contribute to neither.
    scored_cv = {
        k: v for k, v in _cv_per_criterion.items() if v.get("verdict") != "SKIPPED"
    }
    if scored_cv:
        cv_scores = [
            r["score"] for r in scored_cv.values() if isinstance(r.get("score"), (int, float))
        ]
        cv_failures = sum(1 for r in scored_cv.values() if r.get("verdict") == "FAIL")
        cv_verdict = (
            "FAIL" if cv_failures >= 2 else ("MARGINAL" if cv_failures == 1 else "PASS")
        )
        cv_assessment: dict = {
            "overall_verdict": cv_verdict,
            "overall_score": round(sum(cv_scores) / len(cv_scores)) if cv_scores else 5,
            "per_criterion_scores": _cv_per_criterion,
        }
        logger.debug(
            "analyze_document: cv_assessment verdict=%s score=%s criteria=%s",
            cv_assessment["overall_verdict"],
            cv_assessment["overall_score"],
            {k: v["verdict"] for k, v in _cv_per_criterion.items()},
        )
    else:
        cv_assessment: dict = (
            {"overall_verdict": None, "overall_score": None,
             "per_criterion_scores": _cv_per_criterion}
            if _cv_per_criterion
            else {}
        )

    # Step 4.5 — the open-vocabulary detector (ai/detector).
    #
    # Three jobs, in one pass over the page images:
    #   * SCORE the criteria that name it (type="detector") and the cv
    #     criteria with no OpenCV detector — a `has bicycle` criterion should
    #     not need a vision LLM.
    #   * LOCALISE llm presence criteria: the model still scores them, the
    #     detector only says where.
    #   * Degrade to exactly the old behaviour when it is off, unconfigured,
    #     or unreachable — the deferred cv criteria go to the LLM as before.
    _detector_per_criterion: dict = {}
    detector_stats: detector_client.DetectorStats | None = None
    detector_notes: list[str] = []

    # `takeover` is `regions.detector`: the flag that hands the detector the
    # IMPLICIT work. Without it, only a criterion that named the path
    # (type="detector") uses the service at all.
    takeover = bool(want_regions and regions and regions.detector)
    detector_scored = list(explicit_detector) + (cv_fallbacks if takeover else [])
    detector_located = (
        [c for c in llm_criteria if c.hint in ("presence", "auto")]
        if takeover
        else []
    )
    if takeover and not detector_client.is_configured():
        # Asked for and not available. Said plainly in the manifest rather
        # than silently dropped — an empty `detector` block with no
        # explanation is how a caller ends up debugging our configuration.
        detector_notes.append(
            "regions.detector was requested but DETECTOR_URL is not configured "
            "on this container, so no source=\"detector\" regions were produced."
        )

    resolved_by_detector: set[str] = set()
    if want_detector and (detector_scored or detector_located):
        detector_stats = detector_client.DetectorStats()
        found, note = await _run_detector(
            working_images,
            [c.name for c in detector_scored + detector_located],
            geometries or {},
            detector_stats,
        )
        if note:
            detector_notes.append(note)
            detector_stats.errors.append(note)
        if found is not None:
            for c in detector_scored:
                hits = found.get(c.name, [])
                _detector_per_criterion[c.name] = _score_from_detector(c.name, hits)
                resolved_by_detector.add(c.name)
                if want_regions:
                    region_map[c.name] = hits
            if want_regions:
                for c in detector_located:
                    # The LLM still scores these in step 6; the detector only
                    # says where. Step 6.5's enforcement loop APPENDS its own
                    # boxes to the same list when regions.llm_boxes is set,
                    # and cross-checks them against these with IoU.
                    region_map[c.name] = found.get(c.name, [])

    # Whatever the detector did not resolve goes to the vision model: the
    # deferred cv criteria exactly as before this service existed, and an
    # explicit type="detector" criterion too — a missing dependency should
    # degrade the answer, not fail the job.
    for c in cv_fallbacks + explicit_detector:
        if c.name in resolved_by_detector:
            continue
        logger.warning(
            "analyze_document: '%s' was not resolved by a detector — falling "
            "back to the LLM",
            c.name,
        )
        llm_criteria.append(c)

    # Step 5 — text criteria (deterministic, no tokens)
    _text_per_criterion: dict = {}
    for c in text_criteria:
        _text_per_criterion[c.name], text_regions = _evaluate_text_criterion(
            doc, c, geometries
        )
        if want_regions:
            region_map[c.name] = text_regions

    # Step 6 — one LLM call for all LLM-bound criteria (type="llm" + any cv
    # fallbacks). Skipped entirely when every criterion resolved via CV/text.
    document_text, text_truncated = _document_text_for_prompt(doc)
    prompt_page = _pick_prompt_page(working_images, _text_per_criterion)
    want_llm_boxes = bool(want_regions and regions and regions.llm_boxes)

    # `/locate` suppresses scoring, but the enforcement loop needs a presence
    # score to gate on — there is nothing to locate about a feature the model
    # says is absent. So when llm_boxes is on and there IS a page image, the
    # scoring call runs anyway and its score is used as the gate and then
    # thrown away (`_feature_entry` strips score/verdict/confidence). Without
    # llm_boxes, or with no image, the call is skipped as before: it would be
    # spent on an answer nobody reads.
    locate_presence_pass = bool(
        llm_criteria and not scoring and want_llm_boxes and prompt_page is not None
    )
    image_b64: str | None = None

    if llm_criteria and not scoring and not locate_presence_pass:
        why = (
            "this document has no page image to locate against"
            if want_llm_boxes
            else "regions.llm_boxes was not requested"
        )
        logger.info(
            "analyze_document: scoring suppressed — skipping the LLM call for "
            "%d criteria (%s)", len(llm_criteria), why,
        )
        llm_assessment = {
            "overall_verdict": None,
            "overall_score": None,
            "per_criterion_scores": {
                c.name: {
                    "method": "llm",
                    "reason": (
                        f"No LLM call was made for this feature: {why}. Set "
                        'regions={"enabled":true,"llm_boxes":true} on a document '
                        "with a page image to run the bounding-box enforcement "
                        "loop."
                    ),
                }
                for c in llm_criteria
            },
        }
    elif llm_criteria:
        if prompt_page is not None:
            page_image = next(img for idx, img in working_images if idx == prompt_page)
            image_b64 = encode_image_to_base64(page_image)
        logger.info(
            "analyze_document: LLM call — %d criteria, image=%s, document_text=%d chars%s",
            len(llm_criteria),
            f"page {prompt_page}" if prompt_page is not None else "none (text-only)",
            len(document_text),
            " (truncated)" if text_truncated else "",
        )
        llm_raw = await call_vllm(
            build_llm_prompt(
                image_b64,
                llm_criteria,
                document_text=document_text,
                document_kind=doc.kind,
                page_index=prompt_page,
                page_count=len(doc.pages),
                text_truncated=text_truncated,
            )
        )
        for val in (
            llm_raw.get("assessment", {}).get("per_criterion_scores", {}).values()
        ):
            if isinstance(val, dict):
                val["method"] = "llm"
        # Validate and clamp LLM assessment using only the LLM-bound criteria.
        llm_assessment = validate_and_clamp(llm_raw.get("assessment", {}), llm_criteria)
    else:
        logger.info(
            "analyze_document: all criteria resolved via CV/text — skipping LLM call"
        )
        llm_assessment = {
            "overall_verdict": None,
            "overall_score": None,
            "per_criterion_scores": {},
        }

    # Step 6.5 — the LLM bounding-box enforcement loop (llm/boxes.py).
    #
    # Deliberately AFTER the scoring call and deliberately separate from it:
    # the loop reads the score to decide whether there is anything to locate,
    # and can only add keys. A criterion whose every attempt is rejected keeps
    # exactly the score and verdict it had a line ago.
    #
    # Its regions are APPENDED to whatever the detector already put in the map
    # for the same criterion, so a criterion localised by both comes back with
    # sources ["detector", "llm"] and the two can be compared (the loop
    # records the IoU as attrs.detector_iou).
    localizations: dict[str, dict] = {}
    if want_llm_boxes and llm_criteria and geometries and prompt_page in geometries:
        original_page = doc.page(prompt_page)
        llm_regions, localizations = await llm_boxes.locate_criteria(
            llm_criteria,
            llm_assessment.get("per_criterion_scores", {}),
            image_b64=image_b64,
            original_image=original_page.image_bgr if original_page else None,
            geometry=geometries[prompt_page],
            detector_regions=region_map,
            # The same ≤1000-px pixels image_b64 was encoded from, so the loop
            # can draw its coordinate grid on them for the ask.
            working_image=next(
                (img for idx, img in working_images if idx == prompt_page), None
            ),
        )
        for name, found in llm_regions.items():
            region_map.setdefault(name, []).extend(found)
    elif want_llm_boxes:
        logger.info(
            "analyze_document: regions.llm_boxes requested but there is no page "
            "image and no LLM criterion to locate on it — localization is empty"
        )

    # Step 7 — build combined assessment (CV + text + LLM) and compute the
    # weighted overall score across ALL criteria. Each path stays clean in its
    # own keys; combined is the single source for the weighted breakdown.
    combined_raw = {
        "overall_verdict": "...",
        "overall_score": 0,
        "per_criterion_scores": {
            **cv_assessment.get("per_criterion_scores", {}),
            **_detector_per_criterion,
            **_text_per_criterion,
            **llm_assessment.get("per_criterion_scores", {}),
        },
    }
    if scoring:
        combined_assessment = validate_and_clamp(combined_raw, criteria)
        combined_assessment = apply_dependencies(combined_assessment, criteria)
        combined_assessment = compute_weighted_score(combined_assessment, criteria)
    else:
        # /locate: the per-criterion entries are kept as they are (a CV
        # detector's `detail` is genuinely useful — "Green coverage: 14.2%"),
        # but nothing is clamped, weighted, or turned into a verdict, because
        # this endpoint makes no judgement.
        combined_assessment = combined_raw

    # Step 8 — assemble the final response dict.
    # combined_verdict is derived directly from the weighted_score_breakdown
    # final_score so the connection between score and verdict is explicit.
    breakdown = combined_assessment.get("weighted_score_breakdown", {})
    combined_verdict = combined_assessment.get("overall_verdict", "PASS")
    logger.debug(
        "analyze_document: combined breakdown final_score=%s → verdict=%s",
        breakdown.get("final_score"),
        combined_verdict,
    )

    # image_info keeps the pre-document shape so existing consumers (and
    # stored pre_generated_analysis blobs) keep working: page 0's image when
    # there is one, zeros otherwise.
    image_info = {
        "width": first_image_page.width if first_image_page else 0,
        "height": first_image_page.height if first_image_page else 0,
        "format": doc.content_type,
        "size_bytes": doc.size_bytes,
    }

    result = {
        "image_info": image_info,
        "document_info": {
            "kind": doc.kind,
            "filename": doc.filename,
            "page_count": len(doc.pages),
            "truncated_pages": doc.truncated_pages,
            "pages": [
                {
                    "index": p.index,
                    "width": p.width,
                    "height": p.height,
                    "text_source": p.text_source,
                    "ocr_confidence": p.ocr_confidence,
                    "text_chars": p.text_chars(),
                }
                for p in doc.pages
            ],
            "ocr": {
                "mode": ocr_mode,
                "engine": OCR_ENGINE or "none",
                "ran": ocr_ran,
                "pages_recognised": ocr_pages,
            },
            "text_sent_to_llm": {
                "chars": len(document_text) if llm_criteria else 0,
                "truncated": text_truncated if llm_criteria else False,
                "budget": TEXT_CHAR_BUDGET,
            },
            "llm_image_page": prompt_page if (llm_criteria and scoring) else None,
            # What the open-vocabulary detector did for this job. Present
            # even when it was never used, because "configured: false" and
            # "configured: true, calls: 0" are different facts and a caller
            # debugging an empty `by_label` needs to tell them apart.
            "detector": {
                **detector_client.status(),
                **(
                    detector_stats.as_dict()
                    if detector_stats is not None
                    else {"calls": 0, "detections": 0, "elapsed_ms": 0.0}
                ),
                "requested": bool(regions and regions.detector),
                "notes": detector_notes,
            },
        },
    }
    if scoring:
        # combined assessment with all criteria and the weighted score
        result["assessment"] = combined_assessment
        result["verdict"] = combined_verdict
    else:
        # /locate: same per-criterion entries, minus every judgement.
        result["features"] = {
            name: _feature_entry(entry)
            for name, entry in combined_assessment["per_criterion_scores"].items()
        }

    # Step 9 — regions. Purely additive: nothing above this line is re-read
    # or recomputed, so a request with regions on and one with regions off
    # produce identical scores and verdicts for the same document.
    if want_regions:
        target = (
            result["assessment"]["per_criterion_scores"]
            if scoring
            else result["features"]
        )
        # Every criterion gets an entry, so "no regions" is visible as an
        # empty list rather than a missing key the caller has to interpret.
        for c in criteria:
            region_map.setdefault(c.name, [])

        artifacts: dict | None = None
        per_criterion_artifacts: dict[str, dict | None] = {}
        if job_id:
            artifacts, per_criterion_artifacts = await asyncio.to_thread(
                write_region_artifacts,
                job_id,
                doc,
                geometries or {},
                region_map,
                regions,
                criteria,
                detector_stats.as_dict() if detector_stats is not None else None,
                detector_notes,
                localizations,
            )
        else:
            # No job id (a live compare example, a direct library call): the
            # regions are still returned inline, there is just nowhere to
            # name a directory after.
            logger.debug("analyze_document: regions collected without a job id — "
                         "no artifact directory written")

        attach_regions(target, region_map, per_criterion_artifacts, localizations)
        result["page_geometry"] = [
            (geometries or {})[i].as_dict() for i in sorted(geometries or {})
        ]
        result["artifacts"] = artifacts

        # The compare flow renders diff and example layers into this same
        # directory AFTER the subject's analysis finishes, and needs the
        # pieces it cannot recover from the result: the loaded document (for
        # pixels) and the region map in Region objects rather than dicts.
        if capture is not None:
            capture.update(
                document=doc,
                geometries=geometries or {},
                region_map=region_map,
                localizations=localizations,
            )
    elif capture is not None:
        # Regions were off, but a caller may still want the pixels — change
        # detection needs the example's image and nothing else.
        capture.update(document=doc, geometries={}, region_map={}, localizations={})

    logger.info(
        "analyze_document: returning verdict=%s overall_score=%s regions=%d",
        result.get("verdict", "n/a (locate)"),
        combined_assessment.get("overall_score", "n/a"),
        sum(len(v) for v in region_map.values()),
    )
    return result


def _feature_entry(entry: dict) -> dict:
    """Strip the judgement out of a per-criterion result for `/locate`.

    ``score`` / ``verdict`` / ``confidence`` come out; ``method``, ``detail``
    and ``reason`` stay, because "Green coverage: 14.2% of image" is a fact
    about where the thing is, not a verdict on it. ``attach_regions`` then
    adds ``regions`` / ``artifacts`` / ``localization`` to the same dict.

    An `llm` feature run with ``regions.llm_boxes`` is the case that makes
    this a real filter rather than a formality: the enforcement loop needs a
    presence score to gate on, so the scoring call DID happen and the entry
    arrives here carrying a score and a verdict. They are removed — `/locate`
    makes no judgement — while ``localization`` (every attempt, the accepted
    one, the call count) stays, because that is geometry, not opinion.
    """
    if not isinstance(entry, dict):
        return {"method": "unknown"}
    return {
        key: value
        for key, value in entry.items()
        if key not in ("score", "verdict", "confidence")
    }


async def analyze_upload(
    upload: UploadFile,
    criteria: list[CriterionInput],
    ocr_mode: str = "auto",
    *,
    regions: RegionsOptions | None = None,
    job_id: str | None = None,
) -> dict:
    """Entry point for multipart file uploads (POST /assess via form data).

    Reads the uploaded file, validates the content type, loads it as a
    document, and delegates to analyze_document().

    Args:
        upload:   FastAPI UploadFile from a multipart/form-data request.
        criteria: Parsed list of CriterionInput objects.
        ocr_mode: "auto" | "always" | "never".
        regions:  Region/layer options, or None for no regions.
        job_id:   Job id, so the artifact directory can be named.

    Returns:
        Analysis result dict from analyze_document().
    """
    logger.info(
        "analyze_upload: filename=%s content_type=%s ocr=%s criteria=%s",
        upload.filename,
        upload.content_type,
        ocr_mode,
        [c.name for c in criteria],
    )

    # Validate the declared content type before reading the whole file; the
    # magic bytes decide the actual kind during load.
    validate_content_type(upload.content_type)

    contents = await upload.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty document file")

    logger.debug("analyze_upload: read %d bytes", len(contents))
    doc = load_document_bytes(
        contents,
        upload.filename,
        upload.content_type,
        keep_source=bool(regions and regions.enabled),
    )
    result = await analyze_document(
        doc, criteria, ocr_mode, regions=regions, job_id=job_id
    )
    logger.info(
        "analyze_upload: returning combined_verdict=%s", result["verdict"]
    )
    return result


async def analyze_input(
    img: DocumentInput,
    criteria: list[CriterionInput],
    ocr_mode: str = "auto",
    *,
    regions: RegionsOptions | None = None,
    job_id: str | None = None,
    capture: dict | None = None,
) -> dict:
    """Entry point for DocumentInput objects from a JSON request body.

    Loads the bytes from a base64 string or URL, detects the kind, and
    delegates to analyze_document().

    Args:
        img:      DocumentInput with data and type fields.
        criteria: List of CriterionInput objects.
        ocr_mode: "auto" | "always" | "never".
        regions:  Region/layer options, or None for no regions. In a compare
                  job this is passed for the SUBJECT only (and, when
                  `regions.examples` is set, for each live example too).
        job_id:   Job id, so the artifact directory can be named.
        capture:  Out-parameter passed straight to analyze_document — see
                  there.

    Returns:
        Analysis result dict from analyze_document().
    """
    data_repr = img.data[:80] if img.type == "url" else f"base64[{len(img.data)} chars]"
    logger.info(
        "analyze_input: type=%s data=%s ocr=%s criteria=%s",
        img.type,
        data_repr,
        ocr_mode,
        [c.name for c in criteria],
    )
    raw = await _load_bytes_from_input(img.data, img.type)
    filename = img.data.rsplit("/", 1)[-1][:120] if img.type == "url" else "inline"
    doc = load_document_bytes(
        raw, filename, None, keep_source=bool(regions and regions.enabled)
    )
    result = await analyze_document(
        doc, criteria, ocr_mode, regions=regions, job_id=job_id, capture=capture
    )
    logger.info(
        "analyze_input: returning combined_verdict=%s", result["verdict"]
    )
    return result


async def resolve_example(
    example: ExampleInput,
    criteria: list[CriterionInput],
    ocr_mode: str = "auto",
    *,
    regions: RegionsOptions | None = None,
    capture: dict | None = None,
) -> dict:
    """Return the analysis for a reference example, live or pre-generated.

    Used in /assess/compare to obtain an analysis for each reference document.
    If pre_generated_analysis is provided, it is returned immediately without
    any LLM call — this is the recommended pattern for stable reference
    documents to avoid redundant token usage.

    A pre-generated analysis is a JSON blob: it carries no pixels, so neither
    change detection nor example layers can be produced from one. That is why
    ``capture`` comes back empty for those, and why ``run_compare`` says so in
    the result rather than silently skipping them.

    Args:
        example:  ExampleInput including the document and optional prior analysis.
        criteria: The criteria to apply if a live analysis is needed.
        ocr_mode: "auto" | "always" | "never".
        regions:  Region options for this EXAMPLE's own analysis, set by
                  `run_compare` when `regions.examples` is on. None (the
                  default) still collects nothing; the example's document is
                  still captured, which is all change detection needs.
        capture:  Out-parameter — see analyze_document.

    Returns:
        Analysis result dict (same shape as analyze_document output).
    """
    pre_generated = example.pre_generated_analysis is not None
    logger.info(
        "resolve_example: type=%s weight=%s pre_generated=%s criteria=%s",
        example.type,
        example.weight,
        pre_generated,
        [c.name for c in criteria],
    )

    if pre_generated:
        # Skip the LLM entirely — use the cached result
        logger.info("resolve_example: using pre-generated analysis, skipping LLM call")
        return example.pre_generated_analysis

    # Analyse live — same path as a regular /assess call
    result = await analyze_input(
        DocumentInput(data=example.data, type=example.type),
        criteria,
        ocr_mode,
        regions=regions,
        capture=capture,
    )
    logger.info(
        "resolve_example: returning combined_verdict=%s", result["verdict"]
    )
    return result
