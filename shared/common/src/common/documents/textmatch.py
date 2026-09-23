"""Text matching over a loaded ``Document``.

``match_text(document, pattern, mode=...)`` searches every page's text layer
and reports where the pattern hit, how many times, and how close the best
near-miss was. Four modes, each answering a different question:

    contains  substring anywhere            "does the words 'Net 30' appear?"
    exact     whole word / whole line       "is there a field exactly 'TOTAL'?"
    regex     caller-supplied expression    "is there a CASE-\\d{5} number?"
    fuzzy     best sliding-window ratio     "does OCR'd text roughly say
                                             'Notice to Owner'?"

Fuzzy exists because OCR output is not exact: "Notice to Owner" comes back as
"Notlce to 0wner" often enough that a substring test on a scanned page is a
coin flip. A sliding window of the same word count as the pattern, scored with
``difflib.SequenceMatcher``, degrades gracefully instead.

Safety: ``regex`` patterns are caller-supplied, so they are length-capped
(``MAX_PATTERN_CHARS``) and compiled inside a try/except; match iteration is
capped at ``MAX_MATCHES_SCANNED``. Python's ``re`` has no execution timeout,
so bounding input length and match count is the available guard — keep it.

Process flow position: called after loading (and optionally OCR-ing) a
document; the classifier maps the result onto a criterion score.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal, Sequence

from ..vision.model import Region
from .model import Document, Page

MatchMode = Literal["contains", "exact", "regex", "fuzzy"]

# Longest accepted pattern. Catastrophic backtracking needs a long pattern to
# be interesting; 500 characters is far beyond any legitimate search string.
MAX_PATTERN_CHARS = 500

# Upper bound on matches collected per page, so a pattern like ``.?`` on a
# 60k-character page cannot produce a million-entry result.
MAX_MATCHES_SCANNED = 1000

# Characters of context kept either side of a hit in a snippet.
SNIPPET_CONTEXT = 40

# Snippets are for human/LLM consumption, not an exhaustive index.
MAX_SNIPPETS = 8


class InvalidPatternError(ValueError):
    """Raised for a pattern that is too long or not a valid regex."""


@dataclass
class TextHit:
    """One match, as character offsets into ``document.page(page).text``.

    Offsets rather than geometry, because where a hit *is* depends on how the
    page's text came to exist: an OCR'd page maps offsets to line polygons,
    a native PDF page maps them to PyMuPDF rectangles, and a .txt has no
    geometry at all. ``match_text(..., locate=True)`` collects the offsets;
    the geometry lookups live with the thing that owns the coordinates.

    Attributes:
        page:  Zero-based page index.
        start: Inclusive character offset of the match.
        end:   Exclusive character offset of the match.
        ratio: 1.0 for a literal/regex hit, the window similarity for fuzzy.
        text:  The matched substring itself.
    """

    page: int
    start: int
    end: int
    ratio: float = 1.0
    text: str = ""

    def as_dict(self) -> dict:
        """JSON-safe view."""
        return {
            "page": self.page,
            "start": self.start,
            "end": self.end,
            "ratio": round(self.ratio, 4),
            "text": self.text,
        }


@dataclass
class TextMatchResult:
    """Outcome of one ``match_text`` call.

    Attributes:
        found:       True when ``count >= min_count``.
        count:       Total hits across all pages (fuzzy: windows at or above
                     the threshold).
        best_ratio:  1.0 for a literal/regex hit, 0.0 for a literal/regex
                     miss; for fuzzy, the best window similarity seen (0-1).
        mode:        The mode that was used.
        pattern:     The pattern that was searched for.
        pages:       Zero-based indices of pages with at least one hit.
        snippets:    Up to ``MAX_SNIPPETS`` context excerpts —
                     ``{"page": int, "text": str, "ratio": float}``.
        searched_chars: Total characters searched, for diagnostics.
        hits:        Every match as character offsets, but ONLY when the call
                     passed ``locate=True`` (collecting them otherwise is
                     wasted work on a 60k-character contract).
        regions:     Page polygons for the hits that landed on an OCR'd page,
                     also only under ``locate=True``. Native PDF geometry is
                     not here — it needs the PDF bytes, so it comes from
                     ``loaders.pdf_text_regions``.
    """

    found: bool = False
    count: int = 0
    best_ratio: float = 0.0
    mode: MatchMode = "contains"
    pattern: str = ""
    pages: list[int] = field(default_factory=list)
    snippets: list[dict] = field(default_factory=list)
    searched_chars: int = 0
    hits: list[TextHit] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)

    def as_dict(self) -> dict:
        """JSON-safe view, used verbatim in API responses.

        ``hits`` and ``regions`` are deliberately NOT included: this dict goes
        into a criterion's ``detail`` block, and geometry belongs in
        ``regions.json`` and the criterion's own ``regions`` list, not
        duplicated inside a diagnostic."""
        return {
            "found": self.found,
            "count": self.count,
            "best_ratio": round(self.best_ratio, 4),
            "mode": self.mode,
            "pattern": self.pattern,
            "pages": list(self.pages),
            "snippets": list(self.snippets),
            "searched_chars": self.searched_chars,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snippet(text: str, start: int, end: int) -> str:
    """Return ~80 characters of context around ``text[start:end]``.

    Newlines are collapsed so a snippet stays one line in logs and JSON, and
    an ellipsis marks each truncated side.
    """
    lo = max(0, start - SNIPPET_CONTEXT)
    hi = min(len(text), end + SNIPPET_CONTEXT)
    excerpt = " ".join(text[lo:hi].split())
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"


def _compile(pattern: str, mode: MatchMode, case_sensitive: bool) -> re.Pattern:
    """Compile the pattern for the literal-ish modes (contains/exact/regex).

    ``contains`` and ``exact`` escape the pattern so regex metacharacters in a
    plain search string ("Total Due: $4,850.00") are literal; ``exact`` adds
    word boundaries that also satisfy the whole-line case, since a line break
    is a non-word character.

    Raises:
        InvalidPatternError: Pattern too long, or an invalid regex in
            ``regex`` mode.
    """
    if len(pattern) > MAX_PATTERN_CHARS:
        raise InvalidPatternError(
            f"Pattern is {len(pattern)} characters; the maximum is "
            f"{MAX_PATTERN_CHARS}. Long patterns are rejected because "
            "Python's re engine has no execution timeout."
        )
    flags = 0 if case_sensitive else re.IGNORECASE
    if mode == "regex":
        try:
            return re.compile(pattern, flags)
        except re.error as exc:
            raise InvalidPatternError(f"Invalid regular expression: {exc}") from exc
    if mode == "exact":
        # (?<!\w) / (?!\w) rather than \b so patterns that start or end with a
        # non-word character ("$4,850.00") still anchor correctly.
        return re.compile(rf"(?<!\w){re.escape(pattern)}(?!\w)", flags)
    return re.compile(re.escape(pattern), flags)


def _fuzzy_page(
    page_text: str, pattern: str, threshold: float, case_sensitive: bool
) -> tuple[int, float, list[tuple[int, int, float]]]:
    """Slide a word window across ``page_text`` scoring similarity to ``pattern``.

    The window is the pattern's word count (and the same ±1, so a stray OCR
    split or merge still lines up). A window at or above ``threshold`` counts
    as a hit and the scan skips past it, so one phrase is never counted twice.

    Returns:
        ``(hit_count, best_ratio, hits)`` where each hit is
        ``(char_start, char_end, ratio)``.
    """
    needle = pattern if case_sensitive else pattern.lower()
    haystack = page_text if case_sensitive else page_text.lower()

    # Token positions so a hit maps back to character offsets for snippets.
    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", haystack)]
    if not tokens:
        return 0, 0.0, []

    pattern_words = max(1, len(needle.split()))
    window_sizes = sorted({max(1, pattern_words - 1), pattern_words, pattern_words + 1})

    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(needle)  # seq2 is the one SequenceMatcher caches

    best = 0.0
    hits: list[tuple[int, int, float]] = []
    i = 0
    while i < len(tokens):
        window_best = 0.0
        window_span = None
        for size in window_sizes:
            j = min(i + size, len(tokens))
            start, end = tokens[i][0], tokens[j - 1][1]
            candidate = haystack[start:end]
            # Cheap length guard: wildly different lengths cannot pass a high
            # threshold, and skipping them keeps the scan linear-ish.
            if abs(len(candidate) - len(needle)) > max(len(needle), 8):
                continue
            matcher.set_seq1(candidate)
            ratio = matcher.ratio()
            if ratio > window_best:
                window_best, window_span = ratio, (start, end)
        if window_best > best:
            best = window_best
        if window_best >= threshold and window_span is not None:
            hits.append((window_span[0], window_span[1], round(window_best, 4)))
            i += pattern_words  # skip the matched phrase
        else:
            i += 1
        if len(hits) >= MAX_MATCHES_SCANNED:
            break
    return len(hits), best, hits


# ---------------------------------------------------------------------------
# Locating a hit on the page
# ---------------------------------------------------------------------------


def _line_spans(page: Page) -> list[tuple[int, int, dict]]:
    """Character span of every OCR line inside ``page.text``.

    ``apply_ocr`` sets ``page.text`` to ``"\\n".join(line["text"] …)``, so the
    spans are just cumulative lengths plus one character per newline. That
    exact correspondence is the whole trick: it turns a character offset from
    the matcher into the line — and therefore the polygon — it came from.

    Returns:
        ``[(start, end, line_dict), ...]`` in reading order.
    """
    spans: list[tuple[int, int, dict]] = []
    cursor = 0
    for line in page.ocr_lines:
        text = str(line.get("text", ""))
        spans.append((cursor, cursor + len(text), line))
        cursor += len(text) + 1  # + the newline join() inserted
    return spans


def ocr_line_regions(
    page: Page, hits: Sequence[TextHit], label: str, *, max_regions: int = 200
) -> list[Region]:
    """Map hits on an OCR'd page back to the line polygons they came from.

    A hit that straddles a line break (OCR splits "Notice to / Owner" across
    two detected lines more often than not) produces one region per line it
    overlaps, which is the honest answer — there is no single quadrilateral
    covering both.

    Args:
        page:        The page the offsets belong to; needs ``ocr_lines``.
        hits:        Hits from ``match_text(..., locate=True)`` for this page.
        label:       Region label — the criterion name, normally.
        max_regions: Cap, so a pattern matching every line of a dense scan
                     cannot produce thousands of polygons.

    Returns:
        ``polygon`` regions with ``source="ocr"`` and the line's recognition
        confidence as ``score``, in page-image pixels.
    """
    if not page.ocr_lines:
        return []
    spans = _line_spans(page)
    regions: list[Region] = []
    seen: set[tuple[int, int]] = set()
    for hit in hits:
        for index, (start, end, line) in enumerate(spans):
            if end <= hit.start or start >= hit.end:
                continue  # no overlap
            box = line.get("box")
            if not box or len(box) < 3:
                continue
            key = (hit.start, index)
            if key in seen:
                continue
            seen.add(key)
            regions.append(
                Region(
                    page=page.index,
                    kind="polygon",
                    points=[(float(p[0]), float(p[1])) for p in box],
                    label=label,
                    score=line.get("confidence"),
                    source="ocr",
                    attrs={
                        "text": str(line.get("text", ""))[:120],
                        "line": index,
                        "ratio": round(hit.ratio, 4),
                    },
                )
            )
            if len(regions) >= max_regions:
                return regions
    return regions


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def match_text(
    document: Document,
    pattern: str,
    mode: MatchMode = "contains",
    *,
    case_sensitive: bool = False,
    fuzzy_threshold: float = 0.85,
    min_count: int = 1,
    locate: bool = False,
    label: str | None = None,
) -> TextMatchResult:
    """Search every page of ``document`` for ``pattern``.

    Args:
        document:        A loaded (and optionally OCR'd) document.
        pattern:         What to look for. Literal text for contains/exact/
                         fuzzy; a Python regular expression for regex.
        mode:            contains | exact | regex | fuzzy.
        case_sensitive:  Case-fold both sides when False (the default).
        fuzzy_threshold: Similarity 0-1 a window must reach to count as a hit
                         in fuzzy mode. Ignored by the other modes.
        min_count:       How many hits are needed for ``found`` to be True.
        locate:          Also record every hit's character offsets in
                         ``result.hits`` and, for OCR'd pages, its line
                         polygons in ``result.regions``. Off by default: a
                         scoring-only caller should not pay for geometry it
                         will not read.
        label:           Region label when ``locate`` is set; defaults to
                         ``pattern``.

    Returns:
        A ``TextMatchResult``. A document with no text layer at all yields
        ``found=False, count=0`` rather than an error — the caller decides
        whether that is a failure or a "cannot evaluate".

    Raises:
        InvalidPatternError: Pattern too long, or an invalid regex.
    """
    result = TextMatchResult(mode=mode, pattern=pattern)
    if not pattern:
        raise InvalidPatternError("Pattern must not be empty.")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise InvalidPatternError(
            f"Pattern is {len(pattern)} characters; the maximum is {MAX_PATTERN_CHARS}."
        )

    compiled = None if mode == "fuzzy" else _compile(pattern, mode, case_sensitive)
    threshold = min(1.0, max(0.0, fuzzy_threshold))

    region_label = label or pattern

    for page in document.pages:
        text = page.text
        if not text:
            continue
        result.searched_chars += len(text)
        page_hits_located: list[TextHit] = []

        if mode == "fuzzy":
            count, best, hits = _fuzzy_page(text, pattern, threshold, case_sensitive)
            if best > result.best_ratio:
                result.best_ratio = best
            if count:
                result.count += count
                result.pages.append(page.index)
                for start, end, ratio in hits[:MAX_SNIPPETS]:
                    if len(result.snippets) < MAX_SNIPPETS:
                        result.snippets.append(
                            {
                                "page": page.index,
                                "text": _snippet(text, start, end),
                                "ratio": ratio,
                            }
                        )
                if locate:
                    page_hits_located = [
                        TextHit(page.index, s, e, r, text[s:e]) for s, e, r in hits
                    ]
        else:
            page_hits = 0
            for match in compiled.finditer(text):
                page_hits += 1
                if len(result.snippets) < MAX_SNIPPETS:
                    result.snippets.append(
                        {
                            "page": page.index,
                            "text": _snippet(text, match.start(), match.end()),
                            "ratio": 1.0,
                        }
                    )
                if locate:
                    page_hits_located.append(
                        TextHit(page.index, match.start(), match.end(), 1.0, match.group(0))
                    )
                if page_hits >= MAX_MATCHES_SCANNED:
                    break
            if page_hits:
                result.count += page_hits
                result.pages.append(page.index)
                result.best_ratio = 1.0

        # Geometry, only when asked for. OCR'd pages resolve here; native PDF
        # pages need the file itself, so their rectangles are added later by
        # ``loaders.pdf_text_regions`` from these same offsets.
        if locate and page_hits_located:
            result.hits.extend(page_hits_located)
            if page.text_source == "ocr":
                result.regions.extend(
                    ocr_line_regions(page, page_hits_located, region_label)
                )

    result.found = result.count >= max(1, min_count)
    return result
