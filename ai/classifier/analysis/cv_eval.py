"""Running one OpenCV detector over a document, and keeping its geometry.

The `cv` evaluation path. ``cv/`` holds the detectors themselves and knows
nothing about documents or pages; this module is the bridge — it runs a
detector across every page image, collapses the per-page results the way a
multi-page document demands (the worst page wins), and turns the geometry the
detectors already computed into ``Region`` objects in ORIGINAL page pixels.

    _run_cv_criterion() — one detector, every page, worst page reported.
    _cv_regions()       — detector output (working-image dicts) → Regions.

Process flow position: step 4 of ``analysis.pipeline.analyze_document``.
"""

from common.vision import PageGeometry, Region, rescale_region

from logger import logger


def _run_cv_criterion(
    detector,
    name: str,
    page_images: list[tuple[int, object]],
    geometries: dict[int, PageGeometry] | None = None,
) -> tuple[dict, list[Region]]:
    """Run one CV detector across every page image; the worst page wins.

    A multi-page document is only as sharp / well-exposed as its worst page —
    one blurred page of a five-page contract is still an unusable document —
    so the minimum score is reported, with every page's measurement kept in a
    ``pages`` list for the caller to inspect.

    Regions are the exception to "worst page wins": EVERY page's regions are
    kept, because "where is the vegetation" is a different question from "how
    much of it is there", and answering only for the worst page would hide
    most of the document. They are rescaled from the detector's working-image
    coordinates into original page pixels here — the one place that knows
    both the detector output and the page geometry.

    Args:
        detector:    The detector callable from cv.get_detector().
        name:        Criterion name (used for logging and as the region label).
        page_images: ``[(page_index, bgr_image), ...]``, already resized.
        geometries:  ``{page_index: PageGeometry}``; None means the caller did
                     not ask for regions, so none are built.

    Returns:
        ``(result, regions)`` — the standard CV result dict (plus ``pages``
        and ``page``, the index the reported score came from), and the
        regions in ORIGINAL page pixels.
    """
    per_page: list[dict] = []
    regions: list[Region] = []
    for index, image in page_images:
        result = dict(detector(image))
        result["page"] = index
        raw_regions = result.pop("regions", None) or []
        if geometries is not None and index in geometries:
            regions.extend(_cv_regions(raw_regions, name, geometries[index]))
        per_page.append(result)

    worst = min(per_page, key=lambda r: r.get("score", 10))
    merged = dict(worst)
    merged["method"] = "cv"
    if len(per_page) > 1:
        merged["pages"] = [
            {
                "index": r["page"],
                "score": r.get("score"),
                "verdict": r.get("verdict"),
                "detail": r.get("detail"),
            }
            for r in per_page
        ]
        merged["detail"] = (
            f"{worst.get('detail', '')} "
            f"(worst of {len(per_page)} pages — page {worst['page']})"
        ).strip()
    logger.debug(
        "_run_cv_criterion: '%s' pages=%d worst_page=%s score=%s regions=%d",
        name,
        len(per_page),
        worst.get("page"),
        merged.get("score"),
        len(regions),
    )
    return merged, regions


def _cv_regions(
    raw: list[dict], label: str, geometry: PageGeometry
) -> list[Region]:
    """Detector output (working-image dicts) → Regions in original pixels."""
    regions: list[Region] = []
    for item in raw:
        points = item.get("points") or []
        if len(points) < 2:
            continue
        region = Region(
            page=geometry.page,
            kind=item.get("kind", "box"),
            points=points,
            label=label,
            score=item.get("score"),
            source="cv",
            attrs=dict(item.get("attrs") or {}),
        )
        regions.append(rescale_region(region, geometry.working_scale, geometry))
    return regions
