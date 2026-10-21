"""Chat-completions history helpers. See the package docstring."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def message_text(content: Any) -> str:
    """The text of a chat message's content: a string, or the `text` parts of
    a multimodal list joined with newlines. Anything else is ""."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


# Open WebUI (v0.11.4, utils/middleware.py) answers from retrieved sources --
# a turn's attached files / knowledge on its first call, the pages a web
# search or fetch tool returned on every later tool round -- by REWRITING the
# latest user message for that one request (never saved): its RAG template
# with {{CONTEXT}} filled in, then "\n", then the user's own text
# (utils/misc.py update_message_content, append=False, per text part; with
# RAG_SYSTEM_CONTEXT set it goes into the system message instead). The
# default template (config.py DEFAULT_RAG_TEMPLATE) starts "### Task:" and
# ends "<context>\n{{CONTEXT}}\n</context>\n", so the user's text is what
# follows the LAST "</context>\n\n" -- the last, so a fetched page that
# carries the boundary itself cannot pass its own text off as the user's.
_OWUI_RAG_PREFIX = "### Task:"
_OWUI_RAG_OPEN = "<context>"
_OWUI_RAG_BOUNDARY = "</context>\n\n"


def strip_openwebui_rag(text: str) -> str:
    """The user's own words in a message Open WebUI wrapped in its RAG block
    (above); any other text is returned unchanged -- a customised template
    without that shape is left alone rather than guessed at."""
    if not isinstance(text, str) or not text.lstrip().startswith(_OWUI_RAG_PREFIX):
        return text
    head, boundary, tail = text.rpartition(_OWUI_RAG_BOUNDARY)
    if not boundary or _OWUI_RAG_OPEN not in head:
        return text
    return tail


def user_text(content: Any) -> str:
    """What the USER wrote in a user message's ``content``: :func:`message_text`
    with Open WebUI's RAG block removed from each text part
    (:func:`strip_openwebui_rag`)."""
    if isinstance(content, str):
        return strip_openwebui_rag(content)
    if isinstance(content, list):
        return "\n".join(strip_openwebui_rag(p["text"]) for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def user_texts(messages: Any) -> List[str]:
    """:func:`user_text` of every user message, oldest first -- exactly one
    entry per user message, so ``user_texts(m)[k - 1]`` is user message k's
    text and ``len(user_texts(m)) == user_message_count(m)``. A user message
    with no text (an image alone, an Anthropic message carrying only
    ``tool_result`` blocks) is ``""``, not skipped."""
    if not isinstance(messages, list):
        return []
    return [user_text(msg.get("content")) for msg in messages
            if isinstance(msg, dict) and msg.get("role") == "user"]


def _call_ids(msg: dict) -> List[Tuple[str, str]]:
    """(call id, tool name) for every call one message makes: OpenAI
    ``tool_calls[].id`` with its ``function.name``, Anthropic ``tool_use``
    blocks' ``id`` with their ``name``."""
    found: List[Tuple[str, str]] = []
    calls = msg.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"]:
                name = _name_of(call.get("function"))
                if name:
                    found.append((call["id"], name))
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                name = _name_of(block)
                if name and block["id"]:
                    found.append((block["id"], name))
    return found


def tool_results(messages: Any) -> List[Tuple[int, str, str]]:
    """Every tool RESULT in the conversation, oldest first, as ``(index,
    tool name, text)``:

    * ``index`` -- the position in ``messages`` of the message that carries
      it (an Anthropic user message carrying several ``tool_result`` blocks
      gives each the same index), so a caller can tell which results came
      after the latest user message;
    * ``tool name`` -- the result's own ``name`` when it has one (a legacy
      ``function`` message, a named ``tool`` message), else the name of the
      CALL it answers: an OpenAI ``tool`` message's ``tool_call_id`` matched
      to an assistant's ``tool_calls[].id``, an Anthropic ``tool_result``
      block's ``tool_use_id`` matched to a ``tool_use`` block's ``id``.
      ``""`` when neither resolves;
    * ``text`` -- :func:`message_text` of the result's content (a string, or
      the ``text`` parts / blocks of a list; images give ``""``). Tool output
      is not user text, so no RAG block is stripped.

    A result is matched to the most recent call with its id before it (to
    any call with that id when none comes before -- a history replayed out
    of order). Offered tool definitions are never read."""
    out: List[Tuple[int, str, str]] = []
    if not isinstance(messages, list):
        return out
    every: Optional[Dict[str, str]] = None
    seen: Dict[str, str] = {}

    def name_for(call_id: Any) -> str:
        nonlocal every
        if not isinstance(call_id, str) or not call_id:
            return ""
        if call_id in seen:
            return seen[call_id]
        if every is None:
            every = {}
            for msg in messages:
                if isinstance(msg, dict):
                    for cid, name in _call_ids(msg):
                        every.setdefault(cid, name)
        return every.get(call_id, "")

    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role in ("tool", "function"):
            name = _name_of(msg) or name_for(msg.get("tool_call_id"))
            out.append((index, name, message_text(msg.get("content"))))
        content = msg.get("content")
        if role == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    name = _name_of(block) or name_for(block.get("tool_use_id"))
                    out.append((index, name, message_text(block.get("content"))))
        for cid, name in _call_ids(msg):
            seen[cid] = name
    return out


def split_exchanges(messages: Any) -> List[Tuple[str, List[dict]]]:
    """[(user text, the messages after it up to the next user message)], one
    per user message, oldest first. The user text is :func:`user_text`: what
    the user typed, without the RAG block Open WebUI puts in front of it on a
    request that carries retrieved sources. Anything before the first user
    message (system prompts, a compaction summary) belongs to no exchange."""
    out: List[Tuple[str, List[dict]]] = []
    if not isinstance(messages, list):
        return out
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            out.append((user_text(msg.get("content")), []))
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
