"""Implementation of :mod:`common.textops` — see the package docstring."""

from __future__ import annotations

import codecs
import difflib
import re
import time
from dataclasses import dataclass
from typing import Optional


# A numbered-line prefix as ``number_lines`` writes it: optional padding,
# the line number, one TAB. Callers sometimes paste it into ``old_string``;
# ``apply_edit`` says so instead of reporting a bare "not found".
LINE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\t")

# A not-found ``old_string`` only gets a "closest region" when the best
# window scores at least this (difflib ratio over whitespace-stripped lines).
CLOSEST_REGION_MIN_RATIO = 0.3


class FileOpError(Exception):
    """A file operation was refused; nothing was written.

    ``kind`` is the machine-readable reason (``not_found``,
    ``multiple_matches``, ``missing_file``, ...); ``extra`` carries the
    kind-specific fields a caller can put in its error response. Raised by
    :func:`apply_edit`, and usable by any caller's own file I/O for the same
    shape."""

    def __init__(self, kind: str, message: str, **extra) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.extra = extra


# ── UTF-8 boundaries ──────────────────────────────────────────────────────


def looks_binary(data: bytes) -> bool:
    """A NUL byte means "not text"."""
    return b"\x00" in data


def decode_utf8_prefix(data: bytes) -> str:
    """Decode bytes that may stop in the middle of a character (a
    byte-capped read). An incomplete trailing sequence is dropped; invalid
    bytes anywhere else still raise ``UnicodeDecodeError``."""
    return codecs.getincrementaldecoder("utf-8")().decode(data, final=False)


def utf8_tail(data: bytes, max_bytes: int) -> bytes:
    """The last ``max_bytes`` of ``data``, moved forward past any UTF-8
    continuation bytes so the result starts on a character boundary."""
    if max_bytes <= 0:
        return b""
    if len(data) <= max_bytes:
        return data
    tail = data[len(data) - max_bytes:]
    skip = 0
    while skip < min(3, len(tail)) and (tail[skip] & 0xC0) == 0x80:
        skip += 1
    return tail[skip:]


# ── Lines ─────────────────────────────────────────────────────────────────


def split_lines(text: str) -> list[str]:
    """Lines as :func:`number_lines` numbers them: split on ``\\n``, no
    phantom empty line after a final newline, a trailing ``\\r`` (CRLF
    file) dropped."""
    if not text:
        return []
    parts = text.split("\n")
    if text.endswith("\n"):
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def number_lines(
    lines: list[str],
    start: int,
    *,
    max_line_chars: Optional[int] = None,
    width: Optional[int] = None,
) -> tuple[str, int]:
    """``cat -n`` style: right-aligned line number, TAB, line. The number
    is padded to the widest number in the window (or ``width``). Returns
    ``(text, lines_cut)`` — lines longer than ``max_line_chars`` are cut
    with a visible marker and counted."""
    if not lines:
        return "", 0
    width = width or len(str(start + len(lines) - 1))
    out: list[str] = []
    cut = 0
    for i, line in enumerate(lines):
        if max_line_chars is not None and len(line) > max_line_chars:
            line = (
                f"{line[:max_line_chars]}… [line cut at {max_line_chars} "
                f"of {len(line)} chars]"
            )
            cut += 1
        out.append(f"{start + i:>{width}}\t{line}")
    return "\n".join(out), cut


def has_line_number_prefixes(text: str) -> bool:
    """True when every non-blank line starts with a numbered-line prefix."""
    lines = [line for line in text.split("\n") if line.strip()]
    return bool(lines) and all(LINE_NUMBER_PREFIX_RE.match(line) for line in lines)


def strip_line_number_prefixes(text: str) -> str:
    return "\n".join(
        LINE_NUMBER_PREFIX_RE.sub("", line, count=1) for line in text.split("\n")
    )


def line_of_offset(text: str, offset: int) -> int:
    """1-based line number of the character at ``offset``."""
    return text.count("\n", 0, offset) + 1


def find_occurrences(text: str, needle: str) -> list[int]:
    """Start offsets of non-overlapping occurrences, left to right — the
    same occurrences ``str.replace`` would replace."""
    out: list[int] = []
    if not needle:
        return out
    i = text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + len(needle))
    return out


def is_crlf(text: str) -> bool:
    """True for a file whose every newline is ``\\r\\n``."""
    n = text.count("\n")
    return n > 0 and text.count("\r\n") == n


# ── Closest region (not-found diagnostics) ────────────────────────────────


