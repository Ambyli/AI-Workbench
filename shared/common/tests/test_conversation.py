"""common.conversation — message text, exchanges, called tools and the
rendered transcript of a chat-completions history. Pure; no I/O.

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_conversation.py -q
"""

from __future__ import annotations

import copy

import pytest

from common.conversation import (
    called_tool_names,
    message_text,
    render_transcript,
    split_exchanges,
    user_message_count,
)


@pytest.mark.parametrize("content,expected", [
    ("plain", "plain"),
    ([{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}}, {"type": "text", "text": "b"}], "a\nb"),
    ([{"type": "text", "text": 3}, {"type": "text"}, "x", None], ""),
    (None, ""),
    (42, ""),
    ({"type": "text", "text": "a dict is not a part list"}, ""),
])
def test_message_text(content, expected):
    assert message_text(content) == expected


def test_split_exchanges_groups_rounds_under_their_user_message():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "before any user message: no exchange"},
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "run"}}]},
        {"role": "tool", "tool_call_id": "c", "content": "ok"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": [{"type": "text", "text": "and"}, {"type": "text", "text": "this"}]},
    ]
    ex = split_exchanges(msgs)
    assert [text for text, _ in ex] == ["build it", "and\nthis"]
    assert [m["role"] for m in ex[0][1]] == ["assistant", "tool", "assistant"]
    assert ex[1][1] == []  # the current exchange: no rounds yet


def test_split_exchanges_tolerates_junk():
    assert split_exchanges(None) == [] and split_exchanges("x") == [] and split_exchanges({}) == []
    assert split_exchanges([None, 3, "x", {"role": "user", "content": None}]) == [("", [])]


def test_called_tool_names_covers_every_call_shape_most_recent_first():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "ok"},           # unnamed result: nothing
        {"role": "assistant", "function_call": {"name": "c", "arguments": "{}"}},
        {"role": "function", "name": "c", "content": "ok"},
        {"role": "assistant", "content": [{"type": "text", "text": "x"},
                                          {"type": "tool_use", "id": "t", "name": "d", "input": {}}]},
        {"role": "tool", "name": "e", "content": "ok"},
    ]
    assert called_tool_names(msgs) == ["e", "d", "c", "c", "b", "a"]


def test_called_tool_names_ignores_offers_names_and_junk():
    msgs = [None, "x", {"role": "assistant", "tool_calls": "nope"},
            {"role": "assistant", "tool_calls": [None, {"function": "nope"}, {"function": {"name": ""}}]},
            {"role": "assistant", "content": [{"type": "tool_use"}, {"type": "text", "name": "not-a-call"}]},
            {"role": "user", "name": "a-user-name", "content": "hi"},
            {"role": "system", "tools": [{"type": "function", "function": {"name": "offered"}}]}]
    assert called_tool_names(msgs) == []
    assert called_tool_names(None) == [] and called_tool_names({}) == []


