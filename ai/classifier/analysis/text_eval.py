"""The `text` evaluator — deterministic search of one text layer.

No tokens, no image: a ``text`` criterion is answered by
``common.documents.match_text`` against the text layer ITS ``options.ocr``
produces (native, or OCR'd — see ``analysis.context.text_layer``, which
memoises one layer per item and setting and stores each as
``text.p{n}.<key>.json``). The scoring rubric is the interesting part — a
fuzzy near miss scores 1-6 in proportion to how close it got, so "almost
there" is distinguishable from "not there at all".

Two unit shapes, picked by ``options.scope``:

    "page"      (default) one unit per ITEM: this page's layer alone —
                ``evaluate(c, ctx)``, the shared evaluator interface.
    "document"  one unit per DOCUMENT: its pages' layers joined in page order
                with ``context.PAGE_SEPARATOR`` (``DocumentGroup.joined_text``,
                stored as ``text.d{i}.<key>.json``), so a phrase broken over
                a page break still matches — ``evaluate_document(c, group)``.
                Each hit's character offsets are mapped back to the page they
                landed on, and a hit that crosses the break is split into one
                part per page, so every region carries its own item.

    score_text()      — the rubric, from a hit count and a best ratio. Also
                        what the ``sum`` aggregate re-scores summed counts with
                        (``analysis.aggregate``), so the two cannot drift.
    _text_regions()   — where the hits landed, from whichever source has
                        geometry (OCR line polygons, or PyMuPDF on a native
                        PDF page).

Process flow position: one of the four evaluators ``analysis.scheduler``
dispatches to.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from common.documents import (
    Document,
    Page,
    TextHit,
    TextLayer,
    match_text,
    ocr_line_regions,
    page_with_layer,
    pdf_text_regions,
)
from common.vision import Region

from analysis.context import DocumentContext, DocumentGroup, JoinedText, document_text_file
from analysis.outcome import Outcome
from analysis.result_specs import (
    AGGREGATE_FIELD,
    METRIC_FIELD,
    VALUE_FIELD,
    FieldSpec,
    ResultSpec,
    result_spec,
    with_metric,
)
from api.schemas import CriterionInput
from config import FUZZY_CREDIT_FLOOR, TEXT_REGION_MAX_HITS
from logger import logger
from utils import verdict_from_score as _verdict_from_score


# The detail a `text` result carries — the match record — declared here,
# beside `_detail` / `_no_text_detail` and the aggregator's sum (which builds
# the same record), and registered by type (analysis.result_specs). Checked
# against real output by unit-tests/classifier/test_result_specs.py.
TEXT_RESULT = ResultSpec(
    type="text",
    metric="count",
    metric_from="detail",
    fields={
        "metric": METRIC_FIELD,
        "value": VALUE_FIELD,
        "found": FieldSpec("Whether count reached min_count", "boolean"),
        "count": FieldSpec("Hits found (summed across members under `sum`)", "integer"),
        "best_ratio": FieldSpec(
            "Best similarity seen, 0-1 — 1.0 for a literal/regex hit", "number",
        ),
        "mode": FieldSpec("The match mode used", "string", stable=True),
        "pattern": FieldSpec("What was searched for", "string", stable=True),
        "snippets": FieldSpec("Up to 8 context excerpts around the hits", "array"),
        "searched_chars": FieldSpec("Characters searched", "integer"),
        "case_sensitive": FieldSpec("Whether matching was case-sensitive", "boolean", stable=True),
        "min_count": FieldSpec("Hits required to PASS", "integer", stable=True),
        "fuzzy_threshold": FieldSpec(
            "Similarity a fuzzy window must reach; null for other modes", "number", stable=True,
        ),
        "text_source": FieldSpec(
            "native | ocr | none — or `mixed` when summed across members that differ",
            "string",
        ),
        "scope": FieldSpec(
            "`document` when the pages were searched joined", "string",
            stable=True, when="options.scope is document",
        ),
        "separator": FieldSpec(
            "What joined the pages", "string", stable=True, when="options.scope is document",
        ),
        "items_with_hits": FieldSpec(
            "Items the hits landed on", "array", when="options.scope is document",
        ),
        "aggregate": AGGREGATE_FIELD,
    },
    notes=("The full text searched is the `text.<key>.json` artifact the result links to.",),
)


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


def score_text(count: int, best_ratio: float, opts: dict, searched_chars: int) -> tuple[int, str]:
    """``(score, reason)`` for ``count`` hits against ``opts["min_count"]``."""
    if count >= max(1, opts["min_count"]):
        return 10, (
            f"Found {count}x via {opts['match']} match"
            + (f" (best ratio {best_ratio:.2f})" if opts["match"] == "fuzzy" else "")
            + "."
        )
    if opts["match"] == "fuzzy" and best_ratio >= FUZZY_CREDIT_FLOOR:
        # Scale the near miss into 2-6 so "almost there" outranks "absent".
        # Below FUZZY_CREDIT_FLOOR there is no credit at all: difflib gives any
        # unrelated pair of phrases ~0.3-0.5, so anything less is noise.
        span = max(1e-6, opts["fuzzy_threshold"] - FUZZY_CREDIT_FLOOR)
        closeness = min(1.0, (best_ratio - FUZZY_CREDIT_FLOOR) / span)
        score = max(1, min(6, int(round(1 + 5 * closeness))))
        return score, (
            f"Best fuzzy match scored {best_ratio:.2f}, below the "
            f"{opts['fuzzy_threshold']:.2f} threshold."
        )
    return 1, (
        f"'{opts['pattern']}' not found in {searched_chars} characters of document text "
        f"({opts['match']} match, min_count={opts['min_count']})."
    )


def _no_text(c: CriterionInput, opts: dict, source: str, ref: dict) -> Outcome:
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
        detail=_no_text_detail(opts, source),
        text_layer=ref,
    )


def _match(view: Document, c: CriterionInput, opts: dict, locate: bool):
    return match_text(
        view,
        opts["pattern"],
        opts["match"],
        case_sensitive=opts["case_sensitive"],
        fuzzy_threshold=opts["fuzzy_threshold"],
        min_count=opts["min_count"],
        locate=locate,
        label=c.name,
    )


def _detail(res, opts: dict, source: str) -> dict:
    """The match record, in the declared shape (``analysis.result_specs``)."""
    detail = res.as_dict()
    detail.pop("pages", None)  # one page (or one joined document) per unit
    for snippet in detail.get("snippets", []):
        snippet.pop("page", None)
    detail.update(
        case_sensitive=opts["case_sensitive"],
        min_count=opts["min_count"],
        fuzzy_threshold=opts["fuzzy_threshold"] if opts["match"] == "fuzzy" else None,
        text_source=source,
    )
    return with_metric(detail, "text", detail.get("count", 0))


def _no_text_detail(opts: dict, source: str) -> dict:
    """The match record for "there was no text to search" — the same keys as
    any other text result, so a consumer never has to special-case it."""
    return with_metric(
        {
            "found": False,
            "count": 0,
            "best_ratio": 0.0,
            "mode": opts["match"],
            "pattern": opts["pattern"],
            "snippets": [],
            "searched_chars": 0,
            "case_sensitive": opts["case_sensitive"],
            "min_count": opts["min_count"],
            "fuzzy_threshold": opts["fuzzy_threshold"] if opts["match"] == "fuzzy" else None,
            "text_source": source,
        },
        "text",
        0,
    )


@result_spec(TEXT_RESULT)
async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """Score one text criterion against this item's own text layer."""
    opts = c.resolved_options()
    view, layer = await ctx.text_document(opts["ocr"])
    ref = ctx.layer_ref(opts["ocr"], layer)

    if not layer.text.strip():
        return _no_text(c, opts, layer.source, ref)

    locate = ctx.geometry is not None
    res = _match(view, c, opts, locate)
    score, reason = score_text(res.count, res.best_ratio, opts, res.searched_chars)
    detail = _detail(res, opts, layer.source)
    regions = await asyncio.to_thread(_text_regions, view, c.name, opts, res) if locate else []
    logger.info(
        "text_eval: '%s' item=%d pattern=%r match=%s found=%s count=%d score=%d regions=%d",
        c.name, ctx.item, opts["pattern"], opts["match"], res.found, res.count, score,
        len(regions),
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
                       An SVG's ``<text>`` is the same case: MuPDF re-opens
                       it with ``filetype="svg"``.

    .txt / .docx have no geometry and yield nothing — their hits are still
    reported in ``detail.snippets``.
    """
    regions: list[Region] = list(res.regions)
    page = view.pages[0]
    if view.kind in ("pdf", "svg") and page.text_source == "native" and view.source_bytes:
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
                    filetype=view.kind,
                )
            )
    return regions[:TEXT_REGION_MAX_HITS]


# ---------------------------------------------------------------------------
# scope: "document"
# ---------------------------------------------------------------------------


@result_spec(TEXT_RESULT)
async def evaluate_document(c: CriterionInput, group: DocumentGroup) -> Outcome:
    """Score one text criterion against a document's pages joined in order."""
    opts = c.resolved_options()
    joined = await group.joined_text(opts["ocr"])
    ref = {
        "key": joined.key,
        "name": document_text_file(group.index, joined.key),
        "source": joined.source(),
        "chars": len(joined.text),
    }
    if not joined.text.strip():
        return _no_text(c, opts, joined.source(), ref)

    # One synthetic page carrying the joined text: `match_text` searches it
    # exactly as it would a page, and its offsets index `joined.text`.
    flat = Document(
        kind=group.doc.kind,
        filename=group.doc.filename,
        pages=[Page(index=0, text=joined.text, text_source="native")],
    )
    res = _match(flat, c, opts, locate=True)
    score, reason = score_text(res.count, res.best_ratio, opts, res.searched_chars)
    detail = _detail(res, opts, joined.source())
    per_item = _split_hits(joined, res.hits)
    detail["scope"] = "document"
    detail["separator"] = joined.payload(group.index)["separator"]
    detail["items_with_hits"] = sorted(per_item)
    regions = await asyncio.to_thread(_document_regions, group, joined, per_item, c.name, opts)

    confidences = [
        _confidence(layer) for layer in joined.layers.values() if layer.text.strip()
    ]
    logger.info(
        "text_eval: '%s' document=%d (scope document, %d pages) count=%d score=%d regions=%d",
        c.name, group.index, len(group.items), res.count, score, len(regions),
    )
    return Outcome(
        method="text",
        score=score,
        verdict=_verdict_from_score(score),
        # The joined text is only as trustworthy as its least trustworthy page.
        confidence=min(confidences) if confidences else 0,
        reason=reason,
        detail=detail,
        regions=regions,
        text_layer=ref,
    )


def _split_hits(joined: JoinedText, hits: list[TextHit]) -> dict[int, list[TextHit]]:
    """Joined-text hits → per-item hits in each page's OWN layer offsets.

    A hit crossing a page break yields one part per page it covers; the
    separator itself belongs to no page and is dropped.
    """
    out: dict[int, list[TextHit]] = {}
    for hit in hits:
        for start, end, ctx, offset in joined.segments:
            lo, hi = max(hit.start, start), min(hit.end, end)
            if lo >= hi:
                continue
            local_start = lo - start + offset
            local_end = hi - start + offset
            text = joined.text[lo:hi]
            out.setdefault(ctx.item, []).append(
                TextHit(ctx.page.index, local_start, local_end, hit.ratio, text)
            )
    return out


def _document_regions(
    group: DocumentGroup,
    joined: JoinedText,
    per_item: dict[int, list[TextHit]],
    name: str,
    opts: dict,
) -> list[Region]:
    """Each page's share of the hits → regions on THAT page (``page = item``).

    OCR'd pages map through their line polygons; native PDF (and SVG) pages
    through PyMuPDF word-span reconstruction — used for EVERY match mode
    here, since a literal ``search_for(pattern)`` cannot find half a phrase
    whose other half is on the next page. .txt / .docx pages have no
    geometry.
    """
    by_item = {ctx.item: ctx for ctx in group.items}
    regions: list[Region] = []
    for item, hits in sorted(per_item.items()):
        ctx = by_item[item]
        if ctx.geometry is None:
            continue
        layer: Optional[TextLayer] = joined.layers.get(item)
        page = page_with_layer(ctx.page, layer)
        found: list[Region] = []
        if layer is not None and layer.source == "ocr":
            found = ocr_line_regions(page, hits, name, max_regions=TEXT_REGION_MAX_HITS)
        elif (
            ctx.doc.kind in ("pdf", "svg")
            and page.text_source == "native"
            and ctx.doc.source_bytes
        ):
            found = pdf_text_regions(
                ctx.doc.source_bytes,
                page,
                hits,
                pattern=opts["pattern"],
                mode="fuzzy",
                label=name,
                max_regions=TEXT_REGION_MAX_HITS,
                filetype=ctx.doc.kind,
            )
        for region in found:
            region.page = item
        regions.extend(found)
    return regions[:TEXT_REGION_MAX_HITS]
