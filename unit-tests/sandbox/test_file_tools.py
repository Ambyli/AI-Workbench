"""read_file / edit_file / the write_files rewrite guard / exec and
get_files truncation, through ``spawner`` and ``operations`` against the
fake Docker client in ``sandbox_fakes.py``. No Docker, no Postgres.

    uv run --no-sync pytest unit-tests/sandbox -q
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from sandbox_fakes import (
    FakeAPI,
    FakeContainer,
    assert_all_owned,
    assert_parents_listed_first,
    install,
    spawner_with,
)

import constants  # noqa: E402
from common.textops import FileOpError  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _http(coro) -> HTTPException:
    with pytest.raises(HTTPException) as ei:
        run(coro)
    return ei.value


SRC = "".join(f"line {i}\n" for i in range(1, 101))  # 100 lines


# ── Spawner.read_file_bytes ───────────────────────────────────────────────


def test_read_file_bytes_reads_one_file():
    c = FakeContainer({"src/a.js": b"hello\n"})
    sp = spawner_with(c)
    assert sp.read_file_bytes("x", "src/a.js", 1000) == ("src/a.js", b"hello\n", 6, False)


def test_read_file_bytes_caps_and_reports_real_size():
    c = FakeContainer({"big.txt": b"x" * 5000})
    safe, data, size, truncated = spawner_with(c).read_file_bytes("x", "big.txt", 1000)
    assert (len(data), size, truncated) == (1000, 5000, True)


@pytest.mark.parametrize("path,kind", [
    ("missing.js", "missing_file"),
    ("src", "is_directory"),
    ("../etc/passwd", "unsafe_path"),
    ("/etc/passwd", "unsafe_path"),
])
def test_read_file_bytes_refusals(path, kind):
    c = FakeContainer({"src/a.js": b"1"}, dirs={"src"})
    with pytest.raises(FileOpError) as ei:
        spawner_with(c).read_file_bytes("x", path, 1000)
    assert ei.value.kind == kind


def test_read_existing_files_skips_missing_dirs_unsafe_and_oversized():
    c = FakeContainer({"a.js": b"a", "big.js": b"b" * 50}, dirs={"src"})
    got = spawner_with(c).read_existing_files("x", ["a.js", "new.js", "src", "../x", "big.js"], 10)
    assert got == {"a.js": b"a"}


def test_get_files_directory_path_is_an_error_not_a_file_inside_it():
    c = FakeContainer({"src/a.js": b"1"}, dirs={"src"})
    [entry] = spawner_with(c).read_files("x", ["src"], 1000)
    assert "directory" in entry["error"]


def test_get_files_truncation_cuts_on_a_utf8_boundary():
    text = "é" * 10  # 2 bytes each
    c = FakeContainer({"t.txt": text.encode()})
    [entry] = spawner_with(c).read_files("x", ["t.txt"], 7)
    assert entry["encoding"] == "utf-8"  # not the base64 fallback
    assert entry["content"] == "é" * 3
    assert entry["truncated"] is True and entry["size"] == 20


def test_put_files_writes_owned_tarball():
    c = FakeContainer()
    spawner_with(c).put_files("x", {"src/deep/a.js": b"1\n"})
    tarball = c.put_calls[0][1]
    assert_parents_listed_first(assert_all_owned(tarball))
    assert c.files["src/deep/a.js"] == b"1\n"


# ── exec output tail ──────────────────────────────────────────────────────


def test_exec_keeps_the_tail_on_a_utf8_boundary():
    # 20 KB of output in uneven chunks, multi-byte characters throughout,
    # the error at the very end.
    body = ("é" * 7001).encode() + b"\nError: boom\n"
    chunks = [body[i:i + 997] for i in range(0, len(body), 997)]
    sp = spawner_with(FakeContainer(), FakeAPI(chunks=chunks, exit_code=1))
    res = sp.exec_command("x", "npm test", 5, "", 8192)
    assert res.truncated is True
    assert res.exit_code == 1
    out = res.output.encode("utf-8")
    assert len(out) <= 8192 and len(out) >= 8192 - 3
    assert res.output.endswith("Error: boom\n")
    assert "�" not in res.output  # never split a character


def test_exec_short_output_untouched():
    sp = spawner_with(FakeContainer(), FakeAPI(chunks=[b"ok\n"]))
    res = sp.exec_command("x", "true", 5, "", 8192)
    assert (res.output, res.truncated) == ("ok\n", False)


# ── operations._do_read_file ──────────────────────────────────────────────


def test_read_file_window_and_footer_fields(monkeypatch):
    ops, _sp, registry = install(monkeypatch, FakeContainer({"app.js": SRC}))
    r = run(ops._do_read_file("sess1", "app.js", 10, 5))
    assert r["content"].split("\n") == [f"{n}\tline {n}" for n in range(10, 15)]
    assert (r["start_line"], r["end_line"], r["total_lines"]) == (10, 14, 100)
    assert r["more_lines"] == 86 and r["truncated"] is True
    assert registry.metadata_updates  # counts as activity


def test_read_file_defaults_and_limit_cap(monkeypatch):
    ops, _sp, _r = install(monkeypatch, FakeContainer({"app.js": SRC}))
    r = run(ops._do_read_file("sess1", "app.js", None, None))
    assert (r["start_line"], r["end_line"], r["truncated"]) == (1, 100, False)
    big = "".join(f"{i}\n" for i in range(5000))
    ops, _sp, _r = install(monkeypatch, FakeContainer({"big.txt": big}))
    r = run(ops._do_read_file("sess1", "big.txt", 1, 999999))
    assert r["end_line"] == constants.READ_FILE_MAX_LINES


def test_read_file_cuts_long_lines(monkeypatch):
    long = "x" * (constants.READ_FILE_MAX_LINE_CHARS + 10)
    ops, _sp, _r = install(monkeypatch, FakeContainer({"min.js": long + "\nshort\n"}))
    r = run(ops._do_read_file("sess1", "min.js", 1, 10))
    assert r["lines_cut"] == 1
    assert "[line cut at" in r["content"].split("\n")[0]


def test_read_file_past_end(monkeypatch):
    ops, _sp, _r = install(monkeypatch, FakeContainer({"app.js": SRC}))
    r = run(ops._do_read_file("sess1", "app.js", 500, 10))
    assert r["content"] == "" and r["total_lines"] == 100


def test_read_file_byte_capped_file_shows_whole_lines_only(monkeypatch):
    monkeypatch.setattr("operations.READ_FILE_MAX_BYTES", 100)
    ops, _sp, _r = install(monkeypatch, FakeContainer({"app.js": SRC}))
    r = run(ops._do_read_file("sess1", "app.js", 1, 400))
    assert r["file_truncated"] is True and r["truncated"] is True
    last = r["content"].split("\n")[-1]
    assert last.split("\t")[1] == f"line {r['end_line']}"  # not a half line


@pytest.mark.parametrize("files,path,status,kind", [
    ({"logo.png": b"\x89PNG\x00\x00"}, "logo.png", 409, "binary_file"),
    ({"latin.txt": b"caf\xe9\n"}, "latin.txt", 409, "binary_file"),
    ({}, "missing.js", 404, "missing_file"),
    ({}, "../x", 400, "unsafe_path"),
])
def test_read_file_refusals(monkeypatch, files, path, status, kind):
    ops, _sp, _r = install(monkeypatch, FakeContainer(files))
    e = _http(ops._do_read_file("sess1", path))
    assert e.status_code == status
    assert e.detail["kind"] == kind and e.detail["tool"] == "read_file"
    assert e.detail["hint"]


def test_read_file_unknown_session_and_gone_container(monkeypatch):
    ops, _sp, _r = install(monkeypatch, FakeContainer({"a": "1"}))
    assert _http(ops._do_read_file("nope", "a")).status_code == 404
    ops, _sp, _r = install(monkeypatch, FakeContainer({"a": "1"}), alive=False)
    assert _http(ops._do_read_file("sess1", "a")).status_code == 409


# ── operations._do_edit_file ──────────────────────────────────────────────


def test_edit_file_writes_marker_then_owned_file(monkeypatch):
    c = FakeContainer({"src/app.js": SRC})
    ops, _sp, registry = install(monkeypatch, c)
    r = run(ops._do_edit_file("sess1", "src/app.js", "line 50\n", "line fifty\n"))
    assert c.files["src/app.js"].decode() == SRC.replace("line 50\n", "line fifty\n")
    assert (r["replacements"], r["start_line"], r["end_line"]) == (1, 50, 50)
    assert "50\tline fifty" in r["snippet"]
    assert "line 1\n" not in r["snippet"]  # a snippet, not the file
    assert r["app_status"] == {"code": 200, "latency_ms": 1}
    # Reload marker BEFORE the put, so startup_output is this edit's output.
    kinds = [k for k, _ in c.events]
    marker = next(i for i, (k, argv) in enumerate(c.events)
                  if k == "exec" and "write_files reload" in " ".join(argv))
    assert marker < kinds.index("put")
    assert_all_owned(c.put_calls[0][1])
    assert registry.metadata_updates


def test_edit_file_not_found_writes_nothing_and_shows_closest(monkeypatch):
    c = FakeContainer({"app.js": SRC})
    ops, _sp, _r = install(monkeypatch, c)
    e = _http(ops._do_edit_file("sess1", "app.js", "lime 50\n", "x\n"))
    assert e.status_code == 409
    assert e.detail["kind"] == "not_found"
    assert e.detail["closest_start_line"] == 50
    assert "50\tline 50" in e.detail["closest_text"]
    assert "lines 50-50" in e.detail["hint"]
    assert c.put_calls == []


def test_edit_file_multiple_matches(monkeypatch):
    c = FakeContainer({"a.py": "x = 1\ny = 2\nx = 1\n"})
    ops, _sp, _r = install(monkeypatch, c)
    e = _http(ops._do_edit_file("sess1", "a.py", "x = 1", "x = 2"))
    assert (e.status_code, e.detail["kind"], e.detail["match_lines"]) == (409, "multiple_matches", [1, 3])
    assert "replace_all=true" in e.detail["hint"]
    r = run(ops._do_edit_file("sess1", "a.py", "x = 1", "x = 2", True))
    assert r["replacements"] == 2
    assert c.files["a.py"] == b"x = 2\ny = 2\nx = 2\n"


def test_edit_file_python_lint_blocks_the_write(monkeypatch):
    c = FakeContainer({"app.py": "def f():\n    return 1\n"})
    ops, _sp, _r = install(monkeypatch, c)
    e = _http(ops._do_edit_file("sess1", "app.py", "    return 1", "    return (1"))
    assert e.status_code == 400 and e.detail["error"] == "static lint failed"
    assert c.put_calls == []


def test_edit_file_missing_and_binary(monkeypatch):
    c = FakeContainer({"logo.png": b"\x89PNG\x00"})
    ops, _sp, _r = install(monkeypatch, c)
    e = _http(ops._do_edit_file("sess1", "nope.js", "a", "b"))
    assert (e.status_code, e.detail["kind"]) == (404, "missing_file")
    assert "write_files" in e.detail["hint"]
    e = _http(ops._do_edit_file("sess1", "logo.png", "PNG", "GIF"))
    assert (e.status_code, e.detail["kind"]) == (409, "binary_file")


def test_edit_file_result_over_cap(monkeypatch):
    monkeypatch.setattr("operations.MAX_FILE_BYTES", 50)
    c = FakeContainer({"a.txt": "short\n"})
    ops, _sp, _r = install(monkeypatch, c)
    e = _http(ops._do_edit_file("sess1", "a.txt", "short", "x" * 60))
    assert (e.status_code, e.detail["kind"]) == (413, "too_large")
    assert c.put_calls == []


# ── write_files rewrite guard (operations) ────────────────────────────────


def _write(ops, files, overwrite=False):
    return run(ops._do_write_files("sess1", files, [], False, overwrite=overwrite))


def test_write_guard_refuses_similar_rewrite_and_writes_nothing(monkeypatch):
    c = FakeContainer({"app.js": SRC})
    ops, _sp, _r = install(monkeypatch, c)
    with pytest.raises(HTTPException) as ei:
        _write(ops, {"app.js": SRC.replace("line 7\n", "line seven\n"), "new.js": "x"})
    e = ei.value
    assert e.status_code == 409
    assert e.detail["kind"] == "rewrite_of_existing_file"
    assert e.detail["files"][0]["path"] == "app.js"
    assert e.detail["files"][0]["changed_lines"] == 1
    assert "keeps 99% of its lines (1 of 100 changed)" in e.detail["message"]
    assert "overwrite=true" in e.detail["hint"] and "edit_file" in e.detail["hint"]
    assert c.put_calls == []  # all or nothing — new.js was not written either


def test_write_guard_overwrite_true_writes(monkeypatch):
    c = FakeContainer({"app.js": SRC})
    ops, _sp, _r = install(monkeypatch, c)
    new = SRC.replace("line 7\n", "line seven\n")
    _write(ops, {"app.js": new}, overwrite=True)
    assert c.files["app.js"].decode() == new


@pytest.mark.parametrize("existing,new", [
    ({}, SRC),                                              # new file
    ({"app.js": "a\nb\n"}, "a\nb\nc\n"),                     # small file
    ({"app.js": SRC}, "".join(f"other {i}\n" for i in range(100))),  # real rewrite
    ({"app.js": b"\x00" * 400}, SRC),                       # binary existing file
])
def test_write_guard_allows(monkeypatch, existing, new):
    c = FakeContainer(existing)
    ops, _sp, _r = install(monkeypatch, c)
    _write(ops, {"app.js": new})
    assert c.files["app.js"].decode() == new


def test_write_guard_ignores_base64_values(monkeypatch):
    import base64
    c = FakeContainer({"app.js": SRC})
    ops, _sp, _r = install(monkeypatch, c)
    _write(ops, {"app.js": {"encoding": "base64", "content": base64.b64encode(SRC.encode()).decode()}})
    assert c.put_calls


def test_run_follow_up_is_guarded_too(monkeypatch):
    c = FakeContainer({"app.js": SRC})
    ops, _sp, _r = install(monkeypatch, c)
    e = _http(ops._reuse_or_spawn("node", {"app.js": SRC + "x\n"}, None, None, "sess1", []))
    assert e.status_code == 409 and e.detail["tool"] == "run"
    run(ops._reuse_or_spawn("node", {"app.js": SRC + "x\n"}, None, None, "sess1", [], overwrite=True))
    assert c.files["app.js"].decode().endswith("x\n")


# ── the guard decision as a pure function ─────────────────────────────────


def _file(n, tag="line"):
    return "".join(f"{tag} {i}\n" for i in range(n))


def test_guard_writes_only_checks_existing_text_files():
    import operations

    big = _file(60)
    current = {"app.js": big.encode(), "logo.png": b"\x89PNG" + b"\x00" * 99}
    files = {
        "app.js": big.replace("line 3\n", "line three\n"),           # guarded
        "new.js": big,                                                # new file
        "logo.png": {"encoding": "base64", "content": "AAAA"},        # binary value
    }
    assert [v.path for v in operations._guard_writes(files, current)] == ["app.js"]
    # A dict value with utf-8 encoding is text and is guarded too.
    files2 = {"app.js": {"encoding": "utf-8", "content": big}}
    assert [v.path for v in operations._guard_writes(files2, current)] == ["app.js"]
    # The thresholds are the runner's constants.
    small = _file(constants.WRITE_GUARD_MIN_LINES - 1)
    assert operations._guard_writes({"s.js": small + "x\n"}, {"s.js": small.encode()}) == []
