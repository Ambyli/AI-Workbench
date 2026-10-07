"""SVG recognition and external-reference handling — stdlib only.

An SVG is UTF-8 XML, so without this module ``detect_kind`` falls through to
its plain-text check and calls it ``txt``: a ``text`` criterion then searches
the markup, and a vision model is handed angle brackets instead of a picture.
Three small tools fix that and let a caller reason about what the file points
at:

    looks_like_svg(raw)       → True when the first element of the document
                                is ``<svg>`` (or ``<prefix:svg>``). Called by
                                ``detect.detect_kind`` BEFORE the txt fallback.
    find_external_refs(raw)   → every ``href`` / ``xlink:href`` on an
                                ``<image>``, ``<feImage>`` or ``<use>`` start
                                tag that points OUTSIDE the file (not empty,
                                not a ``#fragment``, not a ``data:`` URI), with
                                its byte span.
    replace_refs(raw, {ref: value})
                              → the same bytes with those attribute values
                                rewritten — how a caller inlines a fetched
                                image as a ``data:`` URI, or blanks a link it
                                refused.

Why a regex and not an XML parser — deliberate, and worth keeping:

  * stdlib expat EXPANDS internal entities (``<!ENTITY x "...">``), so a
    parser would report hrefs that only exist after expansion — and the
    billion-laughs shape is a denial of service on the parse itself.
  * Re-serialising a parsed tree changes the bytes (attribute order, quoting,
    namespace prefixes, whitespace), so a rewrite would no longer be the file
    the caller sent with two values changed.
  * MuPDF — the renderer this package uses — does not expand entities at all.
    A regex over the raw bytes sees what MuPDF sees, which is the point: the
    references that matter are the ones the renderer would act on.

What the renderer does with them (verified against PyMuPDF 1.28.2 and pinned
by ``tests/test_documents_svg.py``): MuPDF fetches NOTHING. http(s),
``file://``, absolute and relative paths, an external ``<use>``, CSS
``@import`` / ``url()`` / ``@font-face``, ``<foreignObject>``, an external DTD
and XXE entities are all ignored; only ``data:`` images are drawn. So these
helpers exist to REPORT what was not drawn (``loaders._load_svg`` turns each
reference into a ``Document.warnings`` line) and to let a service that has
decided to fetch — through ``common.net.fetch_url``, never through the
renderer — inline the result.

Process flow position: ``detect.detect_kind`` calls ``looks_like_svg``;
``loaders._load_svg`` calls ``find_external_refs``; services call
``find_external_refs`` + ``replace_refs`` at submit when they fetch images.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Mapping

# How much of the file the root-element check reads. An XML declaration, a
# licence comment and a DOCTYPE with a short internal subset fit comfortably;
# a file that has not reached its first element by then is not worth calling
# an SVG.
_SNIFF_WINDOW = 4096

_UTF8_BOM = b"\xef\xbb\xbf"

# The first element's name: ``svg`` or ``anything:svg``, followed by
# whitespace, ``/`` or ``>`` so ``<svgfoo>`` does not count.
_ROOT_SVG = re.compile(r"<(?:[A-Za-z_][\w.\-]*:)?svg(?=[\s/>])")

# Every pattern below runs over the WHOLE upload, which is caller-supplied,
# so each is written to stay linear on hostile input — the shapes that look
# equivalent are not. ``tests/test_documents_svg.py`` times them on
# adversarial files; keep that test green when touching these.
#
#   * A match that can fail must stop at the next ``<``. Otherwise a file of
#     unterminated ``<image `` tags makes every attempt read to the end of
#     the file: quadratic, ~90 s for 140 KB. ``<`` is never legal inside an
#     attribute value or between attributes, so stopping there loses nothing.
#   * A mask, once started, always succeeds (it ends at its terminator, at
#     the next ``<`` or at the end of the file), so it never fails back into
#     a rescan.
#   * No ``.*?`` between brackets that can itself contain brackets: the old
#     DOCTYPE mask ``\[.*?\]`` split ``[][][]…`` exponentially many ways.

# Regions masked (replaced by spaces, same length) before scanning for
# references, so an ``<image href>`` inside a comment, a CDATA section or a
# DOCTYPE internal subset — none of which MuPDF draws — is never reported.
# An unterminated comment / CDATA / internal subset runs to the end of the
# file, as it would for a parser; a DOCTYPE with no ``>`` ends at the next
# ``<``.
_MASKS = (
    re.compile(rb"<!--.*?(?:-->|\Z)", re.DOTALL),
    re.compile(rb"<!\[CDATA\[.*?(?:\]\]>|\Z)", re.DOTALL),
    re.compile(
        rb"<!DOCTYPE(?:[^\[<>]|\[[^\]]*(?:\]|\Z))*(?:>|(?=<)|\Z)",
        re.IGNORECASE,
    ),
)

# A start tag of one of the referencing elements. Attribute values may
# contain ``>``, so the attribute run is "anything but a quote, ``<`` or
# ``>``, or a whole quoted string with no ``<`` in it". Element names are
# case-sensitive in XML (``feImage``).
_REF_TAG = re.compile(
    rb"<(?:[A-Za-z_][\w.\-]*:)?(image|feImage|use)(?=[\s/>])"
    rb"((?:[^<>\"']|\"[^\"<]*\"|'[^'<]*')*)>"
)

# One ``name="value"`` / ``name='value'`` attribute inside that run. The run
# is walked attribute by attribute (each match consumes its whole quoted
# value), so ``title=' href="x"'`` is one ``title``, never an ``href``. The
# lookbehind starts a name only at the start of the run or after whitespace
# or a quote — without it, a long run with no ``=`` is retried from every
# one of its characters (quadratic).
_ATTR = re.compile(rb"(?<![^\s\"'])([^\s=/>\"']+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")

# The five predefined XML entities — the only ones unescaped in a value.
# ``&amp;`` last so ``&amp;lt;`` reads as the literal text ``&lt;``.
_XML_UNESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&"))


@dataclass(frozen=True)
class ExternalRef:
    """One attribute value in the file that points outside it.

    Frozen (hashable) so a caller can key a ``replace_refs`` mapping by it.

    Attributes:
        tag:   ``image`` | ``feImage`` | ``use`` — the element carrying it.
        attr:  The attribute name as written (``href`` or ``xlink:href``).
        url:   The value with the five predefined XML entities unescaped and
               surrounding whitespace stripped — what a fetch would request.
        start: Byte offset of the first character of the value (just inside
               the opening quote) in the ORIGINAL bytes.
        end:   Byte offset just past the last character of the value (the
               closing quote's position).
    """

    tag: str
    attr: str
    url: str
    start: int
    end: int


def _strip_prolog(text: str) -> str:
    """``text`` with any leading XML declaration, processing instructions,
    comments, DOCTYPE and whitespace removed — or "" when the window ends
    inside one of them."""
    while True:
        text = text.lstrip()
        if text.startswith("<?"):
            end = text.find("?>")
            if end < 0:
                return ""
            text = text[end + 2:]
        elif text.startswith("<!--"):
            end = text.find("-->")
            if end < 0:
                return ""
            text = text[end + 3:]
        elif text[:9].upper() == "<!DOCTYPE":
            # An internal subset ``[...]`` may itself contain ``>`` (entity
            # and element declarations), so when a ``[`` comes before the
            # first ``>``, the declaration ends at the ``>`` after its ``]``.
            bracket = text.find("[")
            close = text.find(">")
            if close < 0:
                return ""
            if 0 <= bracket < close:
                subset_end = text.find("]", bracket)
                if subset_end < 0:
                    return ""
                close = text.find(">", subset_end)
                if close < 0:
                    return ""
            text = text[close + 1:]
        else:
            return text


def looks_like_svg(raw: bytes) -> bool:
    """True when ``raw`` is an SVG document: UTF-8 whose root element is svg.

    Rules, in order:
      * no NUL byte anywhere (UTF-16 XML, or a binary blob, is not accepted);
      * the first ``_SNIFF_WINDOW`` bytes decode as UTF-8 (a BOM is allowed,
        and a multi-byte character cut by the window edge is tolerated);
      * after skipping an optional XML declaration, processing instructions,
        comments, a DOCTYPE (internal subset included) and whitespace, the
        first element is ``svg`` or ``<prefix>:svg``.

    A .txt that merely mentions ``<svg`` mid-text, and an HTML page with an
    inline ``<svg>``, both fail the last rule and stay ``txt``.
    """
    if not raw or b"\x00" in raw:
        return False
    head = raw[:_SNIFF_WINDOW]
    if head.startswith(_UTF8_BOM):
        head = head[len(_UTF8_BOM):]
    try:
        # Incremental, not final: a character split by the window edge is a
        # pending partial sequence, not a decode error.
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return _ROOT_SVG.match(_strip_prolog(text)) is not None


def _masked(raw: bytes) -> bytes:
    """``raw`` with comments, CDATA and the DOCTYPE blanked to spaces —
    same length, so every offset still points into the original bytes."""
    out = bytearray(raw)
    for pattern in _MASKS:
        for m in pattern.finditer(raw):
            out[m.start():m.end()] = b" " * (m.end() - m.start())
    return bytes(out)


def _unescape(value: str) -> str:
    for entity, char in _XML_UNESCAPES:
        value = value.replace(entity, char)
    return value


def find_external_refs(raw: bytes) -> list[ExternalRef]:
    """Every external ``href`` on an ``<image>``, ``<feImage>`` or ``<use>``.

    Skipped (they draw from inside the file, or not at all): an empty value,
    a ``#fragment`` (a reference to an element of this document), and a
    ``data:`` URI. References inside comments, CDATA sections and the DOCTYPE
    are never reported — see ``_MASKS``.

    Args:
        raw: The SVG bytes, exactly as uploaded.

    Returns:
        References in document order. Their ``start`` / ``end`` index ``raw``
        itself, so they can be handed straight to ``replace_refs``.
    """
    scan = _masked(raw)
    refs: list[ExternalRef] = []
    for tag_match in _REF_TAG.finditer(scan):
        tag = tag_match.group(1).decode("ascii")
        attrs_start = tag_match.start(2)
        for attr_match in _ATTR.finditer(tag_match.group(2)):
            name = attr_match.group(1)
            # ``href`` (SVG 2) or ``xlink:href`` — any prefix, since the
            # prefix bound to the XLink namespace is the author's choice.
            if name != b"href" and not name.endswith(b":href"):
                continue
            group = 2 if attr_match.group(2) is not None else 3
            start = attrs_start + attr_match.start(group)
            end = attrs_start + attr_match.end(group)
            url = _unescape(raw[start:end].decode("utf-8", errors="replace")).strip()
            if not url or url.startswith("#") or url[:5].lower() == "data:":
                continue
            refs.append(ExternalRef(
                tag=tag,
                attr=name.decode("utf-8", errors="replace"),
                url=url,
                start=start,
                end=end,
            ))
    return refs


def _escape_attr(value: str) -> bytes:
    """``value`` made safe inside a quoted attribute of either quoting."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
        .encode("utf-8")
    )


def replace_refs(raw: bytes, replacements: Mapping[ExternalRef, str]) -> bytes:
    """``raw`` with each reference's attribute value replaced.

    Rewrites by byte span, last to first, so earlier spans stay valid while
    later ones change length. Nothing else in the file is touched — not the
    quoting, not the attribute order, not a byte of the surrounding markup.

    Args:
        raw:          The bytes the references were found in.
        replacements: ``{ExternalRef: new_value}``. The value is plain text
                      (``data:image/png;base64,...`` or ``""``); ``&``, ``<``,
                      ``"`` and ``'`` are escaped on the way in.

    Returns:
        The rewritten bytes (``raw`` itself when there is nothing to do).

    Raises:
        ValueError: Two references overlap — they cannot both be from one
            ``find_external_refs`` call on ``raw``.
    """
    if not replacements:
        return raw
    out = bytearray(raw)
    previous_start = len(raw) + 1
    for ref in sorted(replacements, key=lambda r: r.start, reverse=True):
        if ref.end > previous_start:
            raise ValueError("overlapping SVG references cannot be replaced together")
        out[ref.start:ref.end] = _escape_attr(replacements[ref])
        previous_start = ref.start
    return bytes(out)
