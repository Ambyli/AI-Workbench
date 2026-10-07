"""Bytes in, ``common.documents.Document`` out — plus the checks on the way.

Everything the pipeline needs before a criterion can run: the declared-type
allowlist, the image decode (EXIF orientation applied once, here, so every
image page in the service goes through the same path), the kind detection and
per-kind load, and the fetch for a caller-supplied URL.

    validate_content_type()      — allowlist check on the declared upload type.
                                   Advisory: the magic bytes have the final say.
    _validate_image_dimensions() — reject page images too small to assess.
    _decode_image_bgr()          — raw image bytes → BGR numpy array.
    load_document_bytes()        — raw bytes → Document (kind detection, every
                                   page up to ``max_pages``, PDF render DPI).
    load_document_page()         — raw bytes → a ONE-page Document: page
                                   ``page`` of a PDF (only the pages up to it
                                   are rendered), the image, or the SVG. What
                                   a reference — always one page — is built on.
    validate_url()               — the SSRF check on a caller-supplied URL. The
                                   rule itself is ``common.net`` (the detector
                                   service needs the identical one); this is the
                                   classifier-side adapter that supplies
                                   ``config.BLOCKED_NETWORKS`` and turns a
                                   refusal into the HTTP 400 the endpoints return.
    load_input_bytes()           — base64, URL, or inline text → raw bytes.
                                   The URL fetch is ``common.net.fetch_url``
                                   (SSRF check first, no redirects).
    resolve_svg_images()         — an SVG's external ``<image>`` links fetched
                                   (``common.net.fetch_url``, bounded by the
                                   CLASSIFIER_SVG_FETCH_* knobs) and inlined as
                                   ``data:`` URIs, each failure a warning. Runs
                                   AT SUBMIT, only when
                                   CLASSIFIER_SVG_FETCH_IMAGES is on.

Process flow position: below the pipeline, above nothing — it imports no
sibling. ``load_input_bytes`` / ``validate_content_type`` /
``resolve_svg_images`` are called by ``api.assess`` (and ``api.references``)
at submit (the bytes are needed there, for the item cap — a PDF counts its
pages); ``load_document_bytes`` by ``jobs.runners`` in the worker, once per
document (``load_document_page`` once per reference).
"""

import asyncio
import base64
import io
from dataclasses import replace
from typing import Optional

import numpy as np
from fastapi import HTTPException
from PIL import Image, ImageOps

from common.documents import (
    Document,
    UnsupportedDocumentError,
    detect_kind,
    find_external_refs,
    guess_content_type,
    load_document,
    replace_refs,
)
from common.net import BlockedURLError, FetchError, fetch_url, validate_url as _validate_url

from config import (
    ACCEPTED_CONTENT_TYPES,
    BLOCKED_NETWORKS,
    FETCH_TIMEOUT,
    HTTP_CONNECT_TIMEOUT,
    MAX_ITEMS,
    MIN_IMAGE_HEIGHT,
    MIN_IMAGE_WIDTH,
    PDF_RENDER_DPI,
    SVG_FETCH_MAX_BYTES,
    SVG_FETCH_MAX_IMAGES,
    SVG_FETCH_TIMEOUT_S,
)
from logger import logger

