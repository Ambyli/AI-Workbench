"""Slugs, colours, and stroke styles — how a layer stays readable.

Three small policies live here, all of them deterministic so the same
criterion gets the same treatment on every page, in every format, and across
re-renders of the same job:

  ``slugify_criterion``  criterion name → a file- and id-safe handle.
  ``criterion_color``    criterion name → a stable hue.
  ``stroke_for``         region source → a stroke style (dash pattern + width).

Why a hash in the slug: a job can legitimately carry ``has faces`` and
``Has Faces!`` as two different criteria. Lowercasing and collapsing
punctuation makes both ``has-faces``, so the slug carries four hex digits of
the exact name to keep them apart — and to keep a criterion's file name stable
even when another criterion's name changes.

Why colour by criterion and style by source: a reader asks "where is *this
criterion*?" far more often than "where did this come from?", so the louder
channel (hue) carries the criterion and the quieter one (stroke) carries the
provenance.

Process flow position: imported by ``render`` and by any service that needs to
address a criterion's artifacts by slug.
"""

from __future__ import annotations

import colorsys
import hashlib
import re
import unicodedata
from typing import Any, Iterable, Optional

# Slug length cap before the hash suffix. Long enough to stay readable in a
# directory listing, short enough that `p12.<slug>.preview.jpg` fits a path.
MAX_SLUG_BASE = 48

# Hex digits of the name hash appended to every slug. Four gives 65 536
# buckets — ample for the handful of criteria in one job, and short enough to
# stay readable.
SLUG_HASH_CHARS = 4

_NON_SLUG = re.compile(r"[^a-z0-9]+")

# Stroke policy by region source: (dash pattern, stroke width multiplier,
# double-line flag). The dash pattern is an SVG ``stroke-dasharray`` value;
# the PNG renderer approximates the same pattern with drawn segments.
_STROKES: dict[str, dict[str, Any]] = {
    "cv":       {"dash": None,      "width": 1.0, "double": False},
    "detector": {"dash": None,      "width": 1.0, "double": False},
    "pdf-text": {"dash": None,      "width": 1.0, "double": False},
    "ocr":      {"dash": (9, 5),    "width": 1.0, "double": False},
    "llm":      {"dash": (2, 5),    "width": 1.2, "double": False},
    "diff":     {"dash": None,      "width": 1.0, "double": True},
}

_DEFAULT_STROKE = {"dash": None, "width": 1.0, "double": False}


def slugify_criterion(name: str) -> str:
    """Criterion name → ``<ascii-slug>-<4 hex of the exact name>``.

    Rules (§ 4.1 of the regions plan):
      * Unicode is NFKD-folded and stripped of combining marks, so ``Café``
        becomes ``cafe`` rather than vanishing.
      * Everything that is not ``[a-z0-9]`` collapses to a single ``-``.
      * A name that leaves nothing behind (``"日本語"``, ``"***"``) uses ``c``
        as its base — the hash still makes it unique.
      * The hash is of the EXACT original name (bytes, UTF-8), so two names
        that slug identically never collide.

    Args:
        name: The criterion name exactly as the caller wrote it.

    Returns:
        A slug safe for a file name, an SVG id, and a URL query value.
    """
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:SLUG_HASH_CHARS]
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    base = _NON_SLUG.sub("-", ascii_only.lower()).strip("-")[:MAX_SLUG_BASE].strip("-")
    return f"{base or 'c'}-{digest}"


def criterion_color(name: str) -> str:
    """Criterion name → a stable ``#rrggbb`` hue.

    The hue comes from a hash of the name, so the same criterion is the same
    colour on every page and in every format without any palette bookkeeping.
    Saturation and value are fixed high enough to read over both a white
    document and a dark photo; the golden-ratio offset spreads nearby hashes
    apart so two criteria rarely land in the same part of the wheel.
    """
    h = int(hashlib.sha1(name.encode("utf-8")).hexdigest()[:8], 16)
    hue = ((h % 3600) / 3600.0 + 0.381966) % 1.0  # golden-ratio conjugate
    r, g, b = colorsys.hsv_to_rgb(hue, 0.78, 0.94)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def stroke_for(source: str) -> dict[str, Any]:
    """Region source → its stroke policy.

    Returns a dict with ``dash`` (an ``(on, off)`` pair or None), ``width``
    (a multiplier on the renderer's base stroke width) and ``double`` (draw a
    second, inset outline). Unknown sources fall back to a plain solid line
    rather than raising — a new source should render, not crash a layer.
    """
    return dict(_STROKES.get(source, _DEFAULT_STROKE))


def dasharray(source: str, base_width: float) -> Optional[str]:
    """SVG ``stroke-dasharray`` for ``source``, scaled to ``base_width``.

    Scaling matters: a 9/5 dash on a 3000-px page drawn with a 4-px stroke is
    invisible at any sane zoom, so the pattern grows with the stroke.
    """
    stroke = stroke_for(source)
    dash = stroke["dash"]
    if not dash:
        return None
    scale = max(1.0, base_width)
    return " ".join(str(round(v * scale, 2)) for v in dash)


def legend_entries(regions: Iterable[Any]) -> list[dict[str, Any]]:
    """One legend row per (criterion, source) pair present in ``regions``.

    Rows are sorted by criterion name then source so the legend order is
    stable between renders; each carries the colour and stroke the renderer
    will use plus a count, which doubles as a quick "did anything land here?"
    summary for a caller that does not want to parse the whole region list.

    Args:
        regions: Any iterable of ``Region`` objects.

    Returns:
        ``[{"label", "slug", "color", "source", "dash", "double", "count"}]``
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for region in regions:
        key = (region.label, region.source)
        entry = buckets.get(key)
        if entry is None:
            stroke = stroke_for(region.source)
            buckets[key] = {
                "label": region.label,
                "slug": slugify_criterion(region.label),
                "color": criterion_color(region.label),
                "source": region.source,
                "dash": stroke["dash"],
                "double": stroke["double"],
                "count": 1,
            }
        else:
            entry["count"] += 1
    return [buckets[k] for k in sorted(buckets)]