# ── render_transcript / user_message_count ──────────────────────────────────
CHAT = [
    {"role": "system", "content": "SYSTEM PROMPT, never rendered"},
    {"role": "user", "content": "Write a script that lists files."},
    {"role": "assistant", "content": "Sure, running it.", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "write_files", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "exec", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "a", "name": "write_files", "content": "TOOL RESULT, never rendered"},
    {"role": "tool", "tool_call_id": "b", "content": "TOOL RESULT, never rendered"},
    {"role": "assistant", "content": "Done: it prints three files.  \r\n"},
    {"role": "user", "content": [{"type": "text", "text": "Now explain\r\nline two"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
    {"role": "assistant", "content": "It walks the tree."},
    {"role": "user", "content": "Thanks!"},
]

CHAT_TEXT = (
    "User: Write a script that lists files.\n"
    "Assistant: Sure, running it.\n"
    "[tool: write_files]\n"
    "[tool: exec]\n"
    "Assistant: Done: it prints three files.\n"
    "\n"
    "User: Now explain\nline two\n"
    "Assistant: It walks the tree.\n"
    "\n"
    "User: Thanks!"
)

# CHAT, continued: what a later request for the same chat carries.
LONGER = CHAT + [
    {"role": "assistant", "content": "You're welcome.", "tool_calls": [{"function": {"name": "late_tool"}}]},
    {"role": "tool", "name": "late_tool", "content": "x"},
    {"role": "user", "content": "One more thing " + "z" * 3000},
    {"role": "assistant", "content": "y" * 5000},
    {"role": "user", "content": "And the last."},
]


def test_render_transcript_format():
    assert render_transcript(CHAT, exchanges=4, max_chars=6000) == CHAT_TEXT


def test_render_transcript_upto_user_ends_with_that_user_message():
    assert render_transcript(CHAT, exchanges=4, max_chars=6000, upto_user=2) == (
        "User: Write a script that lists files.\n"
        "Assistant: Sure, running it.\n"
        "[tool: write_files]\n"
        "[tool: exec]\n"
        "Assistant: Done: it prints three files.\n"
        "\n"
        "User: Now explain\nline two"
    )
    # The current exchange is the user message alone: nothing after it.
    assert render_transcript(CHAT, exchanges=4, max_chars=6000, upto_user=1) == (
        "User: Write a script that lists files.")
    assert render_transcript(CHAT, exchanges=4, max_chars=6000, upto_user=3) == CHAT_TEXT


def test_render_transcript_window_is_the_last_n_exchanges():
    assert render_transcript(CHAT, exchanges=2, max_chars=6000) == (
        "User: Now explain\nline two\nAssistant: It walks the tree.\n\nUser: Thanks!")
    assert render_transcript(CHAT, exchanges=1, max_chars=6000) == "User: Thanks!"


@pytest.mark.parametrize("exchanges", [1, 2, 3, 4, 8])
@pytest.mark.parametrize("max_chars", [1, 15, 60, 120, 400, 6000])
def test_render_transcript_of_a_prefix_never_changes_when_the_chat_continues(exchanges, max_chars):
    for k in range(1, user_message_count(CHAT) + 1):
        assert (render_transcript(LONGER, exchanges=exchanges, max_chars=max_chars, upto_user=k)
                == render_transcript(CHAT, exchanges=exchanges, max_chars=max_chars, upto_user=k))
    assert (render_transcript(CHAT, exchanges=exchanges, max_chars=max_chars)
            == render_transcript(LONGER, exchanges=exchanges, max_chars=max_chars, upto_user=3))


def test_render_transcript_is_pure_and_tracks_edits():
    before = copy.deepcopy(LONGER)
    first = render_transcript(LONGER, exchanges=4, max_chars=6000, upto_user=4)
    assert render_transcript(copy.deepcopy(LONGER), exchanges=4, max_chars=6000, upto_user=4) == first
    assert LONGER == before  # input untouched
    edited = copy.deepcopy(LONGER)
    edited[1]["content"] = "Write a script that lists folders."
    assert render_transcript(edited, exchanges=4, max_chars=6000, upto_user=4) != first


def test_render_transcript_tool_calls_in_call_order_and_no_results():
    msgs = [
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": [{"type": "text", "text": "Reading it."},
                                          {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}},
                                          {"type": "tool_use", "id": "t2", "name": "grep", "input": {}}]},
        # Anthropic results ride in a user message: a user message (an exchange) whose text is empty.
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "SECRET"},
                                     {"type": "tool_result", "tool_use_id": "t2",
                                      "content": [{"type": "text", "text": "SECRET"}]}]},
        {"role": "assistant", "content": None, "function_call": {"name": "legacy", "arguments": "{}"},
         "tool_calls": [{"function": {"name": "first"}}]},
        {"role": "function", "name": "legacy", "content": "SECRET"},
        {"role": "user", "content": "ok?"},
    ]
    out = render_transcript(msgs, exchanges=5, max_chars=6000)
    assert out == ("User: fix the bug\nAssistant: Reading it.\n[tool: read_file]\n[tool: grep]\n\n"
                   "User: \n[tool: first]\n[tool: legacy]\n\n"
                   "User: ok?")
    assert "SECRET" not in out
    assert user_message_count(msgs) == 3


def test_render_transcript_normalises_line_ends_and_trailing_whitespace():
    msgs = [{"role": "user", "content": "  a\rb\r\n\r\nc \t\n\n"},
            {"role": "assistant", "content": " \r\n "},           # only whitespace: no line
            {"role": "user", "content": "next"}]
    assert render_transcript(msgs, exchanges=2, max_chars=100) == "User:   a\nb\n\nc\n\nUser: next"


