"""Open WebUI's stored chat -> the OpenAI messages ``/decide`` received.

A decision point is only worth measuring if it shows the classifier what
route-policy's ``/decide`` saw. For a saved chat, Open WebUI 0.11.4 does NOT
send the browser's copy of the conversation: it rebuilds the history from
its database on every completion request (``utils/middleware.py::
process_chat_payload``, "DB preserves structured 'output' items which the
frontend strips"). :func:`replay` repeats that rebuild on an exported branch
(``export_chats.py``), step for step, with the 0.11.4 function each step
ports:

1. ``load_messages_from_db`` -- the chain from the root to the user message
   being answered; an assistant message with an error and neither content
   nor output is dropped.
2. Image files on a user message become ``image_url`` parts after a ``text``
   part (``process_chat_payload``, "Inject image files into content"); here
   each image's URL is a placeholder, as LiteLLM's hook sends /decide.
3. With context compaction on: the latest stored summary checkpoint drops
   every message before it and adds a ``[CONVERSATION SUMMARY]`` system
   message (``context_compaction._apply_latest_summary_checkpoint``); the
   summary text is not exported, so the system message carries a
   placeholder.
4. ``process_messages_with_output`` -> ``convert_output_to_messages(raw=True,
   reasoning_format=None, flatten_tool_images=True)``: an assistant message
   with ``output`` items becomes the assistant rounds they record -- text,
   ``tool_calls`` (only calls that completed AND have a result), ``tool``
   results, and, for images a tool returned, an extra USER message ("Here
   are the images from the tool results above...") -- then
   ``reconcile_tool_pairs``; any other message keeps ``role`` + ``content``.
   ``reasoning_format`` is None for models served through LiteLLM
   (``get_reasoning_format``: only Ollama and llama.cpp connections get
   one), so reasoning is never replayed.
5. ``sanitize_tool_pairs``.
6. ``add_file_context`` (native function calling, the default): each user
   message gets an ``<attached_files>`` block naming its attached files that
   have a non-``data:`` URL (or are chat references), paired with the stored
   user messages by position -- including 0.11.4's own misalignment when a
   tool-image message (step 4) or compaction (step 3) shifts the payload's
   user messages. Skipped when the chat's ``params.function_calling`` is
   ``legacy``.
7. LiteLLM's pre-call hook, before it calls ``/decide``: the ``auto``
   footer (``\\n\\n---\\n*<model> · <chain>*``) LiteLLM appended to earlier
   answers -- and Open WebUI stored -- is stripped from every assistant
   message (``ai/litellm/hooks/footer/render.py::strip_footers_from_messages``,
   run by ``AutoFooter.strip_incoming``), so neither the rules nor the
   classifier's transcript ever read it. :data:`FOOTER_RE` is a COPY of that
   file's ``FOOTER_RE`` (the bench cannot import the hooks package);
   ``unit-tests/route-policy/bench/test_bench_convert.py`` reads the
   original's source and fails when the two differ.

**Not reproduced** (request-time state the database does not keep): the
system prompt (no evaluator and no transcript reads it), retrieval results
Open WebUI writes into the latest user message for attached files / web
search (``apply_source_context_to_messages`` with the RAG template -- a
point whose answer cited sources is flagged ``answer_sources`` by
``points.py``), a guided-regeneration prompt, and per-model capability
switches that turn native tools off.

Pure and stdlib-only; the input is ``export_chats.py``'s message records.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

TOOL_IMAGES_TEXT = "Here are the images from the tool results above. Please analyze them."
SUMMARY_SYSTEM = "[CONVERSATION SUMMARY]\n[summary text not exported]"
IMAGE_PLACEHOLDER = "[omitted: image]"

# COPY of FOOTER_RE in ai/litellm/hooks/footer/render.py -- the `auto`
# footer LiteLLM appends to an answer and strips from incoming assistant
# messages before /decide. Keep the two identical (test_bench_convert.py
# compares them).
FOOTER_RE = re.compile(
    r"(?:\r?\n){2,}---[ \t]*\r?\n\*[^*\r\n·]+(?: · [^*\r\n·]+)+\*[ \t]*(?:\r?\n[ \t]*)*\Z"
)
# render.py's strip_footer: at most this many stacked footers come off.
FOOTER_STRIP_ROUNDS = 5


@dataclass(frozen=True)
class ReplayOptions:
    # Open WebUI's context compaction was on (export_chats: "context_compaction").
    context_compaction: bool = False
    # add_file_context applies: native function calling (not "legacy").
    file_context: bool = True

    @classmethod
    def for_chat(cls, chat: Dict[str, Any]) -> "ReplayOptions":
        return cls(context_compaction=bool(chat.get("context_compaction")),
                   file_context=chat.get("function_calling") != "legacy")


@dataclass(frozen=True)
class Replay:
    messages: List[Dict[str, Any]]
    compacted: bool          # a summary checkpoint dropped earlier messages


def normalize_timestamp(value: Any) -> Optional[float]:
    """Seconds since the epoch; milliseconds are converted (as Open WebUI's
    ``_normalize_timestamp``). None when missing or not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value / 1000.0 if value > 10_000_000_000 else float(value)


