"""bench.convert: Open WebUI 0.11.4's stored chat -> the messages /decide received.

Each test builds a fabricated stored branch (``bench_fixtures``) and pins the
OpenAI messages Open WebUI's rebuild produces for it -- tool rounds from
output items, the tool-image user message, attached-file blocks, the summary
checkpoint -- and that LiteLLM's ``auto`` footer is stripped with a regex that
stays identical to the hook's.
"""

from __future__ import annotations

import ast
import re

import pytest

from bench_fixtures import (FOOTER_SOURCE, Branch, out_call, out_reasoning, out_result, out_text)

from bench import convert


def replay(branch: Branch, **options) -> list:
    return convert.replay(branch.messages, convert.ReplayOptions(**options)).messages


def test_plain_chat_keeps_role_and_content_only():
    b = Branch()
    b.user("hello")
    b.assistant("hi there")
    b.user("how are you?")
    assert replay(b) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "how are you?"},
    ]


def test_output_items_become_tool_rounds():
    b = Branch()
    b.user("build a calculator")
    b.assistant("ignored when output converts", output=[
        out_text("Let me write it."),
        out_call("c1", "sandbox-mcp_run", '{"runtime": "static"}'),
        out_result("c1", "preview at /s/1"),
        out_text("Done."),
    ])
    b.user("thanks")
    assert replay(b) == [
        {"role": "user", "content": "build a calculator"},
        {"role": "assistant", "content": "Let me write it.", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "sandbox-mcp_run", "arguments": '{"runtime": "static"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "preview at /s/1"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "thanks"},
    ]


def test_calls_without_a_result_or_not_completed_are_dropped():
    b = Branch()
    b.user("go")
    b.assistant("x", output=[
        out_text("Trying."),
        out_call("c1", "run_command", status="in_progress"),
        out_result("c1"),
        out_call("c2", "kill_process"),          # no result
    ])
    b.user("next")
    msgs = replay(b)
    assert msgs[1] == {"role": "assistant", "content": "Trying."}
    assert not any(m.get("role") == "tool" for m in msgs)


def test_non_dict_arguments_are_serialised():
    msgs = convert.output_to_messages([out_call("c1", "f", status="completed") | {"arguments": {"a": 1}},
                                       out_result("c1")])
    assert msgs[0]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'


def test_empty_output_falls_back_to_content_and_empty_both_is_skipped():
    b = Branch()
    b.user("one")
    b.assistant("from content", output=[out_reasoning()])
    b.user("two")
    b.assistant("", output=[out_reasoning()])
    b.user("three")
    assert [m["role"] for m in replay(b)] == ["user", "assistant", "user", "user"]
    assert replay(b)[1] == {"role": "assistant", "content": "from content"}


def test_errored_empty_assistant_is_dropped_but_errored_with_text_is_kept():
    b = Branch()
    b.user("one")
    b.assistant("", error=True)
    b.user("two")
    b.assistant("partial answer", error=True)
    b.user("three")
    assert [m.get("content") for m in replay(b)] == ["one", "two", "partial answer", "three"]


def test_tool_images_add_a_user_message():
    b = Branch()
    b.user("take a screenshot")
    b.assistant("x", output=[
        out_call("c1", "sandbox-mcp_preview"),
        out_result("c1", "shot", images=["data:image/png;base64,[omitted: 900 chars]"]),
        out_text("Here it is."),
    ])
    b.user("nice")
    msgs = replay(b)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "user", "assistant", "user"]
    assert msgs[2] == {"role": "tool", "tool_call_id": "c1", "content": "shot"}
    assert msgs[3]["content"][0] == {"type": "text", "text": convert.TOOL_IMAGES_TEXT}
    assert msgs[3]["content"][1]["type"] == "image_url"


def test_code_interpreter_items_are_inlined():
    msgs = convert.output_to_messages([
        {"type": "open_webui:code_interpreter", "code": "print(1)", "output": {"stdout": "1"}},
        {"type": "open_webui:status", "data": "ignored"},
    ])
    assert msgs == [{"role": "assistant", "content":
                     "<code_interpreter>\nprint(1)\n</code_interpreter>\n"
                     "<code_interpreter_output>\n1\n</code_interpreter_output>"}]


def test_image_files_become_image_parts():
    b = Branch()
    b.user("what is in this photo?", files=[{"type": "image", "url": "/api/v1/files/f1/content"}])
    msg = replay(b, file_context=False)[0]
    assert msg["content"] == [{"type": "text", "text": "what is in this photo?"},
                              {"type": "image_url", "image_url": {"url": convert.IMAGE_PLACEHOLDER}}]


