"""The sandbox MCP tool surface (``sandbox_mcp.build_mcp``): what the model
reads (text) and what structured callers get, for read_file / edit_file /
exec / get_files / write_files — plus the ``app.py`` adapters, which now
validate MCP arguments through the same pydantic models as HTTP.

    uv run --no-sync pytest unit-tests/sandbox -q
"""

from __future__ import annotations

import asyncio
import importlib
import re

import pytest
from fastapi import HTTPException

import sandbox_fakes  # noqa: F401  (puts ai/sandbox/runner on sys.path)
import constants  # noqa: E402
import sandbox_mcp  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Calls:
    """Fake ``*_callable``s for build_mcp: each returns the canned result
    (or raises the canned exception) and records its arguments."""

    def __init__(self, **results):
        self.results = results
        self.args: dict[str, tuple] = {}

    def __getattr__(self, name):
        async def fn(*args, **kwargs):
            self.args[name] = (args, kwargs)
            res = self.results.get(name)
            if isinstance(res, Exception):
                raise res
            return res
        return fn


def build(calls: Calls):
    return sandbox_mcp.build_mcp(
        run_callable=calls.run,
        logs_callable=calls.logs,
        create_callable=calls.create,
        write_files_callable=calls.write_files,
        read_file_callable=calls.read_file,
        edit_file_callable=calls.edit_file,
        get_files_callable=calls.get_files,
        exec_callable=calls.exec,
        preview_callable=calls.preview,
        close_callable=calls.close,
        list_sessions_callable=calls.list_sessions,
    )


def call(calls: Calls, tool: str, args: dict):
    result = run(build(calls).call_tool(tool, args))
    return result.content[0].text, result.structured_content


def test_tool_surface():
    names = sorted(t.name for t in run(build(Calls()).list_tools()))
    assert names == sorted([
        "get_runtime_types", "create", "write_files", "read_file", "edit_file",
        "get_files", "get_logs", "exec", "preview", "close", "list_sessions", "run",
    ])


# ── exec ──────────────────────────────────────────────────────────────────


EXEC_RESULT = {
    "session_id": "sess1", "sandbox_id": "abc", "command": "IGNORED",
    "exit_code": 0, "duration_ms": 12, "output": "ok\n", "truncated": False,
    "timed_out": False,
}


def test_exec_echoes_only_the_first_line_of_a_heredoc():
    heredoc = "cat > t.js <<'EOF'\n" + "console.log(1);\n" * 50 + "EOF\nnode t.js"
    text, structured = call(Calls(exec=EXEC_RESULT), "exec",
                            {"session_id": "sess1", "command": heredoc})
    first = text.split("\n")[0]
    assert first == "Command `cat > t.js <<'EOF'` (+52 more lines) exited 0 in 12 ms."
    assert "console.log" not in text
    assert "command" not in structured and "output" not in structured
    assert structured["exit_code"] == 0 and structured["ok"] is True


def test_exec_echo_cuts_a_long_first_line():
    echo = sandbox_mcp._command_echo("x" * 500)
    assert echo == "`" + "x" * constants.EXEC_COMMAND_ECHO_CHARS + "…`"
    assert sandbox_mcp._command_echo("ls") == "`ls`"
    assert sandbox_mcp._command_echo("a\nb") == "`a` (+1 more line)"


def test_exec_truncated_and_timeout_wording():
    res = dict(EXEC_RESULT, truncated=True, output="tail\n")
    text, _ = call(Calls(exec=res), "exec", {"session_id": "s", "command": "make"})
    assert "--- output (last 8 KiB — earlier output dropped) ---" in text
    res = dict(EXEC_RESULT, timed_out=True, exit_code=-1)
    text, _ = call(Calls(exec=res), "exec",
                   {"session_id": "s", "command": "sleep 99", "timeout_seconds": 5})
    assert "TIMED OUT after 5s" in text


# ── read_file ─────────────────────────────────────────────────────────────


READ_RESULT = {
    "session_id": "sess1", "sandbox_id": "abc", "path": "app.js",
    "start_line": 1, "end_line": 2, "total_lines": 10, "more_lines": 8,
    "truncated": True, "lines_cut": 0, "size": 80, "file_truncated": False,
    "content": "1\tconst a = 1;\n2\tconst b = 2;",
}


