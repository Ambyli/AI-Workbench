"""Bytes in, ``common.documents.Document`` out — plus the checks on the way.

Everything the pipeline needs before a criterion can run: the declared-type
allowlist, the image decode (EXIF orientation applied once, here, so every
image page in the service goes through the same path), the kind detection and
per-kind load, and the fetch for a caller-supplied URL.

    validate_content_type()      — allowlist check on the declared upload type.
                                   Advisory: the magic bytes have the final say.
    _validate_image_dimensions() — reject page images too small to assess.
    _decode_image_bgr()          — raw image bytes → BGR numpy array.
    load_document_bytes()        — raw bytes → Document (kind detection, page
                                   cap, PDF render DPI).
    validate_url()               — the SSRF check on a caller-supplied URL. The
                                   rule itself is ``common.net`` (the detector
                                   service needs the identical one); this is the
                                   classifier-side adapter that supplies
                                   ``config.BLOCKED_NETWORKS`` and turns a
                                   refusal into the HTTP 400 the endpoints return.
    _load_bytes_from_input()     — base64 or URL → raw bytes.

Process flow position: below the pipeline, above nothing — it imports no
sibling. Called by ``analysis.pipeline`` and by ``api.assess`` /
``api.locate`` (``validate_content_type``) before a job is queued.
"""

import base64
import io

import httpx
import numpy as np
from fastapi import HTTPException
from PIL import Image, ImageOps

from common.documents import Document, UnsupportedDocumentError, load_document
from common.net import BlockedURLError, validate_url as _validate_url

from config import (
    ACCEPTED_CONTENT_TYPES,
    BLOCKED_NETWORKS,
    DOC_MAX_PAGES,
    HTTP_CONNECT_TIMEOUT,
    HTTP_TIMEOUT,
    MIN_IMAGE_HEIGHT,
    MIN_IMAGE_WIDTH,
    PDF_RENDER_DPI,
)
from logger import logger

_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)

# ACCEPTED_CONTENT_TYPES lives in config.py (§ Document analysis constants)
# with the rest of the tunables; the SSRF blocklist itself (BLOCKED_NETWORKS)
# lives there too (§ SSRF blocklist), seeded from
# common.net.DEFAULT_BLOCKED_NETWORKS.


def validate_content_type(content_type: str | None) -> None:
    """Reject a declared upload type that is obviously not a document.

    Advisory only — the bytes are re-checked by ``detect_kind`` during load,
    which is what actually decides the kind. This exists so an obviously wrong
    upload (a video, say) is refused before its bytes are read into memory and
    queued.

    Args:
        content_type: The multipart part's Content-Type, if any.

    Raises:
        HTTPException(400): If the type is present and not in the allowlist.
    """
    if content_type and content_type.split(";")[0].strip().lower() not in ACCEPTED_CONTENT_TYPES:
        logger.warning("validate_content_type: rejected content_type=%s", content_type)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported content type '{content_type}'. Accepted: JPEG/PNG images, "
                "PDF, plain text, and .docx (see GET /document-kinds)."
            ),
        )


def _validate_image_dimensions(w: int, h: int) -> None:
    """Reject page images that are too small to produce meaningful assessments.

    Images below MIN_IMAGE_WIDTH × MIN_IMAGE_HEIGHT pixels cannot provide
    enough detail for reliable LLM scoring and are refused early.

    Args:
        w, h: Image width and height in pixels.

    Raises:
        HTTPException(400): If either dimension is below the configured minimum.
    """
    logger.debug(
        "_validate_image_dimensions: w=%d h=%d (min %dx%d)",
        w,
        h,
        MIN_IMAGE_WIDTH,
        MIN_IMAGE_HEIGHT,
    )
    if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
        logger.warning("_validate_image_dimensions: image too small (%dx%d)", w, h)
        raise HTTPException(
            status_code=400,
            detail=f"Image too small ({w}×{h} px). Minimum is {MIN_IMAGE_WIDTH}×{MIN_IMAGE_HEIGHT} px.",
        )
    logger.debug("_validate_image_dimensions: dimensions valid")