def closest_region(
    text: str, needle: str, *, time_budget_s: float = 1.5,
) -> Optional[tuple[int, int, float]]:
    """Best-matching window of the file for a ``needle`` that does not
    occur in it: ``(start_line, end_line, ratio)``, 1-based inclusive.

    The window has the same number of lines as ``needle``. Lines are
    compared with surrounding whitespace stripped, so an indentation
    mistake still finds its region. ``SequenceMatcher`` keeps the needle
    as seq2 (its index is built once) and the cheap upper bounds prune
    most windows; a time budget caps the scan on a huge file.
    """
    lines = split_lines(text)
    needle_lines = split_lines(needle)
    if not lines or not needle_lines:
        return None
    k = min(len(needle_lines), len(lines))
    stripped = [line.strip() for line in lines]
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2("\n".join(line.strip() for line in needle_lines))
    best_ratio, best_i = -1.0, 0
    deadline = time.monotonic() + time_budget_s
    for i in range(len(lines) - k + 1):
        sm.set_seq1("\n".join(stripped[i:i + k]))
        if sm.real_quick_ratio() <= best_ratio or sm.quick_ratio() <= best_ratio:
            continue
        ratio = sm.ratio()
        if ratio > best_ratio:
            best_ratio, best_i = ratio, i
        if time.monotonic() > deadline:
            break
    return best_i + 1, best_i + k, max(best_ratio, 0.0)


# ── Exact-string edit ─────────────────────────────────────────────────────


@dataclass
class EditResult:
    """A successful exact-string edit, computed but not yet written."""

    new_text: str
    replacements: int
    start_line: int          # first replaced region, in the NEW text
    end_line: int
    match_lines: list[int]   # first line of every replacement, NEW text
    total_lines: int         # line count of the new text
    snippet: str             # numbered context around the first replacement


def snippet_around(
    lines: list[str],
    start: int,
    end: int,
    *,
    context: int,
    max_lines: int,
    max_line_chars: Optional[int] = None,
) -> str:
    """Numbered lines ``start-context .. end+context``, with the middle
    elided when that is longer than ``max_lines``."""
    total = len(lines)
    if total == 0:
        return "(the file is now empty)"
    start = max(1, min(start, total))
    end = max(start, min(end, total))
    a = max(1, start - context)
    b = min(total, end + context)
    width = len(str(b))
    if b - a + 1 <= max_lines:
        return number_lines(
            lines[a - 1:b], a, max_line_chars=max_line_chars, width=width,
        )[0]
    head = max_lines // 2
    tail = max_lines - head
    bottom = b - tail + 1
    top_text = number_lines(
        lines[a - 1:a - 1 + head], a, max_line_chars=max_line_chars, width=width,
    )[0]
    bottom_text = number_lines(
        lines[bottom - 1:b], bottom, max_line_chars=max_line_chars, width=width,
    )[0]
    hidden = bottom - (a + head)
    return f"{top_text}\n{'':>{width}}\t… ({hidden} lines not shown)\n{bottom_text}"