# -- 1. load_messages_from_db ------------------------------------------------------


def drops_on_load(msg: Dict[str, Any]) -> bool:
    """An assistant message with an error and neither content nor output."""
    return msg.get("role") == "assistant" and bool(msg.get("error")) \
        and not msg.get("content") and not msg.get("output")


def load(stored: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Working copies of the stored messages Open WebUI replays."""
    return [{"role": m.get("role"), "content": m.get("content"), "output": m.get("output"),
             "files": m.get("files") or [], "context_summary": bool(m.get("context_summary"))}
            for m in stored if isinstance(m, dict) and not drops_on_load(m)]


# -- 2. image files -> image_url parts ---------------------------------------------


def is_raster_image(content_type: Any) -> bool:
    base = (content_type or "").split(";")[0].strip().lower() if isinstance(content_type, str) else ""
    return base.startswith("image/") and base != "image/svg+xml"


def inject_images(messages: List[Dict[str, Any]]) -> None:
    for msg in messages:
        images = [f for f in msg.get("files") or []
                  if isinstance(f, dict) and (f.get("type") == "image" or is_raster_image(f.get("content_type")))]
        content = msg.get("content")
        if msg.get("role") == "user" and images and isinstance(content, str):
            msg["content"] = [{"type": "text", "text": content}] + [
                {"type": "image_url", "image_url": {"url": IMAGE_PLACEHOLDER}} for f in images if f.get("url")]
        msg.pop("files", None)


# -- 3. the summary checkpoint -----------------------------------------------------


def apply_summary_checkpoint(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    last = None
    for i, msg in enumerate(messages):
        if msg.get("context_summary"):
            last = i
    if last is None:
        return messages, False
    return [{"role": "system", "content": SUMMARY_SYSTEM}] + messages[last:], True


# -- 4. output items -> chat-completions messages -----------------------------------


def _text_of_parts(parts: Any, part_type: str) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == part_type)


def reconcile_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop tool results no call claims, and calls no result answers."""
    completed = {m.get("tool_call_id") for m in messages if m.get("role") == "tool" and m.get("tool_call_id")}
    requested = {c.get("id") for m in messages if m.get("role") == "assistant"
                 for c in m.get("tool_calls") or () if c.get("id")}
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool" and msg.get("tool_call_id") not in requested:
            continue
        if role != "assistant" or not msg.get("tool_calls"):
            out.append(msg)
            continue
        valid = [c for c in msg["tool_calls"] if c.get("id") in completed]
        if valid:
            out.append({**msg, "tool_calls": valid})
            continue
        content = msg.get("content") or ""
        if (content.strip() if isinstance(content, str) else content) or msg.get("reasoning_content"):
            out.append({k: v for k, v in msg.items() if k != "tool_calls"})
    return out


def output_to_messages(output: Any) -> List[Dict[str, Any]]:
    """``convert_output_to_messages(output, raw=True, reasoning_format=None,
    flatten_tool_images=True)`` + its ``reconcile_tool_pairs``."""
    if not output or not isinstance(output, list):
        return []
    items = [i for i in output if isinstance(i, dict)]
    completed_ids = {i.get("call_id") for i in items if i.get("type") == "function_call" and i.get("call_id")
                     and i.get("status") in {"completed", "failed", "rejected"}}
    result_ids = {i.get("call_id") for i in items if i.get("type") == "function_call_output" and i.get("call_id")}
    call_ids = completed_ids & result_ids

    messages: List[Dict[str, Any]] = []
    pending_calls: List[Dict[str, Any]] = []
    pending_content: List[str] = []
    pending_images: List[str] = []
    pending_outputs: List[Dict[str, Any]] = []

    def flush_pending() -> None:
        nonlocal pending_calls, pending_content
        if not pending_content and not pending_calls:
            return
        msg: Dict[str, Any] = {"role": "assistant", "content": "\n".join(pending_content) if pending_content else ""}
        if pending_calls:
            msg["tool_calls"] = pending_calls
        messages.append(msg)
        pending_calls, pending_content = [], []

    def flush_images() -> None:
        nonlocal pending_images
        if not pending_images:
            return
        messages.append({"role": "user", "content": [{"type": "text", "text": TOOL_IMAGES_TEXT}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in pending_images]})
        pending_images = []

    def flush_outputs() -> None:
        nonlocal pending_outputs
        if not pending_outputs:
            return
        flush_pending()
        for item in pending_outputs:
            content = ""
            for part in item.get("output") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "input_text":
                    text = part.get("text", "")
                    content += text if isinstance(text, str) else str(text)
                elif part.get("type") == "input_image" and part.get("image_url"):
                    pending_images.append(part["image_url"])
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": content})
        pending_outputs = []

    for item in items:
        kind = item.get("type", "")
        if kind not in ("function_call", "function_call_output"):
            flush_outputs()
            flush_images()
        if kind == "message":
            text = _text_of_parts(item.get("content"), "output_text")
            if text:
                pending_content.append(text)
        elif kind == "function_call":
            if item.get("call_id") not in call_ids:
                continue
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments)
            pending_calls.append({"id": item.get("call_id", ""), "type": "function",
                                  "function": {"name": item.get("name", ""), "arguments": arguments}})
        elif kind == "function_call_output":
            if item.get("call_id") in call_ids:
                pending_outputs.append(item)
        elif kind == "open_webui:code_interpreter":
            code, result = item.get("code", ""), item.get("output", "")
            if code:
                pending_content.append(f"<code_interpreter>\n{code}\n</code_interpreter>")
            if result:
                text = (result.get("stdout", "") or result.get("result", "")) if isinstance(result, dict) else str(result)
                if text:
                    pending_content.append(f"<code_interpreter_output>\n{text}\n</code_interpreter_output>")
        # reasoning (reasoning_format None, nothing replayed) and every other
        # open_webui:* extension item: skipped.

    flush_outputs()
    flush_images()
    flush_pending()
    return reconcile_tool_pairs(messages)


