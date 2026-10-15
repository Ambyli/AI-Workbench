"""common.textops — numbered views, exact-string edits and UTF-8-safe
truncation for tools that let a model read and change text files.

The contract mirrors Claude Code's file tools, so a service that exposes
files to a model gets the same behaviour everywhere:

* a READ is a ``cat -n``-style window — line number, TAB, line
  (:func:`number_lines`, :func:`split_lines`);
* an EDIT is an exact-string replacement: ``old_string`` must occur exactly
  once unless ``replace_all`` (:func:`apply_edit`). A miss raises
  :class:`FileOpError` carrying the closest region of the file
  (:func:`closest_region`), so the model can copy the real text instead of
  re-reading everything; a pasted line-number prefix is called out by name;
* a WHOLE-FILE WRITE over an existing file can be checked for being a
  needless rewrite (:func:`rewrite_guard`) — the thresholds are the
  caller's policy;
* byte caps cut on UTF-8 character boundaries (:func:`utf8_tail` keeps the
  end of a stream, :func:`decode_utf8_prefix` decodes a capped head).

Stdlib only (``codecs``, ``difflib``, ``re``) — no I/O, no framework. The
sandbox runner (``ai/sandbox``) is the first consumer: its ``read_file`` /
``edit_file`` / ``write_files`` MCP tools and its ``exec`` output tail.

Public API:
    apply_edit(text, old, new, replace_all=False, *, context, max_snippet_lines,
               max_listed, max_line_chars) → EditResult, or raises FileOpError
    closest_region(text, needle)         → (start_line, end_line, ratio) | None
    rewrite_guard(path, old_bytes, new_text, *, min_lines, threshold)
                                         → RewriteVerdict | None
    number_lines(lines, start, *, max_line_chars, width) → (text, lines_cut)
    snippet_around(lines, start, end, *, context, max_lines, max_line_chars)
    split_lines / line_of_offset / find_occurrences / is_crlf
    has_line_number_prefixes / strip_line_number_prefixes
    utf8_tail(data, max_bytes) / decode_utf8_prefix(data) / looks_binary(data)
    FileOpError(kind, message, **extra), EditResult, RewriteVerdict
    LINE_NUMBER_PREFIX_RE, CLOSEST_REGION_MIN_RATIO
"""

from .edit import (
    CLOSEST_REGION_MIN_RATIO,
    LINE_NUMBER_PREFIX_RE,
    EditResult,
    FileOpError,
    RewriteVerdict,
    apply_edit,
    closest_region,
    decode_utf8_prefix,
    find_occurrences,
    has_line_number_prefixes,
    is_crlf,
    line_of_offset,
    looks_binary,
    number_lines,
    rewrite_guard,
    snippet_around,
    split_lines,
    strip_line_number_prefixes,
    utf8_tail,
)

__all__ = [
    "CLOSEST_REGION_MIN_RATIO",
    "LINE_NUMBER_PREFIX_RE",
    "EditResult",
    "FileOpError",
    "RewriteVerdict",
    "apply_edit",
    "closest_region",
    "decode_utf8_prefix",
    "find_occurrences",
    "has_line_number_prefixes",
    "is_crlf",
    "line_of_offset",
    "looks_binary",
    "number_lines",
    "rewrite_guard",
    "snippet_around",
    "split_lines",
    "strip_line_number_prefixes",
    "utf8_tail",
]
