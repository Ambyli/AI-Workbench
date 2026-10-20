"""Chat-completions history helpers. See the package docstring."""

from __future__ import annotations

from typing import Any, List, Optional, Tuple


def message_text(content: Any) -> str:
    """The text of a chat message's content: a string, or the `text` parts of
    a multimodal list joined with newlines. Anything else is ""."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def split_exchanges(messages: Any) -> List[Tuple[str, List[dict]]]:
    """[(user text, the messages after it up to the next user message)], one
    per user message, oldest first. Anything before the first user message
    (system prompts, a compaction summary) belongs to no exchange."""
    out: List[Tuple[str, List[dict]]] = []
    if not isinstance(messages, list):
        return out
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            out.append((message_text(msg.get("content")), []))
        elif out:
            out[-1][1].append(msg)
    return out


def user_message_count(messages: Any) -> int:
    """How many entries of ``messages`` have role ``user`` — the number of
    exchanges, and the largest ``upto_user`` :func:`render_transcript` takes."""
    if not isinstance(messages, list):
        return 0
    return sum(1 for msg in messages if isinstance(msg, dict) and msg.get("role") == "user")


def _name_of(obj: Any) -> Optional[str]:
    name = obj.get("name") if isinstance(obj, dict) else None
    return name if isinstance(name, str) and name else None


def _calls_in(msg: dict) -> List[str]:
    """The tools one message calls, in call order: OpenAI ``tool_calls``,
    then legacy ``function_call``, then Anthropic ``tool_use`` blocks."""
    found: List[str] = []
    calls = msg.get("tool_calls")
    if isinstance(calls, list):
        found += [n for n in (_name_of(c.get("function")) for c in calls if isinstance(c, dict)) if n]
    legacy = _name_of(msg.get("function_call"))
    if legacy:
        found.append(legacy)
    content = msg.get("content")
    if isinstance(content, list):
        found += [n for n in (_name_of(b) for b in content
                              if isinstance(b, dict) and b.get("type") == "tool_use") if n]
    return found


def called_tool_names(messages: Any) -> List[str]:
    """Names of the tools the conversation has actually CALLED, most recent
    first. Reads every shape a chat-completions history carries a call in:
    an assistant's OpenAI ``tool_calls[].function.name`` (what Open WebUI
    replays) and legacy ``function_call.name``, Anthropic ``tool_use``
    content blocks, and a ``tool`` / ``function`` result message's ``name``.
    Offered tool definitions (``tools``) are deliberately not read."""
    names: List[str] = []
    if not isinstance(messages, list):
        return names
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        found = _calls_in(msg)
        if msg.get("role") in ("tool", "function"):
            result = _name_of(msg)
            if result:
                found.append(result)
        names += reversed(found)
    return names


# The markers a cut leaves where it took the middle out: the latest user
# line's on lines of their own, an older message's inline.
_LINE_CUT = "\n[…]\n"
_TEXT_CUT = " […] "


def _clean(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def _cut_middle(text: str, limit: int, marker: str) -> str:
    """``text`` if it fits in ``limit`` characters, else its head + ``marker``
    + its tail, exactly ``limit`` long (the head gets the odd character)."""
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return text[:limit]
    keep = limit - len(marker)
    tail = keep // 2
    return text[:keep - tail] + marker + text[len(text) - tail:]


def _exchange_block(user_text: str, rounds: List[dict], cap: int) -> str:
    """One complete older exchange, every message's text capped at ``cap``."""
    lines = ["User: " + _cut_middle(_clean(user_text), cap, _TEXT_CUT)]
    for msg in rounds:
        if msg.get("role") != "assistant":
            continue  # tool results, mid-conversation system notes, junk
        text = _cut_middle(_clean(message_text(msg.get("content"))), cap, _TEXT_CUT)
        if text:
            lines.append("Assistant: " + text)
        lines += [f"[tool: {name}]" for name in _calls_in(msg)]
    return "\n".join(lines)


def render_transcript(messages: Any, *, exchanges: int, max_chars: int,
                      upto_user: Optional[int] = None) -> str:
    """The conversation up to and including its ``upto_user``-th user message
    (1-based; None = the last one) as plain text for a model to classify —
    at most ``max_chars`` characters, at most ``exchanges`` exchanges.

    The last exchange is that user message alone (``User: <text>``); before
    it come up to ``exchanges - 1`` earlier COMPLETE exchanges (the split of
    :func:`split_exchanges`), each its ``User:`` line, an ``Assistant:``
    line per assistant message with text, and a ``[tool: <name>]`` line per
    tool it called (the calls :func:`called_tool_names` reads, in call
    order). Tool results, non-text parts and anything before the first user
    message are not rendered. Exchanges are separated by a blank line;
    ``\\r\\n`` / ``\\r`` become ``\\n`` and each message's trailing
    whitespace is dropped.

    Length: the latest user message is always there — its line cut in the
    middle (``\\n[…]\\n``) to exactly ``max_chars`` if it alone is longer.
    Each older message's text is first capped at
    ``max(200, max_chars // (2 * exchanges))`` (cut in the middle with
    `` […] ``); whole older exchanges are then added newest first while the
    result still fits, and the first that does not ends the walk.

    Pure and deterministic, and a prefix's transcript never changes when the
    conversation continues: route-policy hashes it to key a cached verdict.
    ``""`` when ``upto_user`` is out of range (or there is no user message);
    ``ValueError`` only for ``exchanges`` or ``max_chars`` below 1.
    """
    if exchanges < 1:
        raise ValueError(f"exchanges must be at least 1, got {exchanges!r}")
    if max_chars < 1:
        raise ValueError(f"max_chars must be at least 1, got {max_chars!r}")
    split = split_exchanges(messages)
    upto = len(split) if upto_user is None else upto_user
    if not 1 <= upto <= len(split):
        return ""
    out = _cut_middle("User: " + _clean(split[upto - 1][0]), max_chars, _LINE_CUT)
    cap = max(200, max_chars // (2 * exchanges))
    for user_text, rounds in reversed(split[max(0, upto - exchanges):upto - 1]):
        longer = _exchange_block(user_text, rounds, cap) + "\n\n" + out
        if len(longer) > max_chars:
            break
        out = longer
    return out
