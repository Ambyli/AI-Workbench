"""The item context: everything criteria share about ONE page, computed once.

A request carries a list of documents, and every page of every document is
one ITEM. The unit of work is (criterion, item) — exactly the single-page
evaluation this service always did — so what a unit shares is built here,
once per item, and never written to by an evaluator:

    DocumentContext  ONE item (the name is historical: it is the context an
                     evaluator receives, and an evaluator only ever sees one
                     page):
        doc            a one-page VIEW of the loaded ``Document`` — the same
                       kind, filename and ``source_bytes``, ``pages`` holding
                       just this page, so ``match_text`` searches this page
                       alone and ``pdf_text_regions`` still re-opens the
                       right file (``page.index`` stays the page's index IN
                       ITS DOCUMENT, which is what PyMuPDF needs)
        page           that page
        item           the GLOBAL item index n — the page index the shared
                       vision code draws on: ``geometry.page == n``, layer
                       files are ``p{n}.*``, and every region is re-stamped
                       ``page = n`` by the scheduler
        document       the index of the document the item belongs to
        working_image  the <=MAX_WORKING_DIMENSION copy every detector and the
                       vision model see (None for .txt / .docx)
        geometry       the ``PageGeometry`` mapping working pixels back to the
                       original page (None when there is no image)
        image_b64()    the working image as the base64 JPEG the prompts attach,
                       encoded on first use and cached
        references     the job's ``analysis.references.JobReferences`` (one
                       object shared by every item), or None when the request
                       listed no references
    DocumentGroup    one document and its items, in page order — what a
                     ``text`` criterion with ``options.scope: "document"``
                     searches, and what the ``pages`` level of an aggregate
                     collapses.

The one thing that is NOT computed up front is the text layer, because it
depends on the criterion: ``text_layer(mode)`` returns the layer an OCR
setting gives this page, MEMOISED PER ITEM by the setting. Criteria with
identical settings share one pass per page; different settings each get
their own pass, run concurrently (bounded by the process-wide
CLASSIFIER_OCR_WORKERS). A criterion that needs no text never asks, so a job
with no text / llm criteria never loads the OCR engine.

    ocr_settings() the resolved settings a layer is keyed by — the mode, plus
                   the server-level values that change the result
                   (min_native_chars, the engine). Every one of them is in
                   the memo key AND in the artifact file.
    layer_key()    the short, filename-safe name for a settings dict: the
                   mode alone when every other setting is the server default
                   (``auto``, ``always``, ``never``), ``<mode>-<hash8>`` when
                   one is not, so a future per-criterion OCR setting gets its
                   own file instead of overwriting another's.
    text_layer()   the memoised ``TextLayer``, via
                   ``common.documents.recognize_text_layer`` in a worker
                   thread — it never mutates the page, which is what lets two
                   settings coexist.
    text_document() a view of the item carrying that layer, for
                   ``match_text`` and ``pdf_text_regions``.
    layer_ref()    ``{"key", "name", "source", "chars"}`` — what a criterion
                   result links to; ``name`` is the artifact file,
                   ``text.p{n}.<key>.json``.
    ocr_passes     what actually ran on this item, for ``document_info.ocr``.

Every layer, native or recognised, is handed to ``layer_sink`` the moment it
is created — the pipeline wires that to the job's artifact directory, so the
exact text a criterion searched is stored as ``text.p{n}.<key>.json`` once
per item and distinct setting (see ``regions.artifacts.text_layer_payload``).
A document-scope search's joined text is stored the same way, once per
document and setting, as ``text.d{i}.<key>.json`` (``DocumentGroup``).

Process flow position: built by ``analysis.pipeline.analyze_document`` and
handed to every evaluator by ``analysis.scheduler``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Optional

from common.documents import (
    Document,
    Page,
    RapidOCREngine,
    TextLayer,
    needs_recognition,
    recognize_text_layer,
    with_text_layers,
)
from common.jobs.limits import ConcurrencyLimit
from common.vision import PageGeometry

from analysis.geometry import page_geometry, working_image_of
from analysis.ocr import get_ocr_engine
from config import OCR_ENGINE, OCR_MIN_NATIVE_CHARS, OCR_WORKERS
# A module, not names: tests replace detector_client.detect_page.
from detector import client as detector_client
from logger import logger

# OCR passes in flight across the whole process (every job's workers share
# this one limit). Each pass is one ONNX inference in a worker thread.
OCR_PASSES = ConcurrencyLimit(OCR_WORKERS, name="ocr-passes")

# Called with (key, settings, layer, engine_name) once per distinct layer.
LayerSink = Callable[[str, dict, TextLayer, Optional[str]], Awaitable[None]]
# Called with (key, payload) once per distinct document-scope joined layer.
JoinedSink = Callable[[str, dict], Awaitable[None]]

# Joined between a document's pages (each stripped of its own leading and
# trailing whitespace) for a document-scope text search. One plain space:
# the last word of one page and the first word of the next stay two words —
# nothing is glued into a new token — and a phrase that runs over the page
# break reads exactly as it would mid-page. A word HYPHENATED across the
# break ("installa-" / "tion") is not rejoined; the matcher sees "installa-
# tion", which `fuzzy` still finds.
PAGE_SEPARATOR = " "


def _engine_name(engine: Any) -> str:
    """"rapidocr" for the bundled engine, the class name for anything else."""
    return "rapidocr" if isinstance(engine, RapidOCREngine) else type(engine).__name__


def ocr_settings(mode: str, min_native_chars: int = OCR_MIN_NATIVE_CHARS) -> dict:
    """Every setting that can change a text layer, resolved."""
    return {
        "mode": mode,
        "min_native_chars": int(min_native_chars),
        "engine": OCR_ENGINE or "none",
    }


def layer_key(settings: dict) -> str:
    """A short, stable, filename-safe name for a settings dict.

    The bare mode when everything else is this server's default — which today
    is always — so the files are ``text.p0.auto.json`` /
    ``text.p0.always.json`` / ``text.p0.never.json``. A non-default extra
    setting adds an 8-hex hash of the whole dict, so two different settings
    can never share a file name.
    """
    defaults = ocr_settings(settings["mode"])
    if settings == defaults:
        return settings["mode"]
    digest = hashlib.sha1(
        json.dumps(settings, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    return f"{settings['mode']}-{digest}"


def item_text_file(item: int, key: str) -> str:
    """``text.p{n}.<key>.json`` — one item's text layer under one setting."""
    return f"text.p{item}.{key}.json"


