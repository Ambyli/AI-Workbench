"""The `auto` footer's pure helpers: the footer text and the regex that finds
it, Open WebUI background-task detection, and tolerant readers for LiteLLM's
response / chunk / deployment objects (dicts or attribute objects alike).
Nothing here holds state or raises on malformed input."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from common.conversation import message_text

AUTO_MODEL = "auto"
FOOTER_SEP = " · "  # " · "

# Exactly the footer footer_text() emits: a blank line, a `---` rule, then
# one *italic* line of at least two " · "-separated parts, at the very end.
# ai/openwebui/patches/0004 carries a copy; unit-tests/openwebui pins the two.
FOOTER_RE = re.compile(
    r"(?:\r?\n){2,}---[ \t]*\r?\n\*[^*\r\n·]+(?: · [^*\r\n·]+)+\*[ \t]*(?:\r?\n[ \t]*)*\Z"
)

# Open WebUI v0.11.4 background tasks. Their payloads carry `metadata.task`,
# but routers/openai.py pops `metadata` before the request leaves Open WebUI
# and forwards no task header -- so the prompt text is the only signal. The
# default templates (backend/open_webui/config.py:2211-2441) for title, tags,
# image prompt, follow-ups, query and autocomplete all start "### Task:";
# emoji starts with the sentence below; legacy function calling
# (utils/middleware.py:1351-1373) sends a system message starting
# "Available Tools:". MOA (config.py:2443) is a user-visible answer and is
# deliberately NOT treated as a task.
#
# The prefixes only count on a NON-streaming request: every task above is sent
# with stream=False (routers/tasks.py), while the RAG template
# (DEFAULT_RAG_TEMPLATE) also starts "### Task:" and, with RAG_SYSTEM_CONTEXT
# unset, is PREPENDED to the user's own message (utils/middleware.py,
# add_or_update_user_message(..., append=False)) -- so a streamed chat answer
# that used knowledge retrieval would otherwise look like a task and lose its
# footer for the whole turn.
OWUI_TASK_PREFIXES = (
    "### Task:",
    "Your task is to reflect the speaker's likely facial expression",
)
OWUI_TOOLS_SYSTEM_PREFIX = "Available Tools:"


def footer_text(label: str) -> str:
    return "\n\n---\n*" + label + "*"


def strip_footer(text: Any) -> Any:
    """Remove trailing footers this module emitted; anything else is returned as is."""
    if not isinstance(text, str):
        return text
    for _ in range(5):
        new = FOOTER_RE.sub("", text)
        if new == text:
            break
        text = new
    return text


def strip_footers_from_messages(messages: Any) -> int:
    """Strip our footer from every assistant message, in place. Returns how many changed."""
    changed = 0
    if not isinstance(messages, list):
        return 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str):
            new = strip_footer(content)
            if new != content:
                m["content"] = new
                changed += 1
        elif isinstance(content, list):
            # The footer is at the end of the message: only the LAST text part.
            for p in reversed(content):
                if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                    new = strip_footer(p["text"])
                    if new != p["text"]:
                        p["text"] = new
                        changed += 1
                    break
    return changed


def is_openwebui_task(data: Any) -> bool:
    """True for an Open WebUI background task request (see OWUI_TASK_PREFIXES)."""
    if not isinstance(data, dict):
        return False
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    last_user: Optional[str] = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = message_text(m.get("content"))
        if m.get("role") == "system" and text.lstrip().startswith(OWUI_TOOLS_SYSTEM_PREFIX):
            return True
        if m.get("role") == "user":
            last_user = text
    # Open WebUI sends every task except MOA with stream=False; a streamed
    # request is a chat answer even when it starts "### Task:" (the RAG template).
    if last_user is None or data.get("stream"):
        return False
    text = last_user.lstrip()
    if text.startswith(OWUI_TASK_PREFIXES):
        return True
    # Admin-customised task templates usually keep the defaults' <chat_history> block.
    return "<chat_history>" in text and "</chat_history>" in text


# -- Tolerant readers ------------------------------------------------------------
def get_field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def set_field(obj: Any, key: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


def to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def deployment_fields(deployment: Any) -> Tuple[Optional[str], Optional[str]]:
    """(model_name, litellm_params.model) of a router Deployment or its dict form."""
    if deployment is None:
        return None, None
    group = get_field(deployment, "model_name")
    model = get_field(get_field(deployment, "litellm_params"), "model")
    return (group if isinstance(group, str) else None, model if isinstance(model, str) else None)


def headers_of(obj: Any) -> Dict[str, Any]:
    """Lower-cased _hidden_params["additional_headers"] of a response, stream wrapper or chunk."""
    hidden = getattr(obj, "_hidden_params", None)
    if hidden is None and isinstance(obj, dict):
        hidden = obj.get("_hidden_params")
    if not isinstance(hidden, dict):
        return {}
    headers = hidden.get("additional_headers")
    if not isinstance(headers, dict):
        return {}
    return {str(k).lower(): v for k, v in headers.items()}


def choice_has_tool_calls(obj: Any) -> bool:
    return bool(get_field(obj, "tool_calls")) or bool(get_field(obj, "function_call"))


def clean(value: Optional[str]) -> Optional[str]:
    """Keep a footer part on one line and out of FOOTER_RE's separators."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.replace("*", "").replace("·", "").split())
    return value or None
