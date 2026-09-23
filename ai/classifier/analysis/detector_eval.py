"""The open-vocabulary detector as an evaluation path.

``detector.client`` is the transport — one HTTP call, boxes back. This module
is what the pipeline does with it: ask about every label on every page image,
survive a service that answers for some pages and not others, and score a
criterion from the boxes alone so a `has bicycle` criterion costs no tokens.

    _run_detector()       — one pass over the page images, every label, with
                            the caller-facing note when it could not answer.
    _score_from_detector() — boxes → a per-criterion result, no LLM call.

**Failure is never fatal.** A detector that cannot be reached becomes a
sentence in ``artifacts.notes`` and a fall-through to the behaviour of a build
with no detector at all.

Process flow position: step 4.5 of ``analysis.pipeline.analyze_document``,
after the CV detectors and before the LLM call.
"""

from common.vision import PageGeometry, Region

from config import DETECTOR_MIN_SCORE, DETECTOR_STRONG_SCORE
# Imported as a module, not by name: `detector_client.is_configured()` reads
# DETECTOR_URL at call time, which is what lets a test point it at a stub.
from detector import client as detector_client
from logger import logger
from utils import verdict_from_score as _verdict_from_score

async def _run_detector(
    working_images: list[tuple[int, object]],
    labels: list[str],
    geometries: dict[int, PageGeometry],
    stats: "detector_client.DetectorStats",
) -> tuple[dict[str, list[Region]] | None, str | None]:
    """Ask the detector service about every label, on every page image.

    One call per page, carrying all the labels for that page — the detector
    embeds each label as its own query, so N labels in one call cost about
    what N calls would, minus N-1 image encodes and round trips.

    Args:
        working_images: ``[(page_index, bgr_image), ...]``, already resized.
        labels:         Criterion names to look for.
        geometries:     Page frames, for the working → original rescale.
        stats:          Accumulator, mutated in place.

    Returns:
        ``({label: [Region, ...]}, None)`` on success — every label present,
        an empty list meaning "looked, found none". ``(None, note)`` when the
        service could not answer at all; the note is caller-facing prose for
        ``artifacts.notes``. A page that fails does not abandon the rest: the
        result is whatever the reachable pages said, and only a total failure
        returns ``None``.
    """
    found: dict[str, list[Region]] = {label: [] for label in labels}
    pages = [(i, img) for i, img in working_images if i in geometries]
    if not pages:
        return found, (
            "regions.detector was requested but this document has no page "
            "images to run it on, so no source=\"detector\" regions were produced."
        )

    note: str | None = None
    answered = 0
    for index, image in pages:
        try:
            page_found = await detector_client.detect_page(
                image, labels, geometries[index], stats=stats
            )
        except detector_client.DetectorUnavailable as exc:
            # First failure is the one worth reporting; a wedged service
            # fails identically on every page and 20 copies of the same
            # sentence is not 20 pieces of information.
            note = note or str(exc)
            logger.warning("_run_detector: page %d — %s", index, exc)
            continue
        answered += 1
        for label, regions in page_found.items():
            found.setdefault(label, []).extend(regions)

    if answered == 0:
        return None, note
    if note:
        # Some pages answered and some did not. The per-call note is written
        # for total failure ("no regions were produced for this job"), which
        # would be a lie here, so say what actually happened instead.
        note = (
            f"The detector answered for {answered} of {len(pages)} page(s); "
            f"the rest were skipped. First failure: {note}"
        )
    return found, note


def _score_from_detector(name: str, regions: list[Region]) -> dict:
    """Score one criterion from the detector's boxes alone — no LLM call.

    This is the point of the detector for a `cv` criterion with no OpenCV
    detector behind it: `has bicycle` used to cost a vision-model call and
    come back with no geometry, and now costs neither.

    The rubric mirrors the rest of the service. A box at or above
    ``DETECTOR_STRONG_SCORE`` is a clear PASS (10); a box above the floor but
    below it is a real finding that should not be built on (7 — PASS, same
    meaning as everywhere else); nothing at all is a FAIL (1).

    **No LLM second opinion on a negative.** A criterion the detector looked
    for and did not find is FAILed here and NOT passed to the model. Asking
    both would charge a token cost for every absent feature — which is most
    of them on most documents — and would make the answer depend on which of
    two disagreeing sources is consulted last. If you want the model's
    opinion, ask for it: give the criterion ``type: "llm"``.

    Args:
        name:    Criterion name (also the detector label).
        regions: That criterion's boxes, already filtered to
                 ``DETECTOR_MIN_SCORE``.

    Returns:
        A per-criterion result dict with ``method="detector"``.
    """
    if not regions:
        return {
            "score": 1,
            "verdict": "FAIL",
            # The floor is what we asked for, so "nothing above it" is a
            # statement about the floor as much as about the document.
            "confidence": int(round((1.0 - DETECTOR_MIN_SCORE) * 100)),
            "method": "detector",
            "reason": (
                f"The open-vocabulary detector found no '{name}' at or above "
                f"the {DETECTOR_MIN_SCORE:.2f} confidence floor. Lower "
                "DETECTOR_MIN_SCORE, or give the criterion type=\"llm\" to have "
                "the vision model judge it instead."
            ),
            "detail": {
                "detector_matches": 0,
                "min_score": DETECTOR_MIN_SCORE,
                "pages": [],
            },
        }

    best = max(float(r.score or 0.0) for r in regions)
    pages = sorted({r.page for r in regions})
    score = 10 if best >= DETECTOR_STRONG_SCORE else 7
    return {
        "score": score,
        "verdict": _verdict_from_score(score),
        "confidence": int(round(min(1.0, best) * 100)),
        "method": "detector",
        "reason": (
            f"The open-vocabulary detector found {len(regions)} '{name}' "
            f"box(es) on page(s) {pages}; best confidence {best:.2f} "
            f"(≥{DETECTOR_STRONG_SCORE:.2f} scores 10, above the "
            f"{DETECTOR_MIN_SCORE:.2f} floor scores 7)."
        ),
        "detail": {
            "detector_matches": len(regions),
            "best_score": round(best, 4),
            "min_score": DETECTOR_MIN_SCORE,
            "pages": pages,
        },
    }
