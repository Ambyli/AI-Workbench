"""HEIC / HEIF / AVIF photos in the classifier: decode, size, refusals.

An iPhone saves photos as HEIC by default; they are the same one-item
``image`` kind as a JPEG (``common.documents.detect``), decoded by
``analysis.loading._decode_image_bgr`` through pillow-heif's or Pillow's own
AVIF plugin, and re-encoded as JPEG/PNG for everything after that. What is
pinned here — none of it needs Postgres:

  * the decode — a HEIC and an AVIF come out as BGR arrays of the right size
    and colour, and the committed ``documents/photo_of_letter.heic`` (stored
    sideways, rotated by its container AND its EXIF tag, as an iPhone
    portrait is) decodes to the same upright page as ``photo_of_letter.png``;
  * orientation applied exactly once — an EXIF-oriented HEIC, AVIF and JPEG
    of the same picture decode to the same frame, for all eight orientations;
  * ``api.references._image_size`` — the submit-time size read from the
    header alone — agrees with the decoder for every format and orientation;
  * the refusals — no cv2 fallback for an undecodable HEIC (OpenCV cannot
    read one, and the fallback would hide PIL's message), none for a
    decompression bomb either, a video (Live Photo ``.MOV``) is a 400 naming
    video, and the SVG image inliner still takes PNG / JPEG only (MuPDF draws
    nothing else);
  * the declared types — ``image/heic``, ``image/heif`` and ``image/avif``
    pass the multipart allowlist.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_image_formats.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import io
import pathlib
import struct

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image

import analysis
from analysis import loading
from common.documents import UnsupportedDocumentError

HERE = pathlib.Path(__file__).resolve().parent
DOCS = HERE / "documents"


def _pillow_has_avif() -> bool:
    from PIL import features

    try:
        return bool(features.check_module("avif"))
    except ValueError:
        return False


needs_avif = pytest.mark.skipif(not _pillow_has_avif(), reason="Pillow built without AVIF")


# ---------------------------------------------------------------------------
# Fixture builders (in code — see shared/common/tests/documents_fixtures.py
# for the same recipes, which this suite cannot import)
# ---------------------------------------------------------------------------


def _picture(size=(120, 80)) -> Image.Image:
    """Blue, with a red block in the top-left quarter: its position after
    decoding says which way up the picture came out."""
    width, height = size
    img = Image.new("RGB", size, (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, width // 4, height // 4))
    return img


def _exif(orientation):
    if not orientation:
        return {}
    exif = Image.Exif()
    exif[0x0112] = orientation
    return {"exif": exif.tobytes()}


def _jpeg(img, orientation=None) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, **_exif(orientation))
    return buf.getvalue()


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _heic(img, orientation=None) -> bytes:
    """pillow-heif's encoder: an orientation becomes irot/imir AND stays in
    the stored EXIF — both, like an iPhone photo."""
    pillow_heif = pytest.importorskip("pillow_heif")
    buf = io.BytesIO()
    pillow_heif.from_pillow(img).save(buf, quality=90, **_exif(orientation))
    return buf.getvalue()


def _avif(img, orientation=None) -> bytes:
    """Pillow's encoder: an orientation becomes irot/imir, the EXIF tag is dropped."""
    buf = io.BytesIO()
    img.save(buf, format="AVIF", quality=90, **_exif(orientation))
    return buf.getvalue()


def _ftyp(major: str, compatible=(), tail: bytes = b"") -> bytes:
    brands = b"".join(b.encode("latin-1") for b in compatible)
    return struct.pack(">I", 16 + len(brands)) + b"ftyp" + major.encode() + b"\0\0\0\0" + brands + tail


def _marker_box(bgr):
    red = (bgr[:, :, 2] > 180) & (bgr[:, :, 1] < 90) & (bgr[:, :, 0] < 90)
    ys, xs = np.nonzero(red)
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())) if len(xs) else None


def _close(a, b, slack=3) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= slack for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", [_heic, pytest.param(_avif, marks=needs_avif)], ids=["heic", "avif"])
def test_decode_gives_a_bgr_array(build) -> None:
    bgr = loading._decode_image_bgr(build(Image.new("RGB", (300, 200), (255, 0, 0))))
    assert bgr.shape == (200, 300, 3) and bgr.dtype == np.uint8
    b, g, r = bgr.reshape(-1, 3).mean(axis=0)
    assert r > 230 and g < 25 and b < 25, (b, g, r)


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("fmt", ["heic", pytest.param("avif", marks=needs_avif)])
def test_oriented_heic_avif_and_jpeg_decode_to_the_same_frame(fmt, orientation) -> None:
    build = _heic if fmt == "heic" else _avif
    want = loading._decode_image_bgr(_jpeg(_picture(), orientation))
    got = loading._decode_image_bgr(build(_picture(), orientation))
    assert got.shape == want.shape
    assert _close(_marker_box(got), _marker_box(want)), (_marker_box(got), _marker_box(want))


@pytest.mark.parametrize("orientation", range(1, 9))
@pytest.mark.parametrize("fmt", ["jpeg", "heic", pytest.param("avif", marks=needs_avif)])
def test_reference_image_size_agrees_with_the_decoder(fmt, orientation) -> None:
    """The submit reads a reference photo's size from its header; the worker
    decodes it. A HEIC opens with the container's rotation already applied
    and its EXIF tag reset to 1, an AVIF at its stored size with the
    rotation reported AS the EXIF tag — the one swap in _image_size has to
    land on the decoded frame for both."""
    from api.references import _image_size

    raw = {"jpeg": _jpeg, "heic": _heic, "avif": _avif}[fmt](_picture(), orientation)
    height, width = loading._decode_image_bgr(raw).shape[:2]
    assert _image_size(raw) == (width, height)


