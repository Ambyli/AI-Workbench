"""common.textops — the numbered view, the exact-string edit, the
whole-file rewrite check and UTF-8-safe truncation. Pure; no I/O.

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_textops.py -q
"""

from __future__ import annotations

import pytest

from common.textops import (
    FileOpError,
    apply_edit,
    closest_region,
    decode_utf8_prefix,
    has_line_number_prefixes,
    number_lines,
    rewrite_guard,
    split_lines,
    utf8_tail,
)

SRC = (
    "import express from 'express';\n"
    "const app = express();\n"
    "\n"
    "function greet(name) {\n"
    "  return 'hi, ' + name;\n"
    "}\n"
    "\n"
    "app.get('/', (req, res) => res.send(greet('x')));\n"
    "app.listen(80);\n"
)


def _edit(text=SRC, old="  return 'hi, ' + name;", new="  return `Hello, ${name}!`;", **kw):
    return apply_edit(text, old, new, context=3, max_snippet_lines=40, max_listed=20, **kw)


def _err(fn, *a, **kw) -> FileOpError:
    with pytest.raises(FileOpError) as ei:
        fn(*a, **kw)
    return ei.value


# ── lines ─────────────────────────────────────────────────────────────────


def test_split_lines_no_phantom_last_line_and_strips_cr():
    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("a\nb") == ["a", "b"]
    assert split_lines("a\r\nb\r\n") == ["a", "b"]
    assert split_lines("") == []
    assert split_lines("\n") == [""]


def test_number_lines_cat_n_style_and_cuts_long_lines():
    text, cut = number_lines(["x", "y" * 50], 9, max_line_chars=10)
    rows = text.split("\n")
    assert rows[0] == " 9\tx"
    assert rows[1].startswith("10\tyyyyyyyyyy… [line cut at 10 of 50 chars]")
    assert cut == 1


def test_line_number_prefix_detection():
    assert has_line_number_prefixes("  4\tfunction f() {\n  5\t  return 1;")
    assert not has_line_number_prefixes("function f() {\n  return 1;")
    assert not has_line_number_prefixes("4\tx\nplain")


# ── apply_edit ────────────────────────────────────────────────────────────


def test_edit_unique_match_reports_lines_and_snippet():
    r = _edit()
    assert "return `Hello, ${name}!`;" in r.new_text
    assert "'hi, '" not in r.new_text
    assert (r.replacements, r.start_line, r.end_line) == (1, 5, 5)
    rows = r.snippet.split("\n")
    # ±3 lines of context around line 5, numbered, never the whole file.
    assert rows[0].startswith("2\t") and rows[-1].startswith("8\t")
    assert "5\t  return `Hello, ${name}!`;" in rows


def test_edit_multiline_old_and_new():
    r = _edit(old="function greet(name) {\n  return 'hi, ' + name;\n}",
              new="const greet = (name) =>\n  'hi, ' + name;")
    assert (r.start_line, r.end_line) == (4, 5)
    assert r.new_text.count("\n") == SRC.count("\n") - 1


def test_edit_deletion_new_string_empty():
    r = _edit(old="import express from 'express';\n", new="")
    assert not r.new_text.startswith("import")
    assert r.start_line == r.end_line == 1


def test_edit_not_found_returns_closest_region():
    e = _err(_edit, old="  return 'hello, ' + name;")
    assert e.kind == "not_found"
    assert (e.extra["closest_start_line"], e.extra["closest_end_line"]) == (5, 5)
    assert e.extra["closest_text"] == "5\t  return 'hi, ' + name;"
    assert e.extra["whitespace_only"] is False


def test_edit_not_found_indentation_only_is_flagged():
    # Four-space indent where the file has two.
    e = _err(_edit, old="function greet(name) {\n    return 'hi, ' + name;\n}", new="x")
    assert e.kind == "not_found"
    assert (e.extra["closest_start_line"], e.extra["closest_end_line"]) == (4, 6)
    assert e.extra["whitespace_only"] is True


def test_edit_not_found_nothing_similar_has_no_region():
    e = _err(_edit, old="zzzzzzzzzzzzzzzzzzzzzzzzzzzz", new="y")
    assert e.kind == "not_found"
    assert "closest_text" not in e.extra


def test_edit_line_number_prefixes_are_called_out():
    e = _err(_edit, old="5\t  return 'hi, ' + name;", new="x")
    assert e.kind == "line_number_prefix"
    assert e.extra["matches_without_prefixes"] == 1


def test_edit_multiple_matches_lists_lines():
    text = "a = 1\nb = 2\na = 1\n"
    e = _err(_edit, text, "a = 1", "a = 3")
    assert e.kind == "multiple_matches"
    assert e.extra == {"count": 2, "match_lines": [1, 3]}


