"""common.conversation — message text, exchanges and called tools from a
chat-completions history. Pure; no I/O.

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_conversation.py -q
"""

from __future__ import annotations

import pytest

from common.conversation import called_tool_names, message_text, split_exchanges


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