def document_text_file(document: int, key: str) -> str:
    """``text.d{i}.<key>.json`` — one document's joined (scope: document) text."""
    return f"text.d{document}.{key}.json"


@dataclass
class DocumentContext:
    """The shared, read-only inputs of every unit on one item."""

    doc: Document
    page: Page
    working_image: Any = None
    geometry: Optional[PageGeometry] = None
    layer_sink: Optional[LayerSink] = None
    item: int = 0
    document: int = 0
    detector_stats: detector_client.DetectorStats = field(
        default_factory=detector_client.DetectorStats
    )
    ocr_passes: list[dict] = field(default_factory=list)
    # analysis.references.JobReferences — typed Any so this module does not
    # import the llm layer through it.
    references: Any = None
    _image_b64: Optional[str] = None
    _ask_image: Optional["asyncio.Future[str]"] = None
    _layers: dict[str, "asyncio.Future[TextLayer]"] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        doc: Document,
        *,
        page: Optional[Page] = None,
        item: int = 0,
        document: int = 0,
        layer_sink: Optional[LayerSink] = None,
        detector_stats: Optional[detector_client.DetectorStats] = None,
    ) -> "DocumentContext":
        """Resize once and record the frame for ``page`` (default: the first).

        The frame's ``page`` is the GLOBAL item index, so the shared vision
        code draws this item's regions on ``p{item}``.
        """
        page = page if page is not None else doc.pages[0]
        view = replace(doc, pages=[page], truncated_pages=0)
        working = working_image_of(page)
        geometry = page_geometry(doc, page, working)
        if geometry is not None:
            geometry = replace(geometry, page=item)
        return cls(
            doc=view,
            page=page,
            working_image=working,
            geometry=geometry,
            layer_sink=layer_sink,
            item=item,
            document=document,
            detector_stats=detector_stats or detector_client.DetectorStats(),
        )

    # ── Images ────────────────────────────────────────────────────────────
    @property
    def has_image(self) -> bool:
        return self.working_image is not None

    def image_b64(self) -> Optional[str]:
        """The working image as base64 JPEG, encoded once. None when no image."""
        if self.working_image is None:
            return None
        if self._image_b64 is None:
            from llm.client import encode_image_to_base64

            self._image_b64 = encode_image_to_base64(self.working_image)
        return self._image_b64

    async def ask_image_b64(self) -> Optional[str]:
        """The page as the box loop's ASK call sees it (grid drawn on it when
        LLM_BBOX_GRIDLINES is on), built once per item and shared by every
        located criterion on it. None when no image.

        Drawing the grid and re-encoding is tens of milliseconds of CPU, so it
        runs in a worker thread; concurrent criteria await the same future,
        same memo discipline as ``text_layer``.
        """
        if self.working_image is None:
            return None
        future = self._ask_image
        if future is None:
            from llm import boxes

            future = asyncio.get_running_loop().create_future()
            self._ask_image = future
            try:
                built = await asyncio.to_thread(
                    boxes.ask_image_b64,
                    self.image_b64(),
                    self.working_image,
                    gridlines=boxes.LLM_BBOX_GRIDLINES,
                )
                future.set_result(built)
            except asyncio.CancelledError:
                self._ask_image = None
                future.cancel()
                raise
            except Exception as exc:
                future.set_exception(exc)
                future.exception()
                raise
        return await asyncio.shield(future)

    # ── Text layers ───────────────────────────────────────────────────────
    async def text_layer(
        self, mode: str, min_native_chars: int = OCR_MIN_NATIVE_CHARS
    ) -> TextLayer:
        """This page's text layer under ``mode``, produced at most once per key.

        The first caller for a key starts the pass; every concurrent or later
        caller with the same key awaits the same future. A failure is not
        memoised as a success: the future carries the exception, and every
        unit that asked for that layer fails with it (and only those).
        """
        settings = ocr_settings(mode, min_native_chars)
        key = layer_key(settings)
        future = self._layers.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._layers[key] = future
            try:
                layer, engine_name = await self._produce(settings)
                if self.layer_sink is not None:
                    await self.layer_sink(key, settings, layer, engine_name)
                future.set_result(layer)
            except asyncio.CancelledError:
                # The job is being torn down: nobody gets this layer, and a
                # re-run must not find a cancelled pass in the memo.
                self._layers.pop(key, None)
                future.cancel()
                raise
            except Exception as exc:  # handed to every waiter on this key
                future.set_exception(exc)
                future.exception()  # consumed here, so no "never retrieved" warning
                raise
        # Shielded: a waiter that is cancelled must not cancel the shared pass.
        return await asyncio.shield(future)

    async def _produce(self, settings: dict) -> tuple[TextLayer, Optional[str]]:
        """Recognise (or not) under ``settings``; record what happened."""
        mode, floor = settings["mode"], settings["min_native_chars"]
        wanted = needs_recognition(self.page, mode, floor)
        engine = get_ocr_engine() if wanted else None
        if engine is None:
            layer = TextLayer.of_page(self.page)
            self.ocr_passes.append(
                {
                    "item": self.item,
                    "key": layer_key(settings),
                    "mode": mode,
                    "ran": False,
                    "source": layer.source,
                    "chars": len(layer.text),
                    "confidence": layer.confidence,
                    "note": (
                        f"OCR wanted but unavailable (CLASSIFIER_OCR_ENGINE="
                        f"{OCR_ENGINE or 'none'})" if wanted else None
                    ),
                }
            )
            return layer, None

        async with OCR_PASSES:
            logger.info("text_layer: running OCR (item=%d mode=%s)", self.item, mode)
            layer = await asyncio.to_thread(
                recognize_text_layer,
                self.page,
                engine,
                mode=mode,
                min_native_chars=floor,
            )
        self.ocr_passes.append(
            {
                "item": self.item,
                "key": layer_key(settings),
                "mode": mode,
                "ran": True,
                "source": layer.source,
                "chars": len(layer.text),
                "confidence": layer.confidence,
                "note": None if layer.source == "ocr" else "recognition found no text",
            }
        )
        return layer, (_engine_name(engine) if layer.source == "ocr" else None)

    async def text_document(
        self, mode: str, min_native_chars: int = OCR_MIN_NATIVE_CHARS
    ) -> tuple[Document, TextLayer]:
        """A view of the item carrying ``mode``'s layer, and the layer."""
        layer = await self.text_layer(mode, min_native_chars)
        return with_text_layers(self.doc, [layer]), layer

    def layer_ref(
        self, mode: str, layer: TextLayer, min_native_chars: int = OCR_MIN_NATIVE_CHARS
    ) -> dict:
        """What a criterion result links to: the key, the file, the source, the size."""
        key = layer_key(ocr_settings(mode, min_native_chars))
        return {
            "key": key,
            "name": item_text_file(self.item, key),
            "source": layer.source,
            "chars": len(layer.text),
        }


