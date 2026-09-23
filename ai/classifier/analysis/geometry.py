"""The working image frame, and the map back to original page pixels.

Every detector in this service — OpenCV, the open-vocabulary detector, the
vision model's bounding-box loop — looks at the SAME resized copy of a page,
so their coordinates all mean the same thing and all rescale the same way. The
two halves of that live here: the resize, and the ``PageGeometry`` that
records what the resize did.

    _resized_page_images() — every page image at <=MAX_WORKING_DIMENSION.
    working_page_images()  — the same, for a caller outside the pipeline
                             (``jobs.runners.run_compare``, for change
                             detection, which compares two documents and so
                             cannot happen inside ``analyze_document``).
    _page_geometries()     — one PageGeometry per image-bearing page.

Pages with no image (.txt / .docx) get no geometry — there is no pixel space
for a region to live in.

Process flow position: step 3 of ``analysis.pipeline.analyze_document``; the
geometries are built once and shared by the CV, text and detector paths.
"""

from common.documents import Document
from common.vision import PageGeometry

from config import MAX_WORKING_DIMENSION, PDF_RENDER_DPI
from logger import logger


def _resized_page_images(doc: Document) -> list[tuple[int, object]]:
    """Every page image, resized to ≤MAX_WORKING_DIMENSION on the long side.

    Done once and shared by the CV detectors and the LLM prompt so both see
    exactly the same pixels (and so the CV thresholds keep the meaning they
    were tuned with on single images).
    """
    import cv2

    working: list[tuple[int, object]] = []
    for index, image in doc.page_images():
        h, w = image.shape[:2]
        if max(h, w) > MAX_WORKING_DIMENSION:
            scale = MAX_WORKING_DIMENSION / max(h, w)
            image = cv2.resize(
                image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
            )
            logger.debug("_resized_page_images: page %d resized to %s", index, image.shape)
        working.append((index, image))
    return working


def working_page_images(doc: Document) -> list[tuple[int, object]]:
    """The ≤MAX_WORKING_DIMENSION page copies, for a caller outside the pipeline.

    ``runners.run_compare`` needs them for change detection, which compares
    two documents and so cannot happen inside ``analyze_document``. Same
    frame every detector in this service works in, which is what makes a
    diff's coordinates rescale through a PageGeometry like any other source's.
    """
    return _resized_page_images(doc)


def _page_geometries(
    doc: Document, working_images: list[tuple[int, object]]
) -> dict[int, PageGeometry]:
    """The coordinate frame for every page that carries an image.

    ``working_scale`` is measured from the actual arrays rather than
    recomputed from MAX_WORKING_DIMENSION, so it stays right if the resize
    rule ever changes. ``pdf_points`` is derived from the render DPI (the
    page was rasterised at PDF_RENDER_DPI, so 1 pt = dpi/72 px) instead of
    re-opening the PDF for a number we already know.

    Pages with no image (.txt / .docx) get no geometry — there is no pixel
    space for a region to live in.
    """
    working = {index: image for index, image in working_images}
    geometries: dict[int, PageGeometry] = {}
    for page in doc.pages:
        if page.image_bgr is None or not page.width or not page.height:
            continue
        image = working.get(page.index)
        scale = (image.shape[1] / page.width) if image is not None else 1.0
        pdf_points = None
        if doc.kind == "pdf" and PDF_RENDER_DPI:
            factor = 72.0 / PDF_RENDER_DPI
            pdf_points = (page.width * factor, page.height * factor)
        geometries[page.index] = PageGeometry(
            page=page.index,
            width=page.width,
            height=page.height,
            working_scale=scale,
            pdf_points=pdf_points,
        )
    return geometries