def test_the_heic_fixture_is_the_png_fixture_turned_upright() -> None:
    """``photo_of_letter.heic`` is stored sideways (1300×1000) and says
    "rotate 90° clockwise" twice over — its irot/imir and its EXIF tag — as a
    portrait iPhone photo does. Decoded, it is the PNG's upright page."""
    pytest.importorskip("pillow_heif")
    from api.references import _image_size

    heic = (DOCS / "photo_of_letter.heic").read_bytes()
    png = loading._decode_image_bgr((DOCS / "photo_of_letter.png").read_bytes())
    got = loading._decode_image_bgr(heic)
    assert got.shape == png.shape == (1300, 1000, 3)
    assert _image_size(heic) == (1000, 1300)
    # Lossy, but the same picture: a wrong rotation would differ everywhere.
    assert np.abs(got.astype(np.int16) - png.astype(np.int16)).mean() < 6


def test_load_document_bytes_gives_one_heic_item() -> None:
    raw = _heic(_picture((400, 300)))
    doc = analysis.load_document_bytes(raw, "IMG_0001.HEIC", "image/heic")
    assert doc.kind == "image" and doc.content_type == "image/heic"
    assert len(doc.pages) == 1
    assert (doc.pages[0].width, doc.pages[0].height) == (400, 300)
    page = analysis.load_document_page(raw, "IMG_0001.HEIC", "image/heic")
    assert page.kind == "image" and len(page.pages) == 1


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.fixture
def no_cv2_fallback(monkeypatch):
    """Fails the test if the decoder reaches for cv2.imdecode."""
    import cv2

    def boom(*args, **kwargs):
        raise AssertionError("cv2.imdecode fallback was used")

    monkeypatch.setattr(cv2, "imdecode", boom)


def test_an_undecodable_heic_is_refused_without_the_cv2_fallback(no_cv2_fallback) -> None:
    pytest.importorskip("pillow_heif")
    raw = _ftyp("heic", ["mif1", "heic"], tail=b"\x00\x00\x00\x08free" + b"\x00" * 200)
    with pytest.raises(UnsupportedDocumentError, match="Could not decode heic image"):
        loading._decode_image_bgr(raw)
    with pytest.raises(HTTPException) as exc:
        analysis.load_document_bytes(raw, "broken.heic", "image/heic")
    assert exc.value.status_code == 400 and "heic" in exc.value.detail


@pytest.mark.parametrize("fmt", ["png", "heic"])
def test_a_decompression_bomb_is_refused_without_the_cv2_fallback(fmt, monkeypatch, no_cv2_fallback) -> None:
    raw = _png(_picture()) if fmt == "png" else _heic(_picture())
    # 120×80 = 9,600 px; Pillow raises DecompressionBombError past 2× this.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(UnsupportedDocumentError, match="too large"):
        loading._decode_image_bgr(raw)
    with pytest.raises(HTTPException) as exc:
        analysis.load_document_bytes(raw, f"bomb.{fmt}", None)
    assert exc.value.status_code == 400 and "too large" in exc.value.detail


def test_a_live_photo_video_is_refused_as_video() -> None:
    raw = _ftyp("qt  ", ["qt  "], tail=b"\x00\x00\x00\x08wide" + b"\x00" * 64)
    with pytest.raises(HTTPException) as exc:
        analysis.load_document_bytes(raw, "IMG_0001.MOV", "video/quicktime")
    assert exc.value.status_code == 400
    assert "video" in exc.value.detail and "'qt  '" in exc.value.detail


@pytest.mark.parametrize("content_type", ["image/heic", "image/heif", "image/avif", "IMAGE/HEIC; q=1"])
def test_the_heif_family_passes_the_content_type_allowlist(content_type) -> None:
    analysis.validate_content_type(content_type)


def test_video_still_fails_the_content_type_allowlist() -> None:
    with pytest.raises(HTTPException) as exc:
        analysis.validate_content_type("video/quicktime")
    assert exc.value.status_code == 400 and "HEIC" in exc.value.detail


class _Fetcher:
    def __init__(self, body: bytes):
        self.body = body

    async def __call__(self, url, **kwargs):
        return self.body


@pytest.mark.parametrize("fmt", ["heic", pytest.param("avif", marks=needs_avif)])
def test_the_svg_inliner_refuses_heic_and_avif(fmt, monkeypatch) -> None:
    """MuPDF draws only JPEG and PNG data: images — a HEIC/AVIF inlined into
    an SVG would be a blank region dressed up as a fetched image."""
    body = (_heic if fmt == "heic" else _avif)(_picture())
    monkeypatch.setattr(loading, "fetch_url", _Fetcher(body))
    uri, reason = asyncio.run(loading._fetch_svg_image(f"https://cdn.test/logo.{fmt}"))
    assert uri is None and reason == "not a PNG or JPEG image"


@pytest.mark.parametrize("fmt,mime", [("png", "image/png"), ("jpeg", "image/jpeg")])
def test_the_svg_inliner_still_takes_png_and_jpeg(fmt, mime, monkeypatch) -> None:
    body = _png(_picture()) if fmt == "png" else _jpeg(_picture())
    monkeypatch.setattr(loading, "fetch_url", _Fetcher(body))
    uri, reason = asyncio.run(loading._fetch_svg_image(f"https://cdn.test/logo.{fmt}"))
    assert reason is None and uri.startswith(f"data:{mime};base64,")