# ---------------------------------------------------------------------------
# One document's items, and the text a document-scope search reads
# ---------------------------------------------------------------------------


@dataclass
class JoinedText:
    """A document's pages' text layers joined in page order.

    ``segments`` maps the joined string back to the pages: segment
    ``(start, end, item_ctx, offset)`` says that ``joined[start:end]`` is
    ``item_ctx``'s layer text starting at character ``offset`` (the
    whitespace each page was stripped of is what ``offset`` accounts for).
    """

    key: str
    settings: dict
    text: str
    segments: list[tuple[int, int, DocumentContext, int]]
    layers: dict[int, TextLayer]

    def source(self) -> str:
        """"native" / "ocr" / "none", or "mixed" when the pages differ."""
        sources = {layer.source for layer in self.layers.values() if layer.text.strip()}
        if not sources:
            return "none"
        return sources.pop() if len(sources) == 1 else "mixed"

    def payload(self, document: int) -> dict[str, Any]:
        """The ``text.d{i}.<key>.json`` body — the exact string searched."""
        return {
            "key": self.key,
            "scope": "document",
            "document": document,
            "settings": dict(self.settings),
            "separator": PAGE_SEPARATOR,
            "source": self.source(),
            "chars": len(self.text),
            "text": self.text,
            "segments": [
                {
                    "item": ctx.item,
                    "page": ctx.page.index,
                    "start": start,
                    "end": end,
                    "page_offset": offset,
                    "file": item_text_file(ctx.item, self.key),
                }
                for start, end, ctx, offset in self.segments
            ],
        }