def process_messages_with_output(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    processed: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("output"):
            converted = output_to_messages(msg["output"])
            if converted:
                processed.extend(converted)
                continue
            if not msg.get("content"):
                continue
        processed.append({"role": msg.get("role"), "content": msg.get("content")})
    return processed


# -- 5. sanitize_tool_pairs ---------------------------------------------------------


def sanitize_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = {m.get("tool_call_id") for m in messages if m.get("role") == "tool" and m.get("tool_call_id")}
    calls = {c.get("id") for m in messages if m.get("role") == "assistant"
             for c in m.get("tool_calls") or [] if c.get("id")}
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            kept = [c for c in msg.get("tool_calls") or [] if c.get("id") in results]
            if kept:
                out.append({**msg, "tool_calls": kept})
            else:
                clean = {k: v for k, v in msg.items() if k not in ("tool_calls", "reasoning_items")}
                if clean.get("content"):
                    out.append(clean)
        elif msg.get("role") != "tool" or msg.get("tool_call_id") in calls:
            out.append(msg)
    return out


# -- 6. add_file_context ----------------------------------------------------------


def file_tag(f: Dict[str, Any]) -> str:
    attrs = f'type="{f.get("type", "file")}" id="{f.get("id") or f.get("url")}"'
    if f.get("url"):
        attrs += f' url="{f["url"]}"'
    if f.get("content_type"):
        attrs += f' content_type="{f["content_type"]}"'
    if f.get("name"):
        attrs += f' name="{f["name"]}"'
    return f"<file {attrs}/>"


def add_file_context(messages: List[Dict[str, Any]], stored: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = [dict(m) for m in messages]
    users = [m for m in out if m.get("role") == "user"]
    stored_users = [m for m in stored if isinstance(m, dict) and m.get("role") == "user"]
    for msg, stored_msg in zip(users, stored_users):
        attached = [f for f in stored_msg.get("files") or [] if isinstance(f, dict) and (
            (f.get("url") and not str(f.get("url")).startswith("data:"))
            or (f.get("type") == "chat" and f.get("id")))]
        if not attached:
            continue
        block = "<attached_files>\n" + "\n".join(file_tag(f) for f in attached) + "\n</attached_files>\n\n"
        content = msg.get("content", "")
        if isinstance(content, list):
            msg["content"] = [{"type": "text", "text": block}] + content
        else:
            msg["content"] = block + (content if isinstance(content, str) else "")
    return out


# -- 7. LiteLLM strips its `auto` footer ---------------------------------------------


def strip_footer(text: Any) -> Any:
    """render.py's strip_footer: trailing footers off, anything else as is."""
    if not isinstance(text, str):
        return text
    for _ in range(FOOTER_STRIP_ROUNDS):
        new = FOOTER_RE.sub("", text)
        if new == text:
            break
        text = new
    return text


def strip_footers(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """render.py's strip_footers_from_messages, on copies: every assistant
    message's string content, or the LAST text part of a list content."""
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str):
                msg = {**msg, "content": strip_footer(content)}
            elif isinstance(content, list):
                parts = list(content)
                for i in range(len(parts) - 1, -1, -1):
                    p = parts[i]
                    if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                        parts[i] = {**p, "text": strip_footer(p["text"])}
                        break
                msg = {**msg, "content": parts}
        out.append(msg)
    return out


# -- the whole rebuild -------------------------------------------------------------


def replay(stored: Sequence[Dict[str, Any]], options: ReplayOptions = ReplayOptions()) -> Replay:
    """The messages ``/decide`` received for a saved chat whose stored branch
    prefix is ``stored`` (root first, ending with the user message being
    answered): what Open WebUI 0.11.4 sent, after LiteLLM's footer strip."""
    messages = load(stored)
    inject_images(messages)
    compacted = False
    if options.context_compaction:
        messages, compacted = apply_summary_checkpoint(messages)
    messages = sanitize_tool_pairs(process_messages_with_output(messages))
    if options.file_context:
        messages = add_file_context(messages, stored)
    return Replay(strip_footers(messages), compacted)


def replay_answer(stored_answer: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The messages the stored answer to a user message adds to the next
    request: its assistant rounds and tool results (steps 1, 4, 5)."""
    messages = load([m for m in stored_answer if isinstance(m, dict) and m.get("role") != "user"])
    for msg in messages:
        msg.pop("files", None)
    return sanitize_tool_pairs(process_messages_with_output(messages))