def test_edit_replace_all():
    r = _edit("a = 1\nb = 2\na = 1\n", "a = 1", "a = 3", replace_all=True)
    assert r.new_text == "a = 3\nb = 2\na = 3\n"
    assert r.replacements == 2
    assert r.match_lines == [1, 3]


def test_edit_replace_all_with_length_change_reports_new_lines():
    r = _edit("x\nfoo\nfoo\n", "foo", "bar\nbaz", replace_all=True)
    assert r.new_text == "x\nbar\nbaz\nbar\nbaz\n"
    assert r.match_lines == [2, 4]


@pytest.mark.parametrize("old,new,kind", [
    ("same", "same", "no_change"),
    ("", "x", "empty_old_string"),
])
def test_edit_refuses_no_op_and_empty(old, new, kind):
    assert _err(_edit, "same\n", old, new).kind == kind


def test_edit_preserves_crlf_and_matches_lf_old_string():
    text = "line1\r\nline2\r\nline3\r\n"
    r = _edit(text, "line2\nline3", "LINE2\nLINE3")
    assert r.new_text == "line1\r\nLINE2\r\nLINE3\r\n"


def test_edit_crlf_only_line_ending_difference_is_no_change():
    assert _err(_edit, "a\r\nb\r\n", "a\r\nb", "a\nb").kind == "no_change"


def test_edit_preserves_missing_trailing_newline():
    r = _edit("a\nb", "b", "c")
    assert r.new_text == "a\nc"


def test_edit_long_new_string_snippet_is_capped():
    new = "\n".join(f"line {i}" for i in range(200))
    r = apply_edit("start\nX\nend\n", "X", new, context=3, max_snippet_lines=40)
    rows = r.snippet.split("\n")
    assert len(rows) == 41  # 40 numbered rows + the elision marker
    assert any("lines not shown" in row for row in rows)


def test_closest_region_window_is_needle_sized():
    text = "\n".join(f"row {i}" for i in range(50)) + "\n"
    a, b, ratio = closest_region(text, "row 20\nrow 2l\nrow 22")
    assert (a, b) == (21, 23)
    assert ratio > 0.9


# ── rewrite_guard ─────────────────────────────────────────────────────────


def _file(n, tag="line"):
    return "".join(f"{tag} {i}\n" for i in range(n))


def _guard(old, new, **kw):
    return rewrite_guard("app.js", old.encode() if isinstance(old, str) else old, new,
                         min_lines=kw.get("min_lines", 30), threshold=kw.get("threshold", 0.5))


def test_guard_flags_mostly_unchanged_rewrite():
    old = _file(100)
    new = old.replace("line 50\n", "line fifty\n")
    v = _guard(old, new)
    assert v is not None
    assert v.changed_lines == 1 and v.old_lines == 100
    assert v.similarity >= 0.99


def test_guard_allows_small_files():
    old = _file(29)
    assert _guard(old, old + "x\n") is None
    assert _guard(_file(30), _file(30) + "x\n") is not None


def test_guard_allows_genuine_rewrite():
    assert _guard(_file(100), _file(100, tag="other")) is None


def test_guard_threshold_boundary():
    old = _file(40)
    # Keep 20 of 40 lines, replace the rest: ratio = 2*20/(40+40) = 0.5.
    new = _file(20) + _file(20, tag="new")
    assert _guard(old, new) is not None
    assert _guard(old, new, threshold=0.51) is None


def test_guard_ignores_binary_existing_file():
    assert _guard(b"\x00\x01" * 100, "x\n" * 100) is None
    assert _guard(b"\xff\xfe" + b"a\n" * 100, "a\n" * 100) is None


def test_guard_identical_content_flagged():
    v = _guard(_file(40), _file(40))
    assert v is not None and v.identical and v.changed_lines == 0


# ── UTF-8 boundaries ──────────────────────────────────────────────────────


def test_utf8_tail_starts_on_a_character_boundary():
    data = "a€b€".encode()  # € is 3 bytes
    for cap in range(1, len(data) + 1):
        tail = utf8_tail(data, cap)
        assert len(tail) <= cap
        tail.decode("utf-8")  # never splits a character
        assert data.endswith(tail)
    assert utf8_tail(data, 100) == data


def test_decode_utf8_prefix_drops_partial_last_character():
    data = "ab€".encode()
    assert decode_utf8_prefix(data[:-1]) == "ab"
    assert decode_utf8_prefix(data) == "ab€"
    with pytest.raises(UnicodeDecodeError):
        decode_utf8_prefix(b"\xff\xfeabc")