@dataclass
class DocumentGroup:
    """One document and its items, in page order."""

    index: int
    doc: Document
    items: list[DocumentContext]
    joined_sink: Optional[JoinedSink] = None
    _joined: dict[str, "asyncio.Future[JoinedText]"] = field(default_factory=dict)

    async def joined_text(
        self, mode: str, min_native_chars: int = OCR_MIN_NATIVE_CHARS
    ) -> JoinedText:
        """Every page's layer under ``mode``, joined with PAGE_SEPARATOR.

        Reads each item's memoised layer (so a page-scope criterion with the
        same setting shares the pass), and is itself memoised per key, so
        the joined file is written once however many criteria search it.
        """
        settings = ocr_settings(mode, min_native_chars)
        key = layer_key(settings)
        future = self._joined.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._joined[key] = future
            try:
                layers = await asyncio.gather(
                    *(item.text_layer(mode, min_native_chars) for item in self.items)
                )
                joined = _join(key, settings, list(zip(self.items, layers)))
                if self.joined_sink is not None:
                    await self.joined_sink(key, joined.payload(self.index))
                future.set_result(joined)
            except asyncio.CancelledError:
                self._joined.pop(key, None)
                future.cancel()
                raise
            except Exception as exc:
                future.set_exception(exc)
                future.exception()
                raise
        return await asyncio.shield(future)


def _join(
    key: str, settings: dict, pairs: list[tuple[DocumentContext, TextLayer]]
) -> JoinedText:
    parts: list[str] = []
    segments: list[tuple[int, int, DocumentContext, int]] = []
    cursor = 0
    for ctx, layer in pairs:
        stripped = layer.text.strip()
        if not stripped:
            continue
        if parts:
            parts.append(PAGE_SEPARATOR)
            cursor += len(PAGE_SEPARATOR)
        offset = len(layer.text) - len(layer.text.lstrip())
        segments.append((cursor, cursor + len(stripped), ctx, offset))
        parts.append(stripped)
        cursor += len(stripped)
    return JoinedText(
        key=key,
        settings=settings,
        text="".join(parts),
        segments=segments,
        layers={ctx.item: layer for ctx, layer in pairs},
    )
