"""Tests for SVG support in common.documents — svg.py and the svg loader.

Three groups:

  * the loader — one page with a render and a native text layer, sized from
    width/height (mm units included) or the viewBox, the pixel cap, the
    errors, and the "not rendered" warnings;
  * the NO-FETCH regression — MuPDF must not request, read or expand
    anything an SVG points at. This is the property the whole design leans on
    (the classifier fetches images itself, at submit, through the SSRF guard,
    or not at all), so it is pinned against a real local HTTP listener and a
    real file on disk rather than assumed;
  * ``find_external_refs`` / ``replace_refs`` — the regex scanner and the
    span rewriter the classifier's image inliner is built on.
"""

from __future__ import annotations

import base64
import http.server
import os
import threading
import time

import numpy as np
import pytest

from common.documents import (
    DEFAULT_MAX_RENDER_PIXELS,
    ExternalRef,
    UnsupportedDocumentError,
    find_external_refs,
    load_document,
    match_text,
    pdf_text_regions,
    replace_refs,
    svg_page_size,
)

from .documents_fixtures import make_png, make_svg


def _red_pixels(bgr: np.ndarray) -> int:
    """Pixels that are unmistakably red (the colour every probe image uses)."""
    b, g, r = bgr[:, :, 0], bgr[:, :, 1], bgr[:, :, 2]
    return int(np.count_nonzero((r > 200) & (g < 60) & (b < 60)))


def _red_png_b64() -> str:
    return base64.b64encode(make_png((20, 20), color="red")).decode("ascii")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_svg_loads_as_one_page_with_an_image_and_native_text() -> None:
    raw = make_svg(
        '<rect x="0" y="0" width="50" height="50" fill="red"/>'
        '<text x="60" y="40" font-size="20">Amount due $193.33</text>'
    )
    doc = load_document(raw, filename="bill.svg")
    assert doc.kind == "svg"
    assert doc.content_type == "image/svg+xml"
    assert len(doc.pages) == 1
    page = doc.pages[0]
    assert page.image_bgr is not None
    # 400×200 pt at the default 150 dpi (MuPDF rounds the edge outward).
    assert abs(page.width - 400 * 150 / 72) <= 1 and abs(page.height - 200 * 150 / 72) <= 1
    assert page.text_source == "native"
    assert "Amount due" in page.text
    # The text layer is the drawing's words, never its markup.
    assert "<rect" not in page.text and "fill=" not in page.text
    assert _red_pixels(page.image_bgr) > 0
    assert doc.warnings == []


def test_text_search_hits_svg_text_not_markup() -> None:
    doc = load_document(make_svg('<text x="10" y="40">Notice to Owner</text>'))
    assert match_text(doc, "Notice to Owner").found
    assert not match_text(doc, "font-size").found
    assert not match_text(doc, "xmlns").found


def test_svg_sizes_from_the_viewbox_when_there_is_no_width_or_height() -> None:
    doc = load_document(make_svg(width="", height="", view_box="0 0 400 300"), render_dpi=72)
    assert (doc.pages[0].width, doc.pages[0].height) == (400, 300)


def test_svg_mm_units_are_honoured() -> None:
    doc = load_document(make_svg(width="210mm", height="297mm"), render_dpi=72)
    # A4 in points is 595.3 × 841.9.
    assert abs(doc.pages[0].width - 595) <= 1
    assert abs(doc.pages[0].height - 842) <= 1


def test_a_huge_declared_size_renders_downscaled_under_the_cap() -> None:
    raw = make_svg('<rect width="100%" height="100%" fill="red"/>',
                   width="1000000", height="1000000")
    cap = 1_000_000
    doc = load_document(raw, max_render_pixels=cap)
    page = doc.pages[0]
    assert page.width * page.height <= cap
    assert page.width >= 900  # scaled to fit, not collapsed
    assert svg_page_size(raw, max_pixels=cap) == (page.width, page.height)


def test_the_default_cap_bounds_a_hostile_size() -> None:
    raw = make_svg(width="1000000", height="1000000")
    width, height = svg_page_size(raw)
    assert width * height <= DEFAULT_MAX_RENDER_PIXELS


def test_svg_page_size_matches_the_render() -> None:
    raw = make_svg('<circle cx="50" cy="50" r="40"/>', width="333", height="127")
    for dpi in (150, 97):
        doc = load_document(raw, render_dpi=dpi)
        assert svg_page_size(raw, render_dpi=dpi) == (doc.pages[0].width, doc.pages[0].height)


