"""The working image frame, and the map back to original page pixels.

Every detector in this service — OpenCV, the open-vocabulary detector, the
vision model's bounding-box loop — looks at the SAME resized copy of the page,
so their coordinates all mean the same thing and all rescale the same way. The
two halves of that live here: the resize, and the ``PageGeometry`` that
records what the resize did.

    working_image_of() — the page image at <=MAX_WORKING_DIMENSION, or None.
    page_geometry()    — the PageGeometry for that page, or None when it has
                         no image (.txt / .docx — there is no pixel space for
                         a region to live in).

Process flow position: called once per job by
``analysis.context.DocumentContext.build``; the result is shared, read-only,
by every evaluator.
"""

from typing import Any, Optional

from common.documents import Document, Page
from common.vision import PageGeometry

from config import MAX_WORKING_DIMENSION, PDF_RENDER_DPI
from logger import logger


def working_image_of(page: Page) -> Optional[Any]:
    """The page image resized to ≤MAX_WORKING_DIMENSION on the long side.

    Done once and shared by the CV detectors, the detector service, and the
    LLM prompt so all of them see exactly the same pixels (and so the CV
    thresholds keep the meaning they were tuned with).
    """
    image = page.image_bgr
    if image is None:
        return None
    import cv2

    h, w = image.shape[:2]
    if max(h, w) > MAX_WORKING_DIMENSION:
        scale = MAX_WORKING_DIMENSION / max(h, w)
        image = cv2.resize(
            image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
        )
        logger.debug("working_image_of: page resized to %s", image.shape)
    return image


def page_geometry(doc: Document, page: Page, working: Any) -> Optional[PageGeometry]:
    """The coordinate frame for the page, or None when it has no image.

    ``working_scale`` is measured from the actual arrays rather than
    recomputed from MAX_WORKING_DIMENSION, so it stays right if the resize
    rule ever changes. ``pdf_points`` is derived from the render DPI (the
    page was rasterised at PDF_RENDER_DPI, so 1 pt = dpi/72 px) instead of
    re-opening the PDF for a number we already know.
    """
    if page.image_bgr is None or not page.width or not page.height:
        return None
    scale = (working.shape[1] / page.width) if working is not None else 1.0
    pdf_points = None
    if doc.kind == "pdf" and PDF_RENDER_DPI:
        factor = 72.0 / PDF_RENDER_DPI
        pdf_points = (page.width * factor, page.height * factor)
    return PageGeometry(
        page=page.index,
        width=page.width,
        height=page.height,
        working_scale=scale,
        pdf_points=pdf_points,
    )
