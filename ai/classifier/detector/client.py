"""Client for the open-vocabulary detector service (`ai/detector`).

The detector answers "where is X" for a free-text X. That is the one thing
this service could not do before: a `has bicycle` criterion had no OpenCV
detector, so it fell back to the vision LLM, which produced a score and no
geometry at all. With `DETECTOR_URL` set and `regions.detector` asked for,
the same criterion comes back with boxes — and, when it is a `cv` criterion,
with a score that cost no tokens.

    DetectorUnavailable   — the one exception this module raises. Every
                            caller catches it and degrades; see below.
    detect_page()         — one HTTP call: one page image, every label for
                            that page, boxes back in ORIGINAL page pixels.
    DetectorStats         — what the job reports about the calls it made.

**Failure is never fatal.** The detector is an optional enrichment on top of
a pipeline that already works. A connection refused, a timeout, a 503 while
the model loads, a garbled body — all of them become a
:class:`DetectorUnavailable`, which ``analysis.detector_eval`` turns into
an ``artifacts.notes`` line and a fall-through to the behaviour of a build with
no detector at all. A job must never fail because an enrichment did.

**Batching.** One call per page image, carrying every label wanted for that
page. The detector embeds each label as its own text query, so N labels in
one call cost about the same as N calls — but one call pays the image encode,
the HTTP round trip, and the GPU lock once instead of N times. Lists longer
than ``DETECTOR_MAX_LABELS_PER_CALL`` are split.

**Coordinates.** The page image sent is the ≤1000-px WORKING image (the one
the CV detectors and the LLM already see), so boxes come back in working
pixels and are rescaled into original page pixels here, through the same
``common.vision.rescale_region`` every other source uses. Sending the full
original instead would mean a second JPEG encode of a 12-megapixel photo per
page for no extra accuracy — the detector resizes to 1024 anyway.

Process flow position: called from ``analysis.detector_eval`` in step 4.5 of
the pipeline, after the CV detectors and before the LLM call.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import httpx

from common.vision import PageGeometry, Region, rescale_region

from config import (
    DETECTOR_MAX_LABELS_PER_CALL,
    DETECTOR_MIN_SCORE,
    DETECTOR_TIMEOUT_S,
    DETECTOR_URL,
)
from logger import logger


class DetectorUnavailable(RuntimeError):
    """The detector could not answer. Always caught, never propagated.

    Carries a caller-facing sentence — it ends up verbatim in
    ``artifacts.notes``, so it names the service and the reason without
    leaking a stack trace into a job result.
    """


@dataclass
class DetectorStats:
    """What the job did with the detector, for ``document_info.detector``.

    ``calls`` counts HTTP requests, not labels: a page with 20 labels and a
    cap of 16 is two calls. ``elapsed_ms`` is the detector's own reported
    inference time summed, not wall clock — the difference between the two is
    network and queueing, which is not the detector's cost to report.
    """

    model: str | None = None
    device: str | None = None
    calls: int = 0
    labels: int = 0
    detections: int = 0
    elapsed_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "url_host": _url_host(),
            "model": self.model,
            "device": self.device,
            "calls": self.calls,
            "labels": self.labels,
            "detections": self.detections,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "errors": list(self.errors),
        }


def is_configured() -> bool:
    """True when ``DETECTOR_URL`` is set. Empty means the feature is off."""
    return bool(DETECTOR_URL)


def _url_host() -> str | None:
    """The host of ``DETECTOR_URL`` for reporting — never the full URL.

    A URL can carry credentials; a host cannot. ``GET /document-kinds`` and
    every job result echo this, and neither is a place to publish a secret.
    """
    if not DETECTOR_URL:
        return None
    from urllib.parse import urlparse

    return urlparse(DETECTOR_URL).netloc or DETECTOR_URL


def status() -> dict:
    """The detector block for ``GET /document-kinds``."""
    return {
        "configured": is_configured(),
        "url_host": _url_host(),
        "min_score": DETECTOR_MIN_SCORE,
        "timeout_seconds": DETECTOR_TIMEOUT_S,
        "max_labels_per_call": DETECTOR_MAX_LABELS_PER_CALL,
    }


def _encode_png(working_image) -> bytes:
    """Working BGR page image → PNG bytes.

    PNG, not JPEG: the image is already ≤1000 px so the size difference is
    small, and JPEG ringing around a thin object is exactly the artefact that
    moves a borderline detection score.
    """
    import cv2

    ok, buffer = cv2.imencode(".png", working_image)
    if not ok:
        raise DetectorUnavailable("Could not encode the page image for the detector.")
    return buffer.tobytes()


async def detect_page(
    working_image,
    labels: list[str],
    geometry: PageGeometry,
    *,
    min_score: float = DETECTOR_MIN_SCORE,
    stats: DetectorStats | None = None,
) -> dict[str, list[Region]]:
    """Boxes for every label on one page, in ORIGINAL page pixels.

    Steps:
      1. Encode the working page image once, however many calls it takes.
      2. Call ``POST /detect`` with up to DETECTOR_MAX_LABELS_PER_CALL labels
         at a time, as JSON+base64 (one body shape, no multipart assembly).
      3. Turn each box into a ``Region`` with ``source="detector"`` and
         ``attrs.detector_score``, and rescale it out of working pixels.

    Args:
        working_image: The ≤1000-px BGR page image.
        labels:        Criterion names to look for. Order is preserved; the
                       detector echoes each label back exactly.
        geometry:      That page's frame, for the rescale.
        min_score:     Confidence floor sent as the detector's `threshold`.
        stats:         Accumulator to record the call in, if the caller wants
                       one. Mutated in place.

    Returns:
        ``{label: [Region, ...]}`` with an entry for EVERY requested label —
        an empty list means "looked, found none", which is a different answer
        from a missing key.

    Raises:
        DetectorUnavailable: Not configured, unreachable, or an unusable
            response. Callers catch this and carry on without regions.
    """
    if not DETECTOR_URL:
        raise DetectorUnavailable(
            "regions.detector was requested but DETECTOR_URL is not configured "
            "on this container, so no source=\"detector\" regions were produced."
        )
    if not labels:
        return {}

    # Step 1 — encode once.
    image_b64 = base64.b64encode(_encode_png(working_image)).decode("ascii")
    timeout = httpx.Timeout(DETECTOR_TIMEOUT_S, connect=min(10.0, DETECTOR_TIMEOUT_S))
    regions: dict[str, list[Region]] = {label: [] for label in labels}

    # Step 2 — one call per chunk of labels.
    chunks = [
        labels[i : i + DETECTOR_MAX_LABELS_PER_CALL]
        for i in range(0, len(labels), DETECTOR_MAX_LABELS_PER_CALL)
    ]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for chunk in chunks:
                response = await client.post(
                    f"{DETECTOR_URL}/detect",
                    json={
                        "image": {"data": image_b64, "type": "base64"},
                        "labels": chunk,
                        "threshold": min_score,
                    },
                )
                response.raise_for_status()
                body = response.json()
                _merge_page_result(body, chunk, geometry, regions, min_score, stats)
    except httpx.HTTPStatusError as exc:
        detail = _error_detail(exc.response)
        raise DetectorUnavailable(
            f"The detector at {_url_host()} answered "
            f"{exc.response.status_code}{detail} — no source=\"detector\" "
            "regions were produced for this job."
        )
    except httpx.HTTPError as exc:
        raise DetectorUnavailable(
            f"The detector at {_url_host()} could not be reached "
            f"({type(exc).__name__}: {exc}) — no source=\"detector\" regions "
            "were produced for this job."
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise DetectorUnavailable(
            f"The detector at {_url_host()} returned an unusable response "
            f"({type(exc).__name__}: {exc}) — no source=\"detector\" regions "
            "were produced for this job."
        )

    logger.info(
        "detect_page: page %d, %d label(s) in %d call(s) -> %s",
        geometry.page,
        len(labels),
        len(chunks),
        {label: len(found) for label, found in regions.items()},
    )
    return regions


def _merge_page_result(
    body: dict,
    chunk: list[str],
    geometry: PageGeometry,
    regions: dict[str, list[Region]],
    min_score: float,
    stats: DetectorStats | None,
) -> None:
    """Fold one ``/detect`` response into the page's region map.

    The score filter is applied again here even though it was sent as the
    detector's ``threshold``: a future family might round or clamp
    differently, and "every region in this list is at or above min_score" is
    a promise ``analysis.detector_eval`` scores criteria against.
    """
    by_label = body.get("by_label") or {}
    if stats is not None:
        stats.model = body.get("model") or stats.model
        stats.device = body.get("device") or stats.device
        stats.calls += 1
        stats.labels += len(chunk)
        stats.elapsed_ms += float(body.get("elapsed_ms") or 0.0)

    for label in chunk:
        for item in by_label.get(label) or []:
            box = item.get("box") or []
            score = float(item.get("score") or 0.0)
            if len(box) != 4 or score < min_score:
                continue
            x1, y1, x2, y2 = (float(v) for v in box)
            region = Region(
                page=geometry.page,
                kind="box",
                points=[(x1, y1), (x2, y2)],
                label=label,
                score=score,
                source="detector",
                attrs={"detector_score": round(score, 4)},
            )
            # Step 3 — working pixels → original page pixels, the same
            # transform every other region source goes through.
            regions[label].append(
                rescale_region(region, geometry.working_scale, geometry)
            )
            if stats is not None:
                stats.detections += 1


def _error_detail(response: httpx.Response) -> str:
    """The ``detail`` out of a FastAPI error body, as a parenthesised suffix.

    Best effort — a 502 from something that is not the detector at all has no
    JSON body, and an empty suffix is better than a traceback in a note.
    """
    try:
        detail = response.json().get("detail")
    except Exception:
        return ""
    return f" ({detail})" if detail else ""