@pytest.mark.parametrize(
    "raw",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect',          # truncated
        make_svg(width="0", height="0"),                            # no area
    ],
    ids=["malformed", "zero-size"],
)
def test_unusable_svg_raises(raw: bytes) -> None:
    with pytest.raises(UnsupportedDocumentError):
        load_document(raw, kind="svg")
    with pytest.raises(UnsupportedDocumentError):
        svg_page_size(raw)


def test_external_references_become_warnings() -> None:
    raw = make_svg(
        '<image width="20" height="20" href="https://cdn.example/logo.png"/>'
        '<use xlink:href="https://cdn.example/sprites.svg#star"/>'
        f'<image width="20" height="20" href="data:image/png;base64,{_red_png_b64()}"/>'
    )
    doc = load_document(raw)
    assert doc.warnings == [
        "external image not rendered: https://cdn.example/logo.png",
        "external <use> reference not rendered: https://cdn.example/sprites.svg#star",
    ]
    # ...and the data: image, which needs no fetch, IS drawn.
    assert _red_pixels(doc.pages[0].image_bgr) > 0


def test_warnings_are_capped() -> None:
    body = "".join(
        f'<image width="1" height="1" href="https://cdn.example/{i}.png"/>' for i in range(25)
    )
    warnings = load_document(make_svg(body)).warnings
    assert len(warnings) == 11
    assert warnings[0] == "external image not rendered: https://cdn.example/0.png"
    assert warnings[-1] == "+15 more external reference(s) not rendered"


def test_pdf_text_regions_boxes_svg_text() -> None:
    raw = make_svg('<text x="60" y="40" font-size="20">Amount due</text>')
    doc = load_document(raw, keep_source=True)
    page = doc.pages[0]
    regions = pdf_text_regions(
        doc.source_bytes, page, [], pattern="Amount due", mode="contains", filetype="svg"
    )
    assert len(regions) == 1
    (x0, y0), (x1, y1) = regions[0].points
    scale = 150 / 72
    # Text starts at x=60 pt; the box is in page PIXELS.
    assert abs(x0 - 60 * scale) < 2
    assert 0 < y0 < y1 < page.height and x1 > x0
    assert regions[0].source == "pdf-text"


def test_pdf_text_regions_scale_follows_a_capped_render() -> None:
    raw = make_svg('<text x="1000" y="1000" font-size="200">Wide</text>',
                   width="4000", height="4000")
    doc = load_document(raw, keep_source=True, max_render_pixels=1_000_000)
    page = doc.pages[0]
    regions = pdf_text_regions(raw, page, [], pattern="Wide", mode="contains", filetype="svg")
    (x0, _), _ = regions[0].points
    assert abs(x0 - 1000 * page.width / 4000) < 2


def test_svg_calls_hold_the_pymupdf_lock(monkeypatch) -> None:
    """Every PyMuPDF entry point the svg kind uses runs under the lock."""
    from common.documents import loaders

    held: list[bool] = []
    real_open = __import__("pymupdf").open

    def spy_open(*args, **kwargs):
        held.append(loaders._PYMUPDF_LOCK._is_owned())
        return real_open(*args, **kwargs)

    raw = make_svg('<text x="10" y="40">lock</text>')
    doc = load_document(raw, keep_source=True)  # before the spy
    monkeypatch.setattr("pymupdf.open", spy_open)
    load_document(raw)
    svg_page_size(raw)
    pdf_text_regions(raw, doc.pages[0], [], pattern="lock", filetype="svg")
    assert held == [True, True, True]


