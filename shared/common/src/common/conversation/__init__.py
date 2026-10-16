"""common.conversation — read an OpenAI-style chat-completions history.

A request's ``messages`` list is the whole conversation so far: system
prompts, every user message, every assistant round (text, tool calls) and
every tool result. These helpers answer the questions a router or a proxy
hook asks about it without caring which client produced it:

* :func:`message_text` — the text of one message's ``content`` (a string, or
  the ``text`` parts of a multimodal list, joined with newlines);
* :func:`split_exchanges` — the history as EXCHANGES, one per user message:
  the user's text plus every message after it up to the next user message
  (the model rounds and tool results it started);
* :func:`called_tool_names` — the tools the conversation actually CALLED,
  most recent first, from every shape a history carries a call in (OpenAI
  ``tool_calls`` / legacy ``function_call``, Anthropic ``tool_use`` blocks,
  named ``tool`` / ``function`` results). Offered tool definitions
  (``tools``) sit beside ``messages``, never inside, so they are never read.

Stdlib only, pure, never raises on malformed input (junk entries are
skipped). The LiteLLM hooks (``ai/litellm/hooks``) are the first consumer:
the code-tool remap reads exchanges and called tools.

Public API:
    message_text(content)       -> str
    split_exchanges(messages)   -> [(user text, [messages after it]), ...]
    called_tool_names(messages) -> [tool name, ...] most recent first
"""

from .history import called_tool_names, message_text, split_exchanges

__all__ = ["called_tool_names", "message_text", "split_exchanges"]
