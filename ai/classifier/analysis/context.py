"""The document context: everything criteria share, computed once, read-only.

Every criterion is an independent unit of work — criterion + document in,
one result out — but they all look at the same page. What they share is built
here, once per job, and never written to by an evaluator:

    doc            the loaded ``common.documents.Document`` (single page)
    page           that page
    working_image  the <=MAX_WORKING_DIMENSION copy every detector and the
                   vision model see (None for .txt / .docx)
    geometry       the ``PageGeometry`` mapping working pixels back to the
                   original page (None when there is no image)
    image_b64()    the working image as the base64 JPEG the prompts attach,
                   encoded on first use and cached

The one thing that is NOT computed up front is the text layer, because it
depends on the criterion: ``text_layer(mode)`` returns the layer an OCR
setting gives this page, MEMOISED by the setting. Criteria with identical
settings share one pass; different settings each get their own pass, run
concurrently (bounded by the process-wide CLASSIFIER_OCR_WORKERS). A criterion
that needs no text never asks, so a job with no text / llm criteria never
loads the OCR engine.

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
    text_document() a view of the document carrying that layer, for
                   ``match_text`` and ``pdf_text_regions``.
    layer_ref()    ``{"key", "source", "chars"}`` — what a criterion result
                   links to.
    ocr_passes     what actually ran, for ``document_info.ocr``.

Every layer, native or recognised, is handed to ``layer_sink`` the moment it
is created — the pipeline wires that to the job's artifact directory, so the
exact text a criterion searched is stored as ``text.<key>.json`` once per
distinct setting (see ``regions.artifacts.text_layer_payload``).

Process flow position: built by ``analysis.pipeline.analyze_document`` and
handed to every evaluator by ``analysis.scheduler``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
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
    is always — so the files are ``text.auto.json`` / ``text.always.json`` /
    ``text.never.json``. A non-default extra setting adds an 8-hex hash of
    the whole dict, so two different settings can never share a file name.
    """
    defaults = ocr_settings(settings["mode"])
    if settings == defaults:
        return settings["mode"]
    digest = hashlib.sha1(
        json.dumps(settings, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    return f"{settings['mode']}-{digest}"


@dataclass
class DocumentContext:
    """The shared, read-only inputs of one job's criteria."""

    doc: Document
    page: Page
    working_image: Any = None
    geometry: Optional[PageGeometry] = None
    layer_sink: Optional[LayerSink] = None
    detector_stats: detector_client.DetectorStats = field(
        default_factory=detector_client.DetectorStats
    )
    ocr_passes: list[dict] = field(default_factory=list)
    _image_b64: Optional[str] = None
    _layers: dict[str, "asyncio.Future[TextLayer]"] = field(default_factory=dict)

    @classmethod
    def build(cls, doc: Document, *, layer_sink: Optional[LayerSink] = None) -> "DocumentContext":
        """Resize once and record the frame. The page is ``doc.pages[0]``."""
        page = doc.pages[0]
        working = working_image_of(page)
        return cls(
            doc=doc,
            page=page,
            working_image=working,
            geometry=page_geometry(doc, page, working),
            layer_sink=layer_sink,
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

    # ── Text layers ───────────────────────────────────────────────────────
    async def text_layer(
        self, mode: str, min_native_chars: int = OCR_MIN_NATIVE_CHARS
    ) -> TextLayer:
        """The page's text layer under ``mode``, produced at most once per key.

        The first caller for a key starts the pass; every concurrent or later
        caller with the same key awaits the same future. A failure is not
        memoised as a success: the future carries the exception, and every
        criterion that asked for that layer fails with it (and only those).
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
            logger.info("text_layer: running OCR (mode=%s)", mode)
            layer = await asyncio.to_thread(
                recognize_text_layer,
                self.page,
                engine,
                mode=mode,
                min_native_chars=floor,
            )
        self.ocr_passes.append(
            {
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
        """A view of the document carrying ``mode``'s layer, and the layer."""
        layer = await self.text_layer(mode, min_native_chars)
        return with_text_layers(self.doc, [layer]), layer

    @staticmethod
    def layer_ref(mode: str, layer: TextLayer, min_native_chars: int = OCR_MIN_NATIVE_CHARS) -> dict:
        """What a criterion result links to: the key, the source, the size."""
        return {
            "key": layer_key(ocr_settings(mode, min_native_chars)),
            "source": layer.source,
            "chars": len(layer.text),
        }
