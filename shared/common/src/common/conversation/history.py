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


def _name_of(obj: Any) -> Optional[str]:
    name = obj.get("name") if isinstance(obj, dict) else None
    return name if isinstance(name, str) and name else None


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
        if msg.get("role") in ("tool", "function"):
            result = _name_of(msg)
            if result:
                found.append(result)
        names += reversed(found)
    return names