def apply_edit(
    text: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    context: int = 3,
    max_snippet_lines: int = 40,
    max_listed: int = 20,
    max_line_chars: Optional[int] = None,
) -> EditResult:
    """Claude Code's Edit contract on an in-memory file.

    * ``old_string`` must be non-empty and differ from ``new_string``.
    * It must occur exactly once, unless ``replace_all`` (then every
      non-overlapping occurrence is replaced).
    * A file whose every newline is CRLF is matched and rewritten in LF
      form and converted back, so an ``old_string`` copied from numbered
      output (which shows lines without ``\\r``) matches and the file keeps
      CRLF. A trailing newline, or its absence, is preserved.

    Raises :class:`FileOpError` — ``empty_old_string``, ``no_change``,
    ``not_found`` (with the closest region when one scores at least
    ``CLOSEST_REGION_MIN_RATIO``), ``line_number_prefix``,
    ``multiple_matches`` — and never returns a partial edit.
    """
    if old_string == "":
        raise FileOpError(
            "empty_old_string",
            "old_string is empty — an exact-string edit replaces existing "
            "text; creating or filling a file is a write, not an edit.",
        )
    if old_string == new_string:
        raise FileOpError(
            "no_change", "old_string and new_string are identical — nothing to change.",
        )

    crlf = is_crlf(text)
    work = text.replace("\r\n", "\n") if crlf else text
    old = old_string.replace("\r\n", "\n") if crlf else old_string
    new = new_string.replace("\r\n", "\n") if crlf else new_string
    if old == new:
        raise FileOpError(
            "no_change",
            "old_string and new_string differ only in line endings — the "
            "file keeps its own line endings, so nothing would change.",
        )

    hits = find_occurrences(work, old)
    if not hits:
        _raise_not_found(work, old, max_line_chars=max_line_chars)
    if len(hits) > 1 and not replace_all:
        lines_hit = [line_of_offset(work, h) for h in hits]
        raise FileOpError(
            "multiple_matches",
            f"old_string occurs {len(hits)} times; it must identify exactly "
            "one place.",
            count=len(hits),
            match_lines=lines_hit[:max_listed],
        )

    used = hits if replace_all else hits[:1]
    if replace_all:
        new_work = work.replace(old, new)
    else:
        h = hits[0]
        new_work = work[:h] + new + work[h + len(old):]

    delta = len(new) - len(old)
    new_offsets = [h + k * delta for k, h in enumerate(used)]
    match_lines = [line_of_offset(new_work, o) for o in new_offsets]
    first = new_offsets[0]
    start_line = match_lines[0]
    if new:
        span = len(new) - 1 if new.endswith("\n") else len(new)
        end_line = line_of_offset(new_work, first + span)
    else:
        end_line = start_line
    new_lines = split_lines(new_work)
    total = len(new_lines)
    start_line = min(start_line, max(total, 1))
    end_line = min(max(end_line, start_line), max(total, 1))
    snippet = snippet_around(
        new_lines, start_line, end_line,
        context=context, max_lines=max_snippet_lines,
        max_line_chars=max_line_chars,
    )
    out_text = new_work.replace("\n", "\r\n") if crlf else new_work
    return EditResult(
        new_text=out_text,
        replacements=len(used),
        start_line=start_line,
        end_line=end_line,
        match_lines=match_lines[:max_listed],
        total_lines=total,
        snippet=snippet,
    )


def _raise_not_found(work: str, old: str, *, max_line_chars: Optional[int]) -> None:
    if has_line_number_prefixes(old):
        bare = strip_line_number_prefixes(old)
        n = len(find_occurrences(work, bare)) if bare.strip() else 0
        raise FileOpError(
            "line_number_prefix",
            "old_string starts every line with a line-number prefix "
            "(number + TAB). Those prefixes come from the numbered view and "
            "are not part of the file — use the text AFTER the tab.",
            matches_without_prefixes=n,
        )
    extra: dict = {}
    region = closest_region(work, old)
    if region is not None and region[2] >= CLOSEST_REGION_MIN_RATIO:
        a, b, ratio = region
        lines = split_lines(work)
        extra = {
            "closest_start_line": a,
            "closest_end_line": b,
            "similarity": round(ratio, 2),
            "closest_text": number_lines(
                lines[a - 1:b], a, max_line_chars=max_line_chars,
            )[0],
            "whitespace_only": ratio >= 0.999,
        }
    raise FileOpError(
        "not_found",
        "old_string was not found in the file. It must match the current "
        "text exactly — every space, tab and line.",
        **extra,
    )


# ── Whole-file rewrite similarity ─────────────────────────────────────────


@dataclass
class RewriteVerdict:
    """An existing file that a write would mostly re-send unchanged."""

    path: str
    similarity: float
    changed_lines: int
    old_lines: int
    new_lines: int
    identical: bool = False


def rewrite_guard(
    path: str,
    old_data: bytes,
    new_text: str,
    *,
    min_lines: int,
    threshold: float,
) -> Optional[RewriteVerdict]:
    """Return a verdict when writing ``new_text`` over ``old_data`` keeps
    most of the existing file (a needless whole-file rewrite), else ``None``.

    ``None`` for: a binary / non-UTF-8 existing file, an existing file
    under ``min_lines`` lines, and new content whose line-level
    ``SequenceMatcher`` ratio against the old is below ``threshold`` (a
    genuine rewrite). The thresholds are the caller's policy.
    """
    if looks_binary(old_data):
        return None
    try:
        old_text = old_data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    old_lines = split_lines(old_text)
    if len(old_lines) < min_lines:
        return None
    new_lines = split_lines(new_text)
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    ratio = sm.ratio()
    if ratio < threshold:
        return None
    changed = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in sm.get_opcodes()
        if tag != "equal"
    )
    return RewriteVerdict(
        path=path,
        similarity=round(ratio, 3),
        changed_lines=changed,
        old_lines=len(old_lines),
        new_lines=len(new_lines),
        identical=old_text == new_text,
    )