# ---------------------------------------------------------------------------
# The no-fetch regression
# ---------------------------------------------------------------------------


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server's naming
        _Recorder.hits.append(self.path)
        # Serve a red PNG, so a fetch that DID happen would also be drawn.
        body = make_png((20, 20), color="red")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def listener():
    _Recorder.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_mupdf_fetches_nothing_an_svg_points_at(listener, tmp_path) -> None:
    """Every external-reference vector at once: zero requests, zero red."""
    red_png = tmp_path / "red.png"
    red_png.write_bytes(make_png((20, 20), color="red"))
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    file_png = "file:///" + str(red_png).replace(os.sep, "/").lstrip("/")
    file_secret = "file:///" + str(secret).replace(os.sep, "/").lstrip("/")

    prolog = (
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE svg SYSTEM "{listener}/external.dtd" [\n'
        f'  <!ENTITY xxe SYSTEM "{file_secret}">\n'
        f'  <!ENTITY xxe_http SYSTEM "{listener}/xxe">\n'
        ']>\n'
    )
    body = (
        f'<style>@import url({listener}/import.css);'
        f'@font-face{{font-family:x;src:url({listener}/font.ttf)}}</style>'
        f'<image x="0" y="0" width="40" height="40" xlink:href="{listener}/xlink.png"/>'
        f'<image x="40" y="0" width="40" height="40" href="{listener}/href.png"/>'
        f'<image x="80" y="0" width="40" height="40" xlink:href="{file_png}"/>'
        f'<image x="120" y="0" width="40" height="40" xlink:href="{red_png}"/>'
        '<image x="160" y="0" width="40" height="40" xlink:href="red.png"/>'
        f'<feImage href="{listener}/feimage.png"/>'
        f'<use xlink:href="{listener}/sprite.svg#x"/>'
        f'<rect x="0" y="50" width="40" height="40" style="fill:url({listener}/fill.svg#g)"/>'
        '<foreignObject x="0" y="100" width="100" height="50">'
        f'<img xmlns="http://www.w3.org/1999/xhtml" src="{listener}/fo.png"/></foreignObject>'
        '<text x="200" y="150" font-family="x">&xxe; &xxe_http;</text>'
    )
    raw = make_svg(body, prolog=prolog)

    # Opened from a stream with the CWD next to red.png, so even a relative
    # href has the best possible chance to resolve.
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        doc = load_document(raw, filename="hostile.svg")
    finally:
        os.chdir(cwd)
    time.sleep(0.3)  # let any request MuPDF had started reach the listener

    assert _Recorder.hits == []
    assert _red_pixels(doc.pages[0].image_bgr) == 0
    assert "TOP-SECRET-CONTENT" not in doc.pages[0].text
    # Everything it could have fetched is reported instead.
    assert any("xlink.png" in w for w in doc.warnings)
    assert any("sprite.svg" in w for w in doc.warnings)


def test_the_red_probe_would_have_seen_a_drawn_image() -> None:
    """Control for the regression above: a data: red PNG IS counted, so a
    zero there means nothing was drawn, not that the probe is blind."""
    raw = make_svg(
        f'<image width="40" height="40" href="data:image/png;base64,{_red_png_b64()}"/>'
    )
    assert _red_pixels(load_document(raw).pages[0].image_bgr) > 0


# ---------------------------------------------------------------------------
# find_external_refs / replace_refs
# ---------------------------------------------------------------------------


def test_refs_found_on_image_feimage_and_use_with_either_attribute() -> None:
    raw = make_svg(
        '<image href="https://a.example/1.png"/>'
        "<image xlink:href='https://a.example/2.png'/>"
        '<feImage xlink:href="https://a.example/3.png"/>'
        '<use href="https://a.example/4.svg#id"/>'
    )
    refs = find_external_refs(raw)
    assert [(r.tag, r.attr, r.url) for r in refs] == [
        ("image", "href", "https://a.example/1.png"),
        ("image", "xlink:href", "https://a.example/2.png"),
        ("feImage", "xlink:href", "https://a.example/3.png"),
        ("use", "href", "https://a.example/4.svg#id"),
    ]
    for ref in refs:
        assert raw[ref.start:ref.end].decode() == ref.url


def test_fragments_data_and_empty_values_are_not_external() -> None:
    raw = make_svg(
        '<use href="#local"/>'
        '<image href="data:image/png;base64,AAAA"/>'
        '<image href="DATA:image/png;base64,AAAA"/>'
        '<image href=""/>'
        '<image href="   "/>'
        '<a href="https://a.example/link"><text>not a fetching element</text></a>'
    )
    assert find_external_refs(raw) == []


def test_refs_in_comments_cdata_and_the_doctype_are_ignored() -> None:
    raw = make_svg(
        '<!-- <image href="https://commented.example/x.png"/> -->'
        '<style><![CDATA[ <image href="https://cdata.example/x.png"/> ]]></style>'
        '<image href="https://real.example/x.png"/>',
        prolog='<!DOCTYPE svg [<!ENTITY e "<image href=\'https://entity.example/x.png\'/>">]>',
    )
    assert [r.url for r in find_external_refs(raw)] == ["https://real.example/x.png"]


def test_an_href_inside_another_attribute_value_is_not_an_href() -> None:
    raw = make_svg('<image title=\' href="https://fake.example/x.png"\' href="https://a.example/y.png"/>')
    assert [r.url for r in find_external_refs(raw)] == ["https://a.example/y.png"]


def test_the_five_xml_entities_are_unescaped() -> None:
    raw = make_svg('<image href="https://a.example/i.png?a=1&amp;b=&lt;2&gt;&quot;&apos;"/>')
    (ref,) = find_external_refs(raw)
    assert ref.url == "https://a.example/i.png?a=1&b=<2>\"'"


def test_replace_refs_rewrites_only_the_values() -> None:
    raw = make_svg(
        '<image width="10" height="10" href="https://a.example/1.png"/>'
        "<image width='10' height='10' xlink:href='https://a.example/2.png'/>"
        '<use href="https://a.example/3.svg#x"/>'
    )
    refs = find_external_refs(raw)
    new_value = 'data:x;base64,AB&"<\''
    out = replace_refs(raw, {refs[0]: new_value, refs[1]: ""})
    assert b'href="data:x;base64,AB&amp;&quot;&lt;&apos;"' in out
    assert b"xlink:href=''" in out
    assert b'width="10" height="10"' in out and b"width='10' height='10'" in out
    # The <use> was not in the mapping and is untouched; a re-scan of the
    # output sees exactly what it should.
    rescanned = find_external_refs(out)
    assert [r.url for r in rescanned] == ["https://a.example/3.svg#x"]
    # Escaped on the way in, unescaped on the way out: a round trip.
    out2 = replace_refs(raw, {refs[0]: "https://b.example/x?a=1&b=2"})
    assert find_external_refs(out2)[0].url == "https://b.example/x?a=1&b=2"


def test_replace_refs_with_nothing_returns_the_bytes() -> None:
    raw = make_svg('<image href="https://a.example/1.png"/>')
    assert replace_refs(raw, {}) is raw


def test_replace_refs_refuses_overlapping_spans() -> None:
    raw = make_svg('<image href="https://a.example/1.png"/>')
    (ref,) = find_external_refs(raw)
    overlapping = ExternalRef(tag="image", attr="href", url="x", start=ref.start + 2, end=ref.end)
    with pytest.raises(ValueError):
        replace_refs(raw, {ref: "a", overlapping: "b"})


def test_an_inlined_image_is_drawn() -> None:
    """The whole point of replace_refs: blank an external href or swap in a
    data: URI, and MuPDF draws what it was given."""
    raw = make_svg('<image width="40" height="40" href="https://cdn.example/red.png"/>')
    (ref,) = find_external_refs(raw)
    inlined = replace_refs(raw, {ref: f"data:image/png;base64,{_red_png_b64()}"})
    doc = load_document(inlined)
    assert _red_pixels(doc.pages[0].image_bgr) > 0
    assert doc.warnings == []


# ---------------------------------------------------------------------------
# The scanner stays linear on hostile input
# ---------------------------------------------------------------------------

_HOSTILE_N = 20_000
_HOSTILE_HEAD = b'<svg xmlns="http://www.w3.org/2000/svg">'


@pytest.mark.parametrize(
    "raw",
    [
        _HOSTILE_HEAD + b"<image " * _HOSTILE_N,                    # tags never closed
        _HOSTILE_HEAD + b'<image "' * _HOSTILE_N,                   # quotes never closed
        _HOSTILE_HEAD + b"<image a='" * _HOSTILE_N,
        _HOSTILE_HEAD + b"<!DOCTYPE x " + b"[]" * _HOSTILE_N,        # bracket soup, no >
        _HOSTILE_HEAD + b"<!DOCTYPE [" * _HOSTILE_N + b"]<",
        _HOSTILE_HEAD + b"<!DOCTYPE x" * _HOSTILE_N,
        _HOSTILE_HEAD + b"<image " + b"a" * (_HOSTILE_N * 5) + b">",  # one long name, no =
        _HOSTILE_HEAD + b"<!--" * _HOSTILE_N,
        _HOSTILE_HEAD + b"<![CDATA[" * _HOSTILE_N,
    ],
    ids=[
        "unclosed-tags", "unclosed-double-quotes", "unclosed-single-quotes",
        "doctype-brackets", "doctype-nested-subsets", "doctype-unclosed",
        "long-attribute-name", "unclosed-comments", "unclosed-cdata",
    ],
)
def test_find_external_refs_is_linear_on_hostile_input(raw: bytes) -> None:
    """Every one of these took tens of seconds (or never finished) at ~150 KB
    before the patterns were bounded — a caller-supplied file must not be able
    to pin a worker in the regex engine. A linear scan does them in
    milliseconds; 2 s is slack for a slow CI box, not a target."""
    started = time.perf_counter()
    find_external_refs(raw)
    assert time.perf_counter() - started < 2.0


def test_the_bounded_patterns_still_find_real_refs() -> None:
    """The ``<``-bounded tag scan and the name-boundary attribute rule must
    not lose an href that a renderer would see: a ``>`` inside a quoted
    value, attributes separated by newlines, and a DOCTYPE subset before the
    root."""
    raw = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE svg [<!ENTITY e "<image href=\'https://masked.example/x.png\'/>">]>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        b'<image title="a > b"\n  xlink:href="https://a.example/1.png"/>'
        b'<use\thref="https://b.example/s.svg#x"/>'
        b"</svg>"
    )
    urls = [r.url for r in find_external_refs(raw)]
    assert urls == ["https://a.example/1.png", "https://b.example/s.svg#x"]