def test_read_file_text_and_minimal_structured():
    calls = Calls(read_file=READ_RESULT)
    text, structured = call(calls, "read_file",
                            {"session_id": "sess1", "path": "app.js", "limit": 2})
    assert text.split("\n") == [
        "app.js (lines 1-2 of 10):",
        "1\tconst a = 1;",
        "2\tconst b = 2;",
        "… 8 more lines; call read_file with offset=3.",
    ]
    assert structured == {
        "ok": True, "session_id": "sess1", "sandbox_id": "abc", "path": "app.js",
        "start_line": 1, "end_line": 2, "total_lines": 10, "truncated": True,
    }
    assert calls.args["read_file"][0] == ("sess1", "app.js", 1, 2)


def test_read_file_empty_and_past_end():
    text, _ = call(Calls(read_file=dict(READ_RESULT, total_lines=0, content="")),
                   "read_file", {"session_id": "s", "path": "app.js"})
    assert text == "app.js is empty (0 lines)."
    text, _ = call(Calls(read_file=dict(READ_RESULT, start_line=50, content="")),
                   "read_file", {"session_id": "s", "path": "app.js", "offset": 50})
    assert "offset 50 is past the end" in text


def test_read_file_binary_refusal_is_a_readable_diagnostic():
    exc = HTTPException(409, {
        "error": "not a UTF-8 text file", "kind": "binary_file", "tool": "read_file",
        "session_id": "sess1", "path": "logo.png", "message": "logo.png is not UTF-8 text.",
        "hint": "read_file shows UTF-8 text only. get_files(paths=[...]) returns a binary file as base64.",
    })
    text, structured = call(Calls(read_file=exc), "read_file",
                            {"session_id": "sess1", "path": "logo.png"})
    assert text.startswith("failed: not a UTF-8 text file")
    assert "read_file logo.png: logo.png is not UTF-8 text." in text
    assert "get_files" in text
    assert structured["ok"] is False and structured["status"] == 409


# ── edit_file ─────────────────────────────────────────────────────────────


EDIT_RESULT = {
    "sandbox_id": "abc", "session_id": "sess1", "url": "u", "expires_at": "e",
    "runtime": "node", "path": "app.js", "replacements": 1, "start_line": 5,
    "end_line": 5, "match_lines": [5], "total_lines": 9,
    "snippet": "4\tfunction greet(name) {\n5\t  return `Hello`;\n6\t}",
    "startup_output": "", "app_status": {"code": 200, "latency_ms": 3},
    "recreated": False,
}


def test_edit_file_success_is_short():
    calls = Calls(edit_file=EDIT_RESULT)
    text, structured = call(calls, "edit_file", {
        "session_id": "sess1", "path": "app.js",
        "old_string": "  return 'hi';", "new_string": "  return `Hello`;",
    })
    rows = text.split("\n")
    assert rows[0] == "Edited app.js: 1 replacement, lines 5-5."
    assert rows[1:4] == EDIT_RESULT["snippet"].split("\n")
    assert rows[4] == "App status: HTTP 200 in 3 ms"
    assert "snippet" not in structured
    assert calls.args["edit_file"][0] == (
        "sess1", "app.js", "  return 'hi';", "  return `Hello`;", False,
    )


def test_edit_file_not_found_shows_closest_text_and_hint():
    exc = HTTPException(409, {
        "error": "old_string not found", "kind": "not_found", "tool": "edit_file",
        "session_id": "sess1", "path": "app.js",
        "message": "old_string was not found in the file.",
        "closest_start_line": 5, "closest_end_line": 5, "similarity": 0.91,
        "closest_text": "5\t  return 'hi, ' + name;", "whitespace_only": False,
        "hint": "The closest text is lines 5-5 (shown above). Copy old_string exactly from it.",
    })
    text, _ = call(Calls(edit_file=exc), "edit_file", {
        "session_id": "sess1", "path": "app.js", "old_string": "a", "new_string": "b",
    })
    assert "Closest text in the file (lines 5-5, 91% similar):" in text
    assert "5\t  return 'hi, ' + name;" in text
    assert text.rstrip().endswith("Copy old_string exactly from it.")
    assert "{" not in text  # rendered, not a JSON dump


# ── write_files / run ─────────────────────────────────────────────────────