def test_attached_files_block_is_prepended_in_native_mode_only():
    b = Branch()
    b.user("summarise it", files=[
        {"type": "file", "id": "f1", "url": "/api/v1/files/f1", "name": "report.json",
         "content_type": "application/json"},
        {"type": "file", "id": "f2", "url": "data:text/plain;base64,[omitted: 300 chars]", "name": "inline.txt"},
        {"type": "chat", "id": "chat-9"},
    ])
    native = replay(b)[0]["content"]
    assert native == ('<attached_files>\n'
                      '<file type="file" id="f1" url="/api/v1/files/f1" content_type="application/json" '
                      'name="report.json"/>\n'
                      '<file type="chat" id="chat-9"/>\n'
                      '</attached_files>\n\nsummarise it')
    assert replay(b, file_context=False)[0]["content"] == "summarise it"
    legacy = convert.ReplayOptions.for_chat({"function_calling": "legacy"})
    assert legacy.file_context is False and convert.ReplayOptions.for_chat({}).file_context is True


def test_attached_files_pair_by_position_like_open_webui():
    """0.11.4 zips the payload's user messages with the stored ones, so a
    tool-image user message shifts the blocks onto the wrong messages."""
    b = Branch()
    b.user("first")
    b.assistant("x", output=[out_call("c1", "t"), out_result("c1", "img", images=["u"])])
    b.user("second", files=[{"type": "file", "id": "f1", "url": "/f/1", "name": "a.py"}])
    msgs = replay(b)
    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 3
    # The stored second user message's file lands on the synthetic image message.
    assert isinstance(users[1]["content"], list) and "<attached_files>" in users[1]["content"][0]["text"]
    assert users[2]["content"] == "second"


def test_summary_checkpoint_drops_earlier_messages_when_compaction_is_on():
    b = Branch()
    b.user("one")
    b.assistant("a")
    b.user("two", context_summary=True)
    b.assistant("b")
    b.user("three")
    assert [m["content"] for m in replay(b)] == ["one", "a", "two", "b", "three"]
    result = convert.replay(b.messages, convert.ReplayOptions(context_compaction=True))
    assert result.compacted
    assert result.messages == [{"role": "system", "content": convert.SUMMARY_SYSTEM},
                               {"role": "user", "content": "two"}, {"role": "assistant", "content": "b"},
                               {"role": "user", "content": "three"}]


def test_auto_footer_is_stripped_from_assistant_messages_only():
    b = Branch()
    b.user("hi\n\n---\n*not · a footer*")
    b.assistant("Hello!\n\n---\n*qwen3.8-solo · local-general*")
    b.user("again")
    b.assistant("x", output=[out_text("Sure.\n\n---\n*claude-sonnet-5 · local-code · overflow (local busy)*\n")])
    b.user("last")
    msgs = replay(b)
    assert msgs[0]["content"] == "hi\n\n---\n*not · a footer*"
    assert msgs[1]["content"] == "Hello!"
    assert msgs[3]["content"] == "Sure."


def test_footer_regex_is_the_hooks_own():
    """The bench copies FOOTER_RE from ai/litellm/hooks/footer/render.py (it
    cannot import the hooks package); this fails when the two drift."""
    tree = ast.parse(FOOTER_SOURCE.read_text(encoding="utf-8"))
    found = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "FOOTER_RE" for t in node.targets):
            call = node.value
            assert isinstance(call, ast.Call) and len(call.args) == 1 and not call.keywords, \
                "FOOTER_RE is no longer a one-argument re.compile(...): update bench/convert.py"
            found = ast.literal_eval(call.args[0])
    assert found is not None, "FOOTER_RE not found in render.py"
    assert convert.FOOTER_RE.pattern == found
    assert convert.FOOTER_RE.flags == re.compile(found).flags


def test_strip_footers_handles_list_content_last_text_part():
    msgs = convert.strip_footers([{"role": "assistant", "content": [
        {"type": "text", "text": "a\n\n---\n*m · c*"}, {"type": "image_url", "image_url": {"url": "u"}},
        {"type": "text", "text": "b\n\n---\n*m · c*"}]}])
    assert msgs[0]["content"][0]["text"] == "a\n\n---\n*m · c*"
    assert msgs[0]["content"][2]["text"] == "b"


@pytest.mark.parametrize("value, expected", [
    (1_760_000_000, 1_760_000_000.0),
    (1_760_000_000_123, 1_760_000_000.123),
    (None, None),
    ("1760000000", None),
    (True, None),
])
def test_normalize_timestamp(value, expected):
    got = convert.normalize_timestamp(value)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_replay_answer_reads_the_answers_calls():
    b = Branch()
    b.user("go")
    a = b.assistant("x", output=[out_call("c1", "sandbox-mcp_exec"), out_result("c1")])
    msgs = convert.replay_answer([a])
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "sandbox-mcp_exec"
    assert msgs[1]["role"] == "tool"
