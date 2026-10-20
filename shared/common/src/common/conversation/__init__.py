"""common.conversation — read an OpenAI-style chat-completions history.

A request's ``messages`` list is the whole conversation so far: system
prompts, every user message, every assistant round (text, tool calls) and
every tool result. These helpers answer the questions a routing engine or a
proxy hook asks about it without caring which client produced it:

* :func:`message_text` — the text of one message's ``content`` (a string, or
  the ``text`` parts of a multimodal list, joined with newlines);
* :func:`split_exchanges` — the history as EXCHANGES, one per user message:
  the user's text plus every message after it up to the next user message
  (the model rounds and tool results it started);
* :func:`user_text` / :func:`strip_openwebui_rag` — what the USER wrote in a
  user message. On a request that carries retrieved sources (attached
  files, the pages a web search or fetch tool returned) Open WebUI puts
  its RAG instruction block ("### Task: Respond to the user query using
  the provided context ... <context>...</context>") in front of the
  user's words for that one request; these return the words alone, so a
  keyword rule never reads Open WebUI's instructions or a fetched page as
  the user's request. :func:`split_exchanges` (and so
  :func:`render_transcript`) use it; :func:`message_text` does not;
* :func:`called_tool_names` — the tools the conversation actually CALLED,
  most recent first, from every shape a history carries a call in (OpenAI
  ``tool_calls`` / legacy ``function_call``, Anthropic ``tool_use`` blocks,
  named ``tool`` / ``function`` results). Offered tool definitions
  (``tools``) sit beside ``messages``, never inside, so they are never read;
* :func:`user_message_count` — how many user messages (exchanges) there are;
* :func:`render_transcript` — the conversation up to its k-th user message
  as ``User:`` / ``Assistant:`` / ``[tool: <name>]`` text, the last N
  exchanges within a character budget. Deterministic, and a prefix renders
  the same however the conversation continues — the property a verdict
  cache keyed by its hash relies on.

Stdlib only, pure, never raises on malformed input (junk entries are
skipped; ``render_transcript`` raises ``ValueError`` only for a bad
argument). Consumers: route-policy (``ai/route-policy``: the ``tool_calls``
and ``cooldown`` evaluators read exchanges and called tools; the
``llm_classifier`` evaluator sends ``render_transcript`` to the document
classifier and keys its verdicts by the transcript's hash, and the
benchmark under ``bench/`` renders its decision points with the same
function, so it scores exactly what the evaluator would), and the LiteLLM
hooks (``ai/litellm/hooks``: the ``auto`` footer's background-task detection
reads ``message_text``). The hooks get it as a read-only bind mount, not an
install, which is why it must stay stdlib-only.

Public API:
    message_text(content)        -> str
    user_text(content)           -> str   (message_text without Open WebUI's RAG block)
    strip_openwebui_rag(text)    -> str
    split_exchanges(messages)    -> [(user text, [messages after it]), ...]
    called_tool_names(messages)  -> [tool name, ...] most recent first
    user_message_count(messages) -> int
    render_transcript(messages, *, exchanges, max_chars, upto_user=None) -> str
"""

from .history import (
    called_tool_names,
    message_text,
    render_transcript,
    split_exchanges,
    strip_openwebui_rag,
    user_message_count,
    user_text,
)

__all__ = ["called_tool_names", "message_text", "render_transcript", "split_exchanges",
           "strip_openwebui_rag", "user_message_count", "user_text"]