def test_render_transcript_cuts_the_latest_user_message_in_the_middle():
    msgs = [{"role": "user", "content": "old"}, {"role": "user", "content": "a" * 50 + "b" * 50}]
    out = render_transcript(msgs, exchanges=2, max_chars=40)
    assert out == "User: " + "a" * 12 + "\n[…]\n" + "b" * 17
    assert len(out) == 40
    assert render_transcript(msgs, exchanges=2, max_chars=3) == "Use"


def test_render_transcript_caps_older_messages_then_adds_whole_exchanges():
    long_reply = "x" * 1000 + "y" * 1000
    msgs = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": long_reply},
            {"role": "user", "content": "q2"}]
    # cap = max(200, 6000 // (2 * 4)) = 750: 373 head + " […] " + 372 tail.
    assert render_transcript(msgs, exchanges=4, max_chars=6000) == (
        "User: q1\nAssistant: " + "x" * 373 + " […] " + "y" * 372 + "\n\nUser: q2")
    # cap = max(200, 1000 // 8) = 200.
    out = render_transcript(msgs, exchanges=4, max_chars=1000)
    assert out == "User: q1\nAssistant: " + "x" * 98 + " […] " + "y" * 97 + "\n\nUser: q2"


def test_render_transcript_stops_at_the_first_exchange_that_does_not_fit():
    msgs = [{"role": "user", "content": "a"},                                   # would fit...
            {"role": "user", "content": "b" * 190}, {"role": "assistant", "content": "c" * 190},
            {"role": "user", "content": "now"}]
    # ...but the exchange before "now" (398 characters) does not, and no exchange is ever cut.
    assert render_transcript(msgs, exchanges=5, max_chars=300) == "User: now"
    # 409 characters with it, 418 with "a" too.
    assert render_transcript(msgs, exchanges=5, max_chars=412).startswith("User: " + "b" * 190)
    assert render_transcript(msgs, exchanges=5, max_chars=412).count("User: ") == 2


def test_render_transcript_never_exceeds_max_chars_and_keeps_the_latest_user_message():
    for max_chars in range(1, 900, 13):
        for exchanges in (1, 2, 3, 5):
            for k in range(1, user_message_count(LONGER) + 1):
                out = render_transcript(LONGER, exchanges=exchanges, max_chars=max_chars, upto_user=k)
                assert 1 <= len(out) <= max_chars
                latest = "User: " + split_exchanges(LONGER)[k - 1][0].replace("\r\n", "\n")
                if len(latest) <= max_chars:
                    assert out.endswith(latest)
                else:
                    assert len(out) == max_chars and latest.startswith(out[:max_chars // 3])


@pytest.mark.parametrize("upto", [0, -1, 4, 99])
def test_render_transcript_out_of_range_is_empty(upto):
    assert render_transcript(CHAT, exchanges=4, max_chars=6000, upto_user=upto) == ""


def test_render_transcript_without_user_messages_or_on_junk():
    assert render_transcript([{"role": "system", "content": "s"}, {"role": "assistant", "content": "a"}],
                             exchanges=4, max_chars=100) == ""
    for junk in (None, "x", {}, 3, []):
        assert render_transcript(junk, exchanges=4, max_chars=100) == ""
    msgs = [None, 3, "x", {"content": "no role"}, {"role": "assistant", "content": "before any user"},
            {"role": "user", "content": None},
            {"role": "assistant", "content": 5, "tool_calls": [None, {"function": {"name": ""}},
                                                               {"function": {"name": "ok"}}]},
            {"role": "tool", "name": "ok", "content": "r"}, {"role": "user", "content": "hi"}]
    assert render_transcript(msgs, exchanges=3, max_chars=1000) == "User: \n[tool: ok]\n\nUser: hi"


@pytest.mark.parametrize("kwargs", [{"exchanges": 0, "max_chars": 10}, {"exchanges": -1, "max_chars": 10},
                                    {"exchanges": 1, "max_chars": 0}, {"exchanges": 1, "max_chars": -5}])
def test_render_transcript_bad_arguments(kwargs):
    for messages in (CHAT, None):
        with pytest.raises(ValueError):
            render_transcript(messages, **kwargs)


def test_user_message_count():
    assert user_message_count(CHAT) == 3 and user_message_count(LONGER) == 5
    assert user_message_count([None, "user", {"role": "user"}, {"role": "USER"}, {"content": "x"}]) == 1
    assert user_message_count(None) == 0 and user_message_count({"role": "user"}) == 0