# Sent with every fetch: some servers 403 httpx's default User-Agent.
_USER_AGENT = {"User-Agent": "Classifier/1.0"}
# A URL in a warning is cut to this many characters (the loader's own
# "not rendered" lines use the same bound).
_WARNING_URL_CHARS = 200

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
                "PDF, SVG, plain text, and .docx (see GET /document-kinds)."
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
    max_pages: int = MAX_ITEMS,
    warnings: Optional[list[str]] = None,
) -> Document:
    """Turn uploaded bytes into a Document, or fail with a 400.

    Kind detection is by magic bytes (``common.documents.detect_kind``), so a
    PDF uploaded as ``image/png`` still loads as a PDF and a legacy ``.doc``
    is rejected with an actionable message.

    Args:
        raw:          Complete file bytes.
        filename:     Original filename, recorded on the Document.
        content_type: Declared MIME type (error messages only).
        keep_source:  Retain the raw bytes on the Document — needed for a
                      native PDF, where ``common.documents.pdf_text_regions``
                      re-opens the file to ask PyMuPDF where a phrase is.
                      Regions are always collected now, so the runner always
                      passes True; it costs one copy of an upload the payload
                      already held.
        max_pages:    Pages to load. The runner passes the count read at
                      submit, which already passed CLASSIFIER_MAX_ITEMS.
        warnings:     Notes the SUBMIT already made about this document (an
                      SVG image it could not fetch, and why). Placed ahead of
                      the loader's own in ``Document.warnings``, so the
                      result's ``documents[].warnings`` reads in the order
                      things happened.

    Returns:
        A ``Document`` with every page — each becomes one item.

    Raises:
        HTTPException(400): Unsupported or unparseable bytes, or a PDF with
            more pages than ``max_pages`` (the item cap refuses these at
            submit; one reaching a worker anyway is refused, not truncated).
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
            max_pages=max(1, int(max_pages)),
            render_dpi=PDF_RENDER_DPI,
            image_decoder=_decode_image_bgr,
            keep_source=keep_source,
        )
    except UnsupportedDocumentError as exc:
        logger.warning("load_document_bytes: rejected %s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc))

    if doc.truncated_pages:
        # Submit counted the pages; a document that has more than that by the
        # time a worker reads it is refused rather than silently half-read.
        raise HTTPException(
            status_code=400,
            detail=(
                f"document {filename or '(unnamed)'} has "
                f"{len(doc.pages) + doc.truncated_pages} pages, more than the "
                f"{max_pages} counted at submit"
            ),
        )
    if warnings:
        doc.warnings = list(warnings) + doc.warnings
    logger.info(
        "load_document_bytes: kind=%s pages=%d has_text=%s has_image=%s warnings=%d",
        doc.kind,
        len(doc.pages),
        doc.has_text(),
        doc.has_images(),
        len(doc.warnings),
    )
    return doc


def load_document_page(
    raw: bytes,
    filename: str | None = None,
    content_type: str | None = None,
    *,
    page: int = 0,
    keep_source: bool = True,
    warnings: Optional[list[str]] = None,
) -> Document:
    """One page of an upload, as a one-page ``Document`` — or a 400.

    A reference example is ONE page: a JPEG/PNG, an SVG, or page ``page`` of
    a PDF.
    Only the pages up to it are rendered (a PDF's pages render in order),
    and the result is a view holding just that page, so the pipeline sees a
    one-item document. ``Page.index`` stays the page's index IN ITS PDF,
    which is what ``common.documents.pdf_text_regions`` needs to re-open the
    right page (hence ``keep_source``). ``warnings`` are the submit's notes
    on the document, prepended to the loader's as in ``load_document_bytes``.

    Raises:
        HTTPException(400): Unsupported bytes, a text-only kind (.txt /
            .docx have no page image to show the model), or a page index the
            document does not have.
    """
    try:
        doc = load_document(
            raw,
            filename=filename,
            content_type=content_type,
            max_pages=max(1, int(page) + 1),
            render_dpi=PDF_RENDER_DPI,
            image_decoder=_decode_image_bgr,
            keep_source=keep_source,
        )
    except UnsupportedDocumentError as exc:
        logger.warning("load_document_page: rejected %s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    if doc.kind not in ("image", "pdf", "svg"):
        raise HTTPException(
            status_code=400,
            detail=f"a {doc.kind} document has no page image; a reference is one "
                   "JPEG/PNG, one SVG, or one PDF page",
        )
    if page >= len(doc.pages):
        raise HTTPException(
            status_code=400,
            detail=f"page {page} is not in {filename or 'the document'} "
                   f"({len(doc.pages) + doc.truncated_pages} page(s))",
        )
    return replace(
        doc,
        pages=[doc.pages[page]],
        truncated_pages=0,
        warnings=list(warnings or []) + doc.warnings,
    )


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


async def load_input_bytes(data: str, type_: str) -> bytes:
    """The raw document bytes for a base64 string, a remote URL, or inline text.

    For URL inputs: ``common.net.fetch_url`` performs the SSRF check before
    fetching, refuses redirects (a 3xx could point past the check), and the
    request carries a descriptive User-Agent to avoid 403 responses from
    servers that block default request libraries. No size cap — the item cap
    and the payload store bound what a document can cost — and the same
    FETCH_TIMEOUT / HTTP_CONNECT_TIMEOUT per-operation timeouts as always.

    Args:
        data:  Base64 string, URL string, or the document text itself.
        type_: "base64", "url", or "text" (encoded as UTF-8 — a .txt).

    Returns:
        Raw file bytes (format is determined later, from the bytes themselves).

    Raises:
        HTTPException(400): Invalid base64 data or SSRF-blocked URL.
        HTTPException(502): HTTP error, redirect, or transport failure while
            fetching the URL.
    """
    data_repr = data[:80] if type_ == "url" else f"base64[{len(data)} chars]"
    logger.debug("load_input_bytes: type=%s data=%s", type_, data_repr)

    if type_ == "text":
        raw = data.encode("utf-8")
    elif type_ == "base64":
        # Decode the base64 payload directly — no network call needed
        try:
            raw = base64.b64decode(data)
        except Exception as exc:
            logger.error("load_input_bytes: invalid base64 data: %s", exc)
            raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")
    else:
        # fetch_url runs the SSRF check before it requests anything; the
        # explicit call first keeps the refusal's log line and 400 identical
        # to every other URL input in this service.
        validate_url(data)
        try:
            raw = await fetch_url(
                data,
                timeout=FETCH_TIMEOUT,
                connect_timeout=HTTP_CONNECT_TIMEOUT,
                blocked_networks=BLOCKED_NETWORKS,
                headers=_USER_AGENT,
            )
            logger.debug("load_input_bytes: fetched %d bytes from URL", len(raw))
        except BlockedURLError as exc:
            # Resolved differently the second time — refuse, like the first.
            raise HTTPException(status_code=400, detail=str(exc))
        except FetchError as exc:
            logger.error(
                "load_input_bytes: failed to fetch URL '%s': %s", data[:80], exc
            )
            raise HTTPException(
                status_code=502, detail=f"Failed to fetch document URL: {exc}"
            )

    if not raw:
        raise HTTPException(status_code=400, detail="Document input was empty.")
    return raw


# ---------------------------------------------------------------------------
# SVG external images (CLASSIFIER_SVG_FETCH_IMAGES)
# ---------------------------------------------------------------------------


def _short(url: str) -> str:
    return url if len(url) <= _WARNING_URL_CHARS else url[:_WARNING_URL_CHARS] + "…"


async def _fetch_svg_image(url: str) -> tuple[Optional[str], Optional[str]]:
    """One external image → ``(data_uri, None)``, or ``(None, reason)``.

    Never raises: every way a fetch can go wrong is a reason string, because
    one unreachable logo must not fail the request it is decoration on.
    """
    if not url.lower().startswith(("http://", "https://")):
        # A relative path or file:// would be resolved against nothing we
        # are willing to read; validate_url would refuse it anyway, but this
        # says so in words a caller recognises.
        return None, "not an http(s) URL"
    try:
        body = await fetch_url(
            url,
            timeout=SVG_FETCH_TIMEOUT_S,
            deadline=SVG_FETCH_TIMEOUT_S,
            max_bytes=SVG_FETCH_MAX_BYTES,
            blocked_networks=BLOCKED_NETWORKS,
            headers=_USER_AGENT,
        )
    except BlockedURLError as exc:
        return None, f"blocked: {str(exc).rstrip('.')}"
    except FetchError as exc:
        return None, exc.reason
    except Exception as exc:  # belt and braces — see the docstring
        logger.warning("resolve_svg_images: unexpected error fetching %s: %r", url[:120], exc)
        return None, "fetch failed"
    # PNG / JPEG by the bytes, nothing else: not a nested SVG (which would
    # need its own pass), not an HTML error page served with a 200.
    try:
        kind = detect_kind(body)
    except UnsupportedDocumentError:
        kind = None
    if kind != "image":
        return None, "not a PNG or JPEG image"
    mime = guess_content_type("image", body)
    return f"data:{mime};base64,{base64.b64encode(body).decode('ascii')}", None


async def resolve_svg_images(raw: bytes) -> tuple[bytes, list[str]]:
    """Fetch an SVG's external ``<image>`` links and inline them — at submit.

    MuPDF never fetches, so without this an ``<image href="https://…">`` is a
    blank region. With CLASSIFIER_SVG_FETCH_IMAGES on, the endpoints call
    this BEFORE queueing (``jobs.payloads`` keeps the worker off the
    network), and every external ``<image>`` reference ends up one of two
    things:

      * fetched — its href rewritten to a ``data:`` URI, which MuPDF draws;
      * not — its href rewritten to ``""`` (drawn as nothing, exactly as
        before) and a warning naming the reason:
        ``external image not fetched (<reason>): <url>``.

    Either way the loader no longer sees it as external, so its own
    "not rendered" warning fires only for what this does not handle
    (``<use>``, ``<feImage>``) — no reference is reported twice.

    Limits (``config`` § SVG external images): at most
    CLASSIFIER_SVG_FETCH_MAX_IMAGES DISTINCT URLs — the same logo referenced
    five times is one fetch — the rest are blanked with an "over the limit"
    warning; each fetch bounded by CLASSIFIER_SVG_FETCH_MAX_BYTES and
    CLASSIFIER_SVG_FETCH_TIMEOUT_S, SSRF-checked, no redirects; all of them
    concurrently, so the added submit latency is about one timeout.

    Args:
        raw: The SVG bytes as submitted.

    Returns:
        ``(rewritten_bytes, warnings)`` — ``raw`` itself and ``[]`` when the
        file has no external ``<image>``.
    """
    refs = [r for r in find_external_refs(raw) if r.tag == "image"]
    if not refs:
        return raw, []

    distinct: list[str] = []
    for ref in refs:
        if ref.url not in distinct:
            distinct.append(ref.url)
    to_fetch = distinct[:SVG_FETCH_MAX_IMAGES]
    outcomes = await asyncio.gather(*(_fetch_svg_image(url) for url in to_fetch))
    by_url: dict[str, tuple[Optional[str], Optional[str]]] = dict(zip(to_fetch, outcomes))
    over_limit = f"over the {SVG_FETCH_MAX_IMAGES}-image limit"

    replacements: dict = {}
    warnings: list[str] = []
    warned: set[str] = set()
    for ref in refs:
        data_uri, reason = by_url.get(ref.url, (None, over_limit))
        replacements[ref] = data_uri or ""
        if data_uri is None and ref.url not in warned:
            warned.add(ref.url)
            warnings.append(f"external image not fetched ({reason}): {_short(ref.url)}")
    fetched = sum(1 for d, _ in by_url.values() if d is not None)
    logger.info(
        "resolve_svg_images: %d <image> reference(s), %d distinct URL(s), %d fetched, "
        "%d not",
        len(refs), len(distinct), fetched, len(distinct) - fetched,
    )
    return replace_refs(raw, replacements), warnings
