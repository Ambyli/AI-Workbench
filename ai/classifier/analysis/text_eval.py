"""The `text` evaluator — deterministic search of one text layer.

No tokens, no image: a ``text`` criterion is answered by
``common.documents.match_text`` against the text layer ITS ``options.ocr``
produces (native, or OCR'd — see ``analysis.context.text_layer``, which
memoises one layer per setting and stores each as ``text.<key>.json``). The
scoring rubric is the interesting part — a fuzzy near miss scores 1-6 in
proportion to how close it got, so "almost there" is distinguishable from
"not there at all".

    evaluate()        — the shared evaluator interface.
    _text_regions()   — where the hits landed, from whichever source has
                        geometry (OCR line polygons, or PyMuPDF on a native
                        PDF page).

Process flow position: one of the four evaluators ``analysis.scheduler``
dispatches to.
"""

from __future__ import annotations

import asyncio

from common.documents import Document, TextLayer, match_text, pdf_text_regions
from common.vision import Region

from analysis.context import DocumentContext
from analysis.outcome import Outcome
from api.schemas import CriterionInput
from config import FUZZY_CREDIT_FLOOR, TEXT_REGION_MAX_HITS
from logger import logger
from utils import verdict_from_score as _verdict_from_score


def _confidence(layer: TextLayer) -> int:
    """How much to trust the text that was searched, 0-100.

    Native text is exact (100); OCR'd text is only as trustworthy as the
    recogniser said it was; no text at all is 0.
    """
    if not layer.text.strip():
        return 0
    if layer.source == "ocr":
        return int(round((layer.confidence or 0.0) * 100))
    return 100


async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """Score one text criterion against its own text layer."""
    opts = c.resolved_options()
    view, layer = await ctx.text_document(opts["ocr"])
    ref = ctx.layer_ref(opts["ocr"], layer)
    pattern = opts["pattern"]

    if not layer.text.strip():
        logger.info("text_eval: '%s' — no text layer under ocr=%s", c.name, opts["ocr"])
        return Outcome(
            method="text",
            score=1,
            verdict="FAIL",
            confidence=0,
            reason=(
                f"No text available for this document under ocr={opts['ocr']} (no "
                "native text layer, and OCR did not run or found nothing — "
                "ocr=never, or the OCR engine is disabled/unavailable)."
            ),
            detail={"pattern": pattern, "match": opts["match"], "text_source": layer.source},
            text_layer=ref,
        )

    locate = ctx.geometry is not None
    res = match_text(
        view,
        pattern,
        opts["match"],
        case_sensitive=opts["case_sensitive"],
        fuzzy_threshold=opts["fuzzy_threshold"],
        min_count=opts["min_count"],
        locate=locate,
        label=c.name,
    )

    if res.found:
        score = 10
        reason = (
            f"Found {res.count}x via {opts['match']} match"
            + (f" (best ratio {res.best_ratio:.2f})" if opts["match"] == "fuzzy" else "")
            + "."
        )
    elif opts["match"] == "fuzzy" and res.best_ratio >= FUZZY_CREDIT_FLOOR:
        # Scale the near miss into 2-6 so "almost there" outranks "absent".
        # Below FUZZY_CREDIT_FLOOR there is no credit at all: difflib gives any
        # unrelated pair of phrases ~0.3-0.5, so anything less is noise.
        span = max(1e-6, opts["fuzzy_threshold"] - FUZZY_CREDIT_FLOOR)
        closeness = min(1.0, (res.best_ratio - FUZZY_CREDIT_FLOOR) / span)
        score = max(1, min(6, int(round(1 + 5 * closeness))))
        reason = (
            f"Best fuzzy match scored {res.best_ratio:.2f}, below the "
            f"{opts['fuzzy_threshold']:.2f} threshold."
        )
    else:
        score = 1
        reason = (
            f"'{pattern}' not found in {res.searched_chars} characters of document text "
            f"({opts['match']} match, min_count={opts['min_count']})."
        )

    detail = res.as_dict()
    detail.pop("pages", None)  # single-page documents: always [0] or []
    for snippet in detail.get("snippets", []):
        snippet.pop("page", None)
    detail.update(
        case_sensitive=opts["case_sensitive"],
        min_count=opts["min_count"],
        fuzzy_threshold=opts["fuzzy_threshold"] if opts["match"] == "fuzzy" else None,
        text_source=layer.source,
    )
    regions = await asyncio.to_thread(_text_regions, view, c.name, opts, res) if locate else []
    logger.info(
        "text_eval: '%s' pattern=%r match=%s found=%s count=%d score=%d regions=%d",
        c.name, pattern, opts["match"], res.found, res.count, score, len(regions),
    )
    return Outcome(
        method="text",
        score=score,
        verdict=_verdict_from_score(score),
        confidence=_confidence(layer),
        reason=reason,
        detail=detail,
        regions=regions,
        text_layer=ref,
    )


def _text_regions(view: Document, name: str, opts: dict, res) -> list[Region]:
    """Where the hits landed, from whichever source has geometry.

      OCR'd layer      ``match_text(locate=True)`` already mapped the offsets
                       to line polygons, in page-image pixels.
      Native PDF text  needs the file itself — ``pdf_text_regions`` re-opens
                       ``doc.source_bytes`` (always kept for a PDF here).

    .txt / .docx have no geometry and yield nothing — their hits are still
    reported in ``detail.snippets``.
    """
    regions: list[Region] = list(res.regions)
    page = view.pages[0]
    if view.kind == "pdf" and page.text_source == "native" and view.source_bytes:
        hits = list(res.hits)
        if hits or opts["match"] in ("contains", "exact"):
            regions.extend(
                pdf_text_regions(
                    view.source_bytes,
                    page,
                    hits,
                    pattern=opts["pattern"],
                    mode=opts["match"],
                    label=name,
                    max_regions=TEXT_REGION_MAX_HITS,
                )
            )
    return regions[:TEXT_REGION_MAX_HITS]