def test_write_files_passes_overwrite_and_renders_rewrite_guard():
    exc = HTTPException(409, {
        "error": "rewrite of existing file", "kind": "rewrite_of_existing_file",
        "tool": "write_files", "session_id": "sess1",
        "files": [{"path": "app.js", "similarity": 0.96}],
        "message": "app.js already exists and your new content keeps 96% of its lines (12 of 310 changed).",
        "hint": "No files were written. Use edit_file for changes like this.",
    })
    calls = Calls(write_files=exc)
    text, _ = call(calls, "write_files",
                   {"session_id": "sess1", "files": {"app.js": "x"}, "overwrite": True})
    assert calls.args["write_files"][0] == ("sess1", {"app.js": "x"}, [], False, True)
    assert "keeps 96% of its lines (12 of 310 changed)" in text
    assert "Use edit_file" in text


def test_run_passes_overwrite():
    calls = Calls(run={
        "sandbox_id": "abc", "session_id": "s", "url": "https://x/sandboxes/abc/",
        "expires_at": "e", "reused": True, "runtime": "node",
        "startup_output": "", "app_status": {"code": 200, "latency_ms": 1},
    })
    text, _ = call(calls, "run", {"runtime": "node", "session_id": "s",
                                  "files": {"a.js": "1"}, "overwrite": True})
    assert calls.args["run"][1]["overwrite"] is True
    assert "edit_file" in text and "patch" not in text


# ── get_files ─────────────────────────────────────────────────────────────


def test_get_files_listing_has_no_encoding_and_does_not_crash():
    calls = Calls(get_files={"session_id": "s", "sandbox_id": "abc", "files": [
        {"path": "app.js", "size": 1234}, {"path": "index.html", "size": 10},
    ]})
    text, _ = call(calls, "get_files", {"session_id": "s"})
    assert "- app.js (1,234 bytes)" in text


def test_get_files_contents_only_in_text():
    calls = Calls(get_files={"session_id": "s", "sandbox_id": "abc", "files": [
        {"path": "a.txt", "size": 5000, "encoding": "utf-8", "content": "hello",
         "truncated": True},
    ]})
    text, structured = call(calls, "get_files", {"session_id": "s", "paths": ["a.txt"]})
    assert "--- a.txt ---\nhello" in text
    assert "truncated: first 5 of 5,000 bytes" in text
    assert "content" not in structured["files"][0]


# ── guidance strings ──────────────────────────────────────────────────────


def _tool_texts():
    """Every description a model reads: tool descriptions and the
    descriptions of every parameter."""
    out = {}
    for t in run(build(Calls()).list_tools()):
        params = t.parameters.get("properties", {})
        out[t.name] = "\n".join(
            [t.description or ""] + [p.get("description", "") for p in params.values()]
        )
    return out


def test_no_tool_mentions_a_removed_tool_or_legacy_wording():
    for name, text in _tool_texts().items():
        low = text.lower()
        assert not re.search(r"patch(_files)?", low), name
        for word in ("legacy", "deprecated", "formerly"):
            assert word not in low, (name, word)
    startup = sandbox_mcp._format_startup_output("node", "Error: boom")
    assert "edit_file" in startup and "patch" not in startup


def test_tool_descriptions_quote_the_current_limits():
    texts = _tool_texts()
    read = texts["read_file"]
    assert f"first {constants.READ_FILE_DEFAULT_LINES} lines" in read
    assert f"at most {constants.READ_FILE_MAX_LINES}" in read
    assert f"longer than {constants.READ_FILE_MAX_LINE_CHARS} characters" in read
    assert f"first {constants.READ_FILE_MAX_BYTES // 1024} KiB" in read
    write = texts["write_files"]
    assert f"{constants.WRITE_GUARD_MIN_LINES}+ lines" in write
    assert f"{round(constants.WRITE_GUARD_SIMILARITY * 100)}%+ of its lines" in write
    assert f"{constants.WRITE_GUARD_MIN_LINES}+ lines" in texts["run"]
    ex = texts["exec"]
    assert f"LAST {constants.EXEC_MAX_OUTPUT_BYTES // 1024} KiB" in ex
    assert f"Hard cap {constants.EXEC_HARD_TIMEOUT_S}" in ex
    assert f"{constants.EDIT_SNIPPET_CONTEXT_LINES} lines of context" in texts["edit_file"]
    assert f"Hard cap {constants.GET_FILES_HARD_CAP_BYTES}" in texts["get_files"]