def _decode_image_bgr(raw: bytes):
    """Decode raw image bytes to a BGR numpy array suitable for OpenCV.

    Passed to ``load_document`` as its image decoder, so every image page in
    the system goes through the same path:

      1. Open with PIL and apply EXIF orientation correction.
         Phone cameras embed orientation metadata; without this step a portrait
         photo may load sideways, producing wrong CV scores.
      2. Convert the PIL RGB array to OpenCV BGR format.
      3. Fall back to direct cv2.imdecode() if PIL fails for any reason.

    Magic-byte validation happens in common.documents.detect before this is
    reached, so there is no format check here.

    Args:
        raw: Raw JPEG or PNG file bytes.

    Returns:
        BGR numpy array (H×W×3).

    Raises:
        HTTPException(400): If decoding fails entirely.
    """
    import cv2

    logger.debug("_decode_image_bgr: decoding %d bytes", len(raw))

    try:
        # PIL handles EXIF orientation (cv2 does not)
        pil_img = Image.open(io.BytesIO(raw))
        pil_img = ImageOps.exif_transpose(pil_img)  # rotate to match camera orientation
        rgb = np.array(pil_img.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        logger.debug(
            "_decode_image_bgr: PIL decode + EXIF correction succeeded shape=%s", bgr.shape
        )
    except Exception as exc:
        # PIL failed; fall back to cv2 (no EXIF correction)
        logger.warning(
            "_decode_image_bgr: PIL EXIF correction failed (%s), falling back to cv2", exc
        )
        nparr = np.frombuffer(raw, np.uint8)
        bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if bgr is None:
        logger.error("_decode_image_bgr: failed to decode image from %d bytes", len(raw))
        raise HTTPException(status_code=400, detail="Failed to decode image.")

    logger.debug("_decode_image_bgr: returning image shape=%s", bgr.shape)
    return bgr


def load_document_bytes(
    raw: bytes,
    filename: str | None = None,
    content_type: str | None = None,
    *,
    keep_source: bool = False,
) -> Document:
    """Turn uploaded bytes into a Document, or fail with a 400.

    Kind detection is by magic bytes (``common.documents.detect_kind``), so a
    PDF uploaded as ``image/png`` still loads as a PDF and a legacy ``.doc``
    is rejected with an actionable message.

    Args:
        raw:          Complete file bytes.
        filename:     Original filename, recorded on the Document.
        content_type: Declared MIME type (error messages only).
        keep_source:  Retain the raw bytes on the Document. Only set when the
                      request asked for regions on a document that might be a
                      native PDF — ``common.documents.pdf_text_regions`` has
                      to re-open the file to ask PyMuPDF where a phrase is.

    Returns:
        A ``Document`` with at least one page.

    Raises:
        HTTPException(400): Unsupported or unparseable bytes.
    """
    logger.debug(
        "load_document_bytes: %d bytes filename=%s content_type=%s keep_source=%s",
        len(raw),
        filename,
        content_type,
        keep_source,
    )
    try:
        doc = load_document(
            raw,
            filename=filename,
            content_type=content_type,
            max_pages=DOC_MAX_PAGES,
            render_dpi=PDF_RENDER_DPI,
            image_decoder=_decode_image_bgr,
            keep_source=keep_source,
        )
    except UnsupportedDocumentError as exc:
        logger.warning("load_document_bytes: rejected %s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc))

    logger.info(
        "load_document_bytes: kind=%s pages=%d truncated=%d has_text=%s has_images=%s",
        doc.kind,
        len(doc.pages),
        doc.truncated_pages,
        doc.has_text(),
        doc.has_images(),
    )
    return doc


def validate_url(url: str) -> None:
    """Raise HTTP 400 if the URL targets a private or internal network address.

    Args:
        url: The URL string supplied by the caller.

    Raises:
        HTTPException(400): If the URL scheme is invalid, the hostname cannot
            be resolved, or a resolved IP is in a blocked network.
    """
    logger.debug("validate_url: checking url=%s", url[:120])
    try:
        _validate_url(url, BLOCKED_NETWORKS)
    except BlockedURLError as exc:
        logger.warning("validate_url: refused url=%s: %s", url[:120], exc)
        raise HTTPException(status_code=400, detail=str(exc))
    logger.debug("validate_url: url passed SSRF check")


async def _load_bytes_from_input(data: str, type_: str) -> bytes:
    """Fetch the raw document bytes from a base64 string or a remote URL.

    For URL inputs: performs an SSRF check before fetching (ssrf.validate_url)
    and sets a descriptive User-Agent to avoid 403 responses from servers that
    block default request libraries.

    Args:
        data:  Base64 string or URL string.
        type_: "base64" or "url".

    Returns:
        Raw file bytes (format is determined later, from the bytes themselves).

    Raises:
        HTTPException(400): Invalid base64 data or SSRF-blocked URL.
        HTTPException(502): HTTP error while fetching the URL.
    """
    data_repr = data[:80] if type_ == "url" else f"base64[{len(data)} chars]"
    logger.debug("_load_bytes_from_input: type=%s data=%s", type_, data_repr)

    if type_ == "base64":
        # Decode the base64 payload directly — no network call needed
        try:
            raw = base64.b64decode(data)
        except Exception as exc:
            logger.error("_load_bytes_from_input: invalid base64 data: %s", exc)
            raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")
    else:
        # SSRF check must pass before we fetch anything
        validate_url(data)
        try:
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                r = await client.get(data, headers={"User-Agent": "Classifier/1.0"})
                r.raise_for_status()
                raw = r.content
            logger.debug("_load_bytes_from_input: fetched %d bytes from URL", len(raw))
        except httpx.HTTPError as exc:
            logger.error(
                "_load_bytes_from_input: failed to fetch URL '%s': %s", data[:80], exc
            )
            raise HTTPException(
                status_code=502, detail=f"Failed to fetch document URL: {exc}"
            )

    if not raw:
        raise HTTPException(status_code=400, detail="Document input was empty.")
    return raw
