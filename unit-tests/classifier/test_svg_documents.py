"""SVG documents end to end: the svg kind, its warnings, and the opt-in fetch.

An SVG used to be detected as plain text and searched as markup. It is now a
document kind of its own — one item, rendered by MuPDF like a one-page PDF,
its ``<text>`` the native text layer — and what MuPDF does not draw (every
external reference: it fetches nothing) is reported in the result's
``documents[].warnings``. With CLASSIFIER_SVG_FETCH_IMAGES on, the submit
fetches each external ``<image>`` itself (``common.net.fetch_url``, SSRF
guard, no redirects, bounded) and inlines it as a ``data:`` URI before the
job is queued. What is pinned here:

  * the request — JSON base64 and multipart ``image/svg+xml`` both give a
    ``kind: "svg"`` document with one item and its warnings; ``.svgz`` is a
    400 at submit;
  * text — a ``text`` criterion matches the drawing's words (with
    ``pdf-text`` boxes) and never its markup;
  * introspection — ``/document-kinds`` lists svg and refuses svgz;
  * references — an SVG is accepted as a reference page;
  * the fetch — an inlined image is drawn (the stored page image has its
    pixels), a blocked / non-image / over-the-cap link becomes a warning
    naming why, the fetch happens at SUBMIT (before the 202), and with the
    flag off nothing is fetched at all.

No network: ``fetch_url`` is replaced in ``analysis.loading`` and the vision
model is scripted at ``llm.client._send``.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_svg_documents.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import pathlib
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
from analysis import loading
from common.net import BlockedURLError, FetchError
from llm import client as llm_client

HERE = pathlib.Path(__file__).resolve().parent
DIAGRAM = (HERE / "documents" / "diagram.svg").read_bytes()
DIAGRAM_LOGO = "https://assets.acme-roofing.example/logo.png"

SVG_NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    import main

    with TestClient(main.app) as c:
        yield c


class Model:
    """Transport fake: every scoring call answers 9 PASS."""

    def __init__(self):
        self.prompts: list[str] = []

    async def __call__(self, prompt):
        self.prompts.append(json.dumps(prompt))
        body = {"score": 9, "verdict": "PASS", "confidence": 70, "reason": "scripted",
                "description": "A roof plan."}
        return {"choices": [{"message": {"content": json.dumps(body)}}]}


@pytest.fixture
def model(monkeypatch):
    fake = Model()
    monkeypatch.setattr(llm_client, "_send", fake)
    return fake


class Fetcher:
    """Stands in for ``common.net.fetch_url``: answers per URL, records calls."""

    def __init__(self, answers=None, default=None):
        self.answers = dict(answers or {})
        self.default = default
        self.calls: list[str] = []

    async def __call__(self, url, **kwargs):
        self.calls.append(url)
        answer = self.answers.get(url, self.default)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise FetchError("HTTP 404 fetching the URL", reason="HTTP 404")
        return answer


@pytest.fixture
def fetch_on(monkeypatch):
    """CLASSIFIER_SVG_FETCH_IMAGES=true, with the fetch replaced."""
    fetcher = Fetcher()
    monkeypatch.setattr(config, "SVG_FETCH_IMAGES", True)
    monkeypatch.setattr(loading, "fetch_url", fetcher)
    return fetcher


def _png(color=(255, 0, 0), size=(20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _svg(body: str, width=200, height=100) -> bytes:
    return f'<svg {SVG_NS} width="{width}" height="{height}">{body}</svg>'.encode()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _wait(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["phase"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _done(client, response):
    assert response.status_code == 202, response.text
    job = _wait(client, response.json()["job_id"])
    assert job["phase"] == "completed", job
    return job


def _assess_svg(client, raw: bytes, criteria=None, filename="drawing.svg"):
    return client.post("/assess", json={
        "documents": [{"type": "base64", "data": _b64(raw), "filename": filename}],
        "criteria": criteria or [{"name": "sharpness", "type": "cv"}],
    })


def _red_pixels(job_id: str, client) -> int:
    jpeg = client.get(f"/jobs/{job_id}/artifacts/p0.base.jpg")
    assert jpeg.status_code == 200, jpeg.text
    rgb = np.array(Image.open(io.BytesIO(jpeg.content)).convert("RGB"))
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return int(np.count_nonzero((r > 200) & (g < 60) & (b < 60)))


# ---------------------------------------------------------------------------
# The svg kind
# ---------------------------------------------------------------------------


def test_json_base64_svg_is_one_svg_item_with_its_warnings(client, model):
    job = _done(client, _assess_svg(client, DIAGRAM, [
        {"name": "System Size: 8.4 kW", "type": "text"},
        {"name": "markup", "type": "text", "options": {"pattern": "xlink:href"}},
        {"name": "sharpness", "type": "cv"},
    ], filename="diagram.svg"))
    result = job["result"]
    (doc,) = result["documents"]
    assert (doc["kind"], doc["pages"], doc["items"]) == ("svg", 1, [0])
    assert doc["document_info"]["content_type"] == "image/svg+xml"
    assert doc["document_info"]["has_image"] is True
    assert doc["warnings"] == [f"external image not rendered: {DIAGRAM_LOGO}"]
    assert len(result["items"]) == 1

    entries = result["assessment"]["per_criterion_scores"]
    size = entries["System Size: 8.4 kW"]
    assert size["verdict"] == "PASS"
    # The words were found on the drawing, and boxed where they are drawn.
    assert any(r["source"] == "pdf-text" for r in size["regions"])
    # The markup is not the text layer.
    assert entries["markup"]["verdict"] == "FAIL"
    # And the cv criterion had a page image to measure.
    assert entries["sharpness"]["status"] == "ok"


def test_multipart_svg_is_accepted_and_detected(client, model):
    r = client.post(
        "/assess",
        files={"file": ("diagram.svg", DIAGRAM, "image/svg+xml")},
        data={"criteria": json.dumps([{"name": "ROOF PLAN", "type": "text"}])},
    )
    job = _done(client, r)
    (doc,) = job["result"]["documents"]
    assert doc["kind"] == "svg" and doc["filename"] == "diagram.svg"
    assert job["result"]["assessment"]["per_criterion_scores"]["ROOF PLAN"]["verdict"] == "PASS"


def test_a_plain_document_has_no_warnings(client, model):
    job = _done(client, client.post("/assess", json={
        "documents": [{"type": "text", "data": "Limited Warranty"}],
        "criteria": [{"name": "Limited Warranty", "type": "text"}],
    }))
    assert job["result"]["documents"][0]["warnings"] == []


def test_svgz_is_refused_at_submit(client):
    import gzip

    r = _assess_svg(client, gzip.compress(DIAGRAM), filename="diagram.svgz")
    assert r.status_code == 400
    assert ".svgz" in r.json()["detail"] and "decompress" in r.json()["detail"]


def test_document_kinds_lists_svg(client):
    body = client.get("/document-kinds").json()
    svg = next(k for k in body["kinds"] if k["kind"] == "svg")
    assert svg["extensions"] == [".svg"]
    assert svg["content_types"] == ["image/svg+xml"]
    assert svg["has_page_images"] is True and svg["native_text"] is True
    assert svg["external_images"]["fetch"] is config.SVG_FETCH_IMAGES
    assert "svgz" in {u["kind"] for u in body["unsupported"]}
    assert body["limits"]["svg_max_render_pixels"] > 0


def test_a_reference_accepts_an_svg_page(client, model):
    r = client.post("/references", json={
        "document": {"type": "base64", "data": _b64(DIAGRAM), "filename": "diagram.svg"},
        "criteria": [{"name": "sharpness", "type": "cv"}],
        "description": "A roof plan.",
    })
    assert r.status_code == 202, r.text
    reference_id = r.json()["reference_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ref = client.get(f"/references/{reference_id}").json()
        if ref["status"] in ("ready", "failed"):
            break
        time.sleep(0.05)
    assert ref["status"] == "ready", ref
    record = ref["record"]
    assert record["page"]["kind"] == "svg"
    assert record["page"]["geometry"]["width"] > 0
    # The page's "not rendered" line is on the reference too.
    assert f"external image not rendered: {DIAGRAM_LOGO}" in record["warnings"]


def test_a_reference_svg_page_must_be_page_0(client, model):
    r = client.post("/references", json={
        "document": {"type": "base64", "data": _b64(DIAGRAM), "filename": "diagram.svg"},
        "page": 1,
        "criteria": [{"name": "sharpness", "type": "cv"}],
    })
    assert r.status_code == 400 and "one page" in r.json()["detail"]


# ---------------------------------------------------------------------------
# CLASSIFIER_SVG_FETCH_IMAGES
# ---------------------------------------------------------------------------


def test_flag_off_fetches_nothing(client, model, monkeypatch):
    fetcher = Fetcher(default=_png())
    monkeypatch.setattr(config, "SVG_FETCH_IMAGES", False)
    monkeypatch.setattr(loading, "fetch_url", fetcher)
    job = _done(client, _assess_svg(client, DIAGRAM))
    assert fetcher.calls == []
    assert job["result"]["documents"][0]["warnings"] == [
        f"external image not rendered: {DIAGRAM_LOGO}"
    ]


def test_an_inlined_image_is_drawn(client, model, fetch_on):
    url = "https://cdn.test/red.png"
    fetch_on.answers[url] = _png()
    raw = _svg(f'<image x="0" y="0" width="200" height="100" href="{url}"/>')
    response = _assess_svg(client, raw)
    # Fetched AT SUBMIT: the call was made before the 202 came back.
    assert fetch_on.calls == [url]
    job = _done(client, response)
    assert job["result"]["documents"][0]["warnings"] == []
    assert _red_pixels(job["job_id"], client) > 1000


def test_failed_links_are_blanked_and_warned_about(client, model, fetch_on):
    blocked = "https://internal.test/a.png"
    html = "https://cdn.test/page.html"
    redirect = "https://cdn.test/moved.png"
    fetch_on.answers = {
        blocked: BlockedURLError("URL resolves to a blocked network address."),
        html: b"<html><body>Not an image</body></html>",
        redirect: FetchError("redirect not followed (HTTP 302)", reason="redirect not followed"),
    }
    raw = _svg(
        f'<image width="10" height="10" href="{blocked}"/>'
        f'<image width="10" height="10" xlink:href="{html}"/>'
        f'<image width="10" height="10" href="{redirect}"/>'
        '<image width="10" height="10" href="relative/logo.png"/>'
        '<use href="https://cdn.test/sprite.svg#x"/>'
    )
    job = _done(client, _assess_svg(client, raw))
    assert job["result"]["documents"][0]["warnings"] == [
        "external image not fetched (blocked: URL resolves to a blocked network address): "
        f"{blocked}",
        f"external image not fetched (not a PNG or JPEG image): {html}",
        f"external image not fetched (redirect not followed): {redirect}",
        "external image not fetched (not an http(s) URL): relative/logo.png",
        # <use> is not fetched — the loader still reports it, once.
        "external <use> reference not rendered: https://cdn.test/sprite.svg#x",
    ]
    # The relative path never reached the fetcher.
    assert sorted(fetch_on.calls) == sorted([blocked, html, redirect])


def test_links_over_the_count_cap_are_not_fetched(monkeypatch, fetch_on):
    monkeypatch.setattr(loading, "SVG_FETCH_MAX_IMAGES", 2)
    fetch_on.default = _png()
    urls = [f"https://cdn.test/{i}.png" for i in range(3)]
    # The first URL twice: one distinct URL, one fetch, both hrefs inlined.
    raw = _svg("".join(f'<image width="5" height="5" href="{u}"/>' for u in urls + urls[:1]))
    out, warnings = asyncio.run(loading.resolve_svg_images(raw))
    assert fetch_on.calls == urls[:2]
    assert warnings == [f"external image not fetched (over the 2-image limit): {urls[2]}"]
    from common.documents import find_external_refs

    assert find_external_refs(out) == []  # every href inlined or blanked
    assert out.count(b"data:image/png;base64,") == 3


def test_a_fetch_that_raises_unexpectedly_is_still_only_a_warning(fetch_on):
    fetch_on.default = RuntimeError("boom")
    raw = _svg('<image width="5" height="5" href="https://cdn.test/x.png"/>')
    out, warnings = asyncio.run(loading.resolve_svg_images(raw))
    assert warnings == ["external image not fetched (fetch failed): https://cdn.test/x.png"]
    assert b'href=""' in out


def test_no_external_images_is_a_no_op(fetch_on):
    raw = _svg('<rect width="5" height="5"/>')
    out, warnings = asyncio.run(loading.resolve_svg_images(raw))
    assert out is raw and warnings == [] and fetch_on.calls == []


def test_a_reference_fetches_at_submit_too(client, model, fetch_on):
    fetch_on.answers[DIAGRAM_LOGO] = BlockedURLError("URL hostname could not be resolved.")
    r = client.post("/references", json={
        "document": {"type": "base64", "data": _b64(DIAGRAM), "filename": "diagram.svg"},
        "criteria": [{"name": "sharpness", "type": "cv"}],
        "description": "A roof plan.",
    })
    assert r.status_code == 202, r.text
    assert fetch_on.calls == [DIAGRAM_LOGO]
    reference_id = r.json()["reference_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ref = client.get(f"/references/{reference_id}").json()
        if ref["status"] in ("ready", "failed"):
            break
        time.sleep(0.05)
    assert ref["status"] == "ready", ref
    assert ref["record"]["warnings"] == [
        "external image not fetched (blocked: URL hostname could not be resolved): "
        f"{DIAGRAM_LOGO}"
    ]