def test_descriptions_follow_the_constants(monkeypatch):
    """Rebuilt with other limits, the text changes with them — nothing in a
    description is a hard-coded copy of a limit."""
    import importlib

    monkeypatch.setattr(constants, "READ_FILE_DEFAULT_LINES", 123)
    monkeypatch.setattr(constants, "WRITE_GUARD_MIN_LINES", 77)
    monkeypatch.setattr(constants, "EXEC_MAX_OUTPUT_BYTES", 16 * 1024)
    try:
        mod = importlib.reload(sandbox_mcp)
        assert "first 123 lines" in mod.READ_FILE_DESCRIPTION
        assert "77+ lines" in mod.WRITE_FILES_DESCRIPTION
        assert "LAST 16 KiB" in mod.EXEC_DESCRIPTION
    finally:
        monkeypatch.undo()
        importlib.reload(sandbox_mcp)


# ── app.py adapters validate like HTTP ────────────────────────────────────


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    # app.py sets up logging into LOG_DIR at import; keep it out of /data.
    constants.LOG_DIR = str(tmp_path_factory.mktemp("sandbox-runner-logs"))
    return importlib.import_module("app")


def test_mcp_write_files_applies_the_per_file_cap(app_module):
    big = "x" * (constants.MAX_FILE_BYTES + 1)
    with pytest.raises(HTTPException) as ei:
        run(app_module._mcp_write_files("sess1", {"big.txt": big}, [], False))
    assert ei.value.status_code == 413
    assert ei.value.detail["error"] == "payload too large"
    assert "big.txt" in ei.value.detail["message"]


def test_mcp_run_and_edit_and_create_validate(app_module):
    big = "x" * (constants.MAX_FILE_BYTES + 1)
    with pytest.raises(HTTPException) as ei:
        run(app_module._mcp_run("node", {"a.js": big}, None, None, None, []))
    assert ei.value.status_code == 413
    with pytest.raises(HTTPException) as ei:
        run(app_module._mcp_edit_file("sess1", "a.js", "a", big, False))
    assert ei.value.status_code == 413
    with pytest.raises(HTTPException) as ei:
        run(app_module._mcp_create("node", None, None, {"HTTP_PROXY": "x"}))
    assert ei.value.status_code == 422
    assert "reserved" in ei.value.detail["message"]


def test_end_to_end_read_then_edit_through_the_real_stack(app_module, monkeypatch):
    from sandbox_fakes import FakeContainer, install

    src = "".join(f"line {i}\n" for i in range(1, 61))
    c = FakeContainer({"src/app.js": src})
    install(monkeypatch, c)
    mcp = app_module._mcp

    r = run(mcp.call_tool("read_file", {
        "session_id": "sess1", "path": "src/app.js", "offset": 30, "limit": 3,
    }))
    assert r.content[0].text.split("\n") == [
        "src/app.js (lines 30-32 of 60):",
        "30\tline 30", "31\tline 31", "32\tline 32",
        "… 28 more lines; call read_file with offset=33.",
    ]

    # The model pastes the numbered line by mistake: told, nothing written.
    r = run(mcp.call_tool("edit_file", {
        "session_id": "sess1", "path": "src/app.js",
        "old_string": "31\tline 31", "new_string": "line thirty-one",
    }))
    text = r.content[0].text
    assert "line-number" in text and "exactly one place" in text
    assert c.put_calls == []

    r = run(mcp.call_tool("edit_file", {
        "session_id": "sess1", "path": "src/app.js",
        "old_string": "line 31\n", "new_string": "line thirty-one\n",
    }))
    text = r.content[0].text
    assert text.startswith("Edited src/app.js: 1 replacement, lines 31-31.")
    assert "31\tline thirty-one" in text
    assert "line 1\n" not in text
    assert c.files["src/app.js"].decode() == src.replace("line 31\n", "line thirty-one\n")

    # Re-sending the whole file is refused; overwrite=true is the opt-out.
    whole = c.files["src/app.js"].decode().replace("line 40\n", "line forty\n")
    r = run(mcp.call_tool("write_files", {
        "session_id": "sess1", "files": {"src/app.js": whole},
    }))
    assert "rewrite of existing file" in r.content[0].text
    assert "line 40" in c.files["src/app.js"].decode()
    run(mcp.call_tool("write_files", {
        "session_id": "sess1", "files": {"src/app.js": whole}, "overwrite": True,
    }))
    assert "line forty" in c.files["src/app.js"].decode()


def test_mcp_tool_renders_payload_too_large(app_module):
    big = "x" * (constants.MAX_FILE_BYTES + 1)
    result = run(app_module._mcp.call_tool(
        "write_files", {"session_id": "sess1", "files": {"big.txt": big}},
    ))
    text = result.content[0].text
    assert text.startswith("failed: payload too large")
    assert "Individual file cap" in text
