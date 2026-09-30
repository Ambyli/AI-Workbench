"""The throughput changes: work moved off the event loop, and work done once.

  * ``DocumentContext.ask_image_b64`` — the box loop's gridded ask image is
    built ONCE per item, in a worker thread, however many located criteria
    ask for it at the same time.
  * ``jobs.runners.run_assess`` — documents load in worker threads,
    concurrently, and a failure is reported in DOCUMENT order.
  * ``llm.client._client`` — one pooled HTTP client per event loop, reused
    by every model call on it, closed by ``aclose``.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_speed_paths.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time

import numpy as np
import pytest

from common.documents import Document, Page

from analysis.context import DocumentContext
from jobs import runners
from jobs.payloads import PAYLOAD_SCHEMA
from llm import boxes
from llm import client as llm_client


def _ctx() -> DocumentContext:
    img = np.full((200, 300, 3), 255, dtype=np.uint8)
    page = Page(index=0, image_bgr=img, text="", text_source="none", width=300, height=200)
    doc = Document(kind="image", content_type="image/png", filename="p.png", pages=[page])
    return DocumentContext.build(doc)


# ── The ask image, once per item ──────────────────────────────────────────


def test_ask_image_is_built_once_per_item_off_the_loop(monkeypatch):
    calls: list[str] = []
    real = boxes.ask_image_b64

    def counting(image_b64, working_image, *, gridlines):
        calls.append(threading.current_thread().name)
        time.sleep(0.05)  # long enough that the concurrent askers overlap it
        return real(image_b64, working_image, gridlines=gridlines)

    monkeypatch.setattr(boxes, "ask_image_b64", counting)
    monkeypatch.setattr(boxes, "LLM_BBOX_GRIDLINES", True)
    ctx = _ctx()

    async def main():
        return await asyncio.gather(*(ctx.ask_image_b64() for _ in range(6)))

    results = asyncio.run(main())
    assert len(calls) == 1, "every concurrent criterion must share one build"
    assert calls[0] != threading.main_thread().name, "the build must not run on the loop"
    assert len(set(results)) == 1
    assert results[0] is not ctx.image_b64(), "gridlines on: the ask image is the gridded copy"


def test_ask_image_is_the_page_image_itself_when_gridlines_are_off(monkeypatch):
    # The box loop tells "grid drawn" from "bare page" by identity.
    monkeypatch.setattr(boxes, "LLM_BBOX_GRIDLINES", False)
    ctx = _ctx()
    assert asyncio.run(ctx.ask_image_b64()) is ctx.image_b64()


def test_ask_image_failure_reaches_every_waiter(monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("grid broke")

    monkeypatch.setattr(boxes, "ask_image_b64", broken)
    ctx = _ctx()

    async def main():
        return await asyncio.gather(
            *(ctx.ask_image_b64() for _ in range(3)), return_exceptions=True
        )

    outcomes = asyncio.run(main())
    assert all(isinstance(o, RuntimeError) for o in outcomes)


# ── Document loading in the runner ────────────────────────────────────────


def _payload(names: list[str]) -> dict:
    return {
        "schema": PAYLOAD_SCHEMA,
        "job_id": None,
        "criteria": [{"name": "anything", "type": "llm"}],
        "documents": [
            {"file_b64": base64.b64encode(n.encode()).decode(), "filename": n}
            for n in names
        ],
    }


def test_documents_load_concurrently_in_worker_threads(monkeypatch):
    threads: list[str] = []
    in_flight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def fake_load(raw, filename, content_type, **_kw):
        with lock:
            threads.append(threading.current_thread().name)
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        time.sleep(0.05)
        with lock:
            in_flight["now"] -= 1
        return filename

    seen: list = []

    async def fake_analyze(docs, criteria, *, job_id=None):
        seen.extend(docs)
        return {"ok": True}

    monkeypatch.setattr(runners, "load_document_bytes", fake_load)
    monkeypatch.setattr(runners, "analyze_document", fake_analyze)

    assert asyncio.run(runners.run_assess(_payload(["a", "b", "c"]))) == {"ok": True}
    assert seen == ["a", "b", "c"], "documents keep their submitted order"
    assert threading.main_thread().name not in threads
    assert in_flight["peak"] > 1, "the documents of one job load concurrently"


def test_first_failure_in_document_order_is_the_one_reported(monkeypatch):
    def fake_load(raw, filename, content_type, **_kw):
        # The FIRST document fails last; completion order must not decide.
        time.sleep({"a": 0.1, "b": 0.0, "c": 0.0}[filename])
        if filename in ("a", "b"):
            raise ValueError(f"bad {filename}")
        return filename

    monkeypatch.setattr(runners, "load_document_bytes", fake_load)
    with pytest.raises(ValueError, match="bad a"):
        asyncio.run(runners.run_assess(_payload(["a", "b", "c"])))


# ── One pooled HTTP client per event loop ─────────────────────────────────


def test_http_client_is_reused_within_a_loop_and_closed_by_aclose():
    async def main():
        first, second = llm_client._client(), llm_client._client()
        assert first is second
        await llm_client.aclose()
        assert first.is_closed
        third = llm_client._client()
        assert third is not first and not third.is_closed
        await llm_client.aclose()

    asyncio.run(main())


def test_each_event_loop_gets_its_own_client():
    async def grab():
        c = llm_client._client()
        await llm_client.aclose()
        return c

    assert asyncio.run(grab()) is not asyncio.run(grab())
