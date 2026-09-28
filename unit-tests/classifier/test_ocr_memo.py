"""The OCR memo: one pass per distinct setting, shared by every criterion.

``analysis.context.DocumentContext.text_layer`` keys each text layer by its
resolved OCR settings, per ITEM. Criteria with the same settings must share
ONE recognition pass per page (and one ``text.p{n}.<key>.json``); different
settings must each get their own, concurrently, bounded by
CLASSIFIER_OCR_WORKERS; each page of a multi-page document has its own memo;
and a criterion that needs no text must not load the engine at all.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_ocr_memo.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest

from common.documents import Document, OCRResult, Page

import analysis
from analysis import context as context_module
from analysis import ocr
from analysis.context import DocumentContext, layer_key, ocr_settings
from api.schemas import CriterionInput


class CountingEngine:
    """Records every recognise call and how many overlapped (threads)."""

    def __init__(self, delay=0.0, fail=False):
        self.calls = 0
        self.delay = delay
        self.fail = fail
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def recognize(self, image_bgr):
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(self.delay)
            if self.fail:
                raise RuntimeError("onnx exploded")
            return OCRResult(
                text="NOTICE TO OWNER", confidence=0.9,
                lines=[{"text": "NOTICE TO OWNER", "confidence": 0.9,
                        "box": [[1, 1], [50, 1], [50, 10], [1, 10]]}],
            )
        finally:
            with self._lock:
                self.in_flight -= 1


@pytest.fixture
def engine(monkeypatch):
    fake = CountingEngine()
    monkeypatch.setattr(ocr, "_ocr_engine", fake)
    return fake


def _page(text=""):
    return Page(index=0, image_bgr=np.zeros((100, 200, 3), dtype=np.uint8), text=text,
                text_source="native" if text.strip() else "none", width=200, height=100)


def _ctx(text="", sink=None):
    return DocumentContext.build(Document(kind="image", pages=[_page(text)]), layer_sink=sink)


# ── Keys ───────────────────────────────────────────────────────────────────


def test_keys_are_the_bare_mode_for_server_defaults():
    assert [layer_key(ocr_settings(m)) for m in ("auto", "always", "never")] == [
        "auto", "always", "never"
    ]


def test_a_non_default_setting_gets_its_own_hashed_key():
    key = layer_key(ocr_settings("auto", min_native_chars=500))
    assert key.startswith("auto-") and len(key) == len("auto-") + 8
    assert key != layer_key(ocr_settings("auto", min_native_chars=501))


# ── Sharing ────────────────────────────────────────────────────────────────


def test_same_settings_share_one_pass(engine):
    ctx = _ctx()

    async def main():
        return await asyncio.gather(*(ctx.text_layer("always") for _ in range(5)))

    layers = asyncio.run(main())
    assert engine.calls == 1
    assert all(layer is layers[0] for layer in layers)
    assert layers[0].source == "ocr"
    assert [p["key"] for p in ctx.ocr_passes] == ["always"]


def test_different_settings_get_separate_passes(engine):
    ctx = _ctx()

    async def main():
        return await asyncio.gather(
            ctx.text_layer("always"), ctx.text_layer("auto"), ctx.text_layer("never")
        )

    always, auto, never = asyncio.run(main())
    # always and auto both recognise this image-only page; never does not.
    assert engine.calls == 2
    assert always.source == auto.source == "ocr"
    assert never.source == "none"
    assert sorted(p["key"] for p in ctx.ocr_passes) == ["always", "auto", "never"]
    assert {p["key"]: p["ran"] for p in ctx.ocr_passes} == {
        "always": True, "auto": True, "never": False
    }


def test_auto_skips_recognition_when_native_text_is_enough(engine):
    ctx = _ctx("This page already carries plenty of native text to search.")
    layer = asyncio.run(ctx.text_layer("auto"))
    assert engine.calls == 0 and layer.source == "native"


def test_separate_passes_run_concurrently_within_the_ocr_limit(monkeypatch):
    fake = CountingEngine(delay=0.1)
    monkeypatch.setattr(ocr, "_ocr_engine", fake)
    monkeypatch.setattr(context_module.OCR_PASSES, "limit", 4)
    async def two_settings(ctx):
        # Two distinct recognising settings: "always" and a non-default "auto".
        await asyncio.gather(
            ctx.text_layer("always"), ctx.text_layer("auto", min_native_chars=999)
        )

    asyncio.run(two_settings(_ctx()))
    assert fake.calls == 2 and fake.peak == 2

    fake = CountingEngine(delay=0.05)
    monkeypatch.setattr(ocr, "_ocr_engine", fake)
    monkeypatch.setattr(context_module.OCR_PASSES, "limit", 1)
    asyncio.run(two_settings(_ctx()))
    assert fake.calls == 2 and fake.peak == 1


def test_a_failed_pass_fails_every_criterion_that_shares_it(monkeypatch):
    fake = CountingEngine(fail=True)
    monkeypatch.setattr(ocr, "_ocr_engine", fake)
    ctx = _ctx()

    async def main():
        return await asyncio.gather(
            ctx.text_layer("always"), ctx.text_layer("always"), return_exceptions=True
        )

    results = asyncio.run(main())
    assert all(isinstance(r, RuntimeError) for r in results)
    assert fake.calls == 1


def test_the_sink_sees_each_layer_once(engine):
    written = []

    async def sink(key, settings, layer, engine_name):
        written.append((key, settings["mode"], layer.source, engine_name))

    ctx = _ctx(sink=sink)

    async def main():
        await asyncio.gather(
            ctx.text_layer("always"), ctx.text_layer("always"), ctx.text_layer("never")
        )

    asyncio.run(main())
    assert sorted(written) == [
        ("always", "always", "ocr", "CountingEngine"),
        ("never", "never", "none", None),
    ]


# ── Through the pipeline ───────────────────────────────────────────────────


def _png():
    import cv2

    return cv2.imencode(".png", np.full((120, 240, 3), 200, dtype=np.uint8))[1].tobytes()


def _run(criteria):
    doc = analysis.load_document_bytes(_png(), "p.png", None)
    return asyncio.run(analysis.analyze_document(doc, criteria))


def test_criteria_with_identical_settings_share_the_pass_in_a_job(engine):
    result = _run([
        CriterionInput(name="NOTICE", type="text"),
        CriterionInput(name="OWNER", type="text"),
        CriterionInput(name="TO", type="text", options={"ocr": "auto"}),
    ])
    assert engine.calls == 1
    assert [p["key"] for p in result["documents"][0]["document_info"]["ocr"]["layers"]] == ["auto"]
    assert all(
        e["verdict"] == "PASS" for e in result["assessment"]["per_criterion_scores"].values()
    )


def test_criteria_with_different_settings_each_get_a_pass(engine):
    result = _run([
        CriterionInput(name="NOTICE", type="text", options={"ocr": "never"}),
        CriterionInput(name="OWNER", type="text", options={"ocr": "always"}),
    ])
    entries = result["assessment"]["per_criterion_scores"]
    assert engine.calls == 1  # never does not recognise
    assert entries["NOTICE"]["verdict"] == "FAIL"
    assert entries["OWNER"]["verdict"] == "PASS"
    assert sorted(p["key"] for p in result["documents"][0]["document_info"]["ocr"]["layers"]) == [
        "always", "never"
    ]


def test_criteria_that_need_no_text_never_touch_the_engine(monkeypatch):
    def explode():
        raise AssertionError("the OCR engine was loaded")

    monkeypatch.setattr(context_module, "get_ocr_engine", explode)
    result = _run([CriterionInput(name="sharpness", type="cv")])
    assert result["documents"][0]["document_info"]["ocr"]["layers"] == []


def test_every_item_has_its_own_memo(engine):
    """Two photos, two criteria with one setting: one pass PER ITEM, not per job."""
    docs = [analysis.load_document_bytes(_png(), f"p{i}.png", None) for i in range(2)]
    result = asyncio.run(analysis.analyze_document(docs, [
        CriterionInput(name="NOTICE", type="text"),
        CriterionInput(name="OWNER", type="text"),
    ]))
    assert engine.calls == 2
    layers = [d["document_info"]["ocr"]["layers"] for d in result["documents"]]
    assert [[(p["item"], p["key"]) for p in doc] for doc in layers] == [[(0, "auto")], [(1, "auto")]]
