"""THE `auto` FOOTER. When -- and only when -- a client calls model ``auto``
(the semantic router), the assistant text gets a markdown footer naming the
backend that actually answered::

    <answer>

    ---
    *qwen3.8-solo · local-general*

with `` · overflow (local busy|local down)`` when a Claude deployment
answered (busy/down read from the overflow filter's backend state --
approximate), and `` · router bypassed`` when ``auto`` itself fell back to
local-private. The served deployment comes from the inner LiteLLM's response
headers, which the outer openai provider keeps as
``llm_provider-x-litellm-model-id`` / ``-model-name`` / ``-model-group`` in
``_hidden_params["additional_headers"]`` (core_helpers.py:318-319; streams:
streaming_handler.py:164-171); the model id is mapped back to its deployment
through the proxy's own router, because inner and outer are the same proxy. A
router bypass shows up as an outer ``data["deployment"]``
(common_request_processing.py:1707) whose model group is not ``auto``.

The three LiteLLM hooks that drive it live on hooks.handler.Handler (the
proxy's leaf-class ``__dict__`` rule); they call into AutoFooter. They skip
tool calls, ``response_format`` requests and Open WebUI background tasks
(title / tags / follow-ups / queries / autocomplete / emoji / legacy function
calling, which are detected by their default prompt templates on a
non-streaming request -- a streamed answer whose user message carries the
"### Task:" RAG template still gets its footer), strip a previous footer from
incoming assistant messages, and never throw. AUTO_FOOTER_ENABLED=false turns
the whole feature off. See ai/litellm/LITELLM.md > The `auto` footer.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..deployments import strip_provider
from ..log import ThrottledLog
from ..overflow.filter import OverflowFilter
from ..overflow.state import BUSY, DOWN
from .render import (
    AUTO_MODEL,
    FOOTER_SEP,
    choice_has_tool_calls,
    clean,
    deployment_fields,
    footer_text,
    get_field,
    headers_of,
    is_openwebui_task,
    set_field,
    strip_footer,
    strip_footers_from_messages,
)


def proxy_router() -> Any:
    """The running proxy's Router, without importing the proxy (tests, scripts)."""
    mod = sys.modules.get("litellm.proxy.proxy_server")
    return getattr(mod, "llm_router", None) if mod is not None else None


class AutoFooter:
    """When to add the footer, what it says, and the non-streaming edit.
    The streaming edit is footer/stream.py's FooterStream."""

    def __init__(
        self,
        enabled: bool,
        router_getter: Callable[[], Any],
        overflow: OverflowFilter,
        stats: Dict[str, int],
        log: ThrottledLog,
    ) -> None:
        self.enabled = enabled
        self.router_getter = router_getter
        self.overflow = overflow
        self.stats = stats
        self._log = log

    def wanted(self, data: Any) -> bool:
        if not self.enabled or not isinstance(data, dict) or data.get("model") != AUTO_MODEL:
            return False
        if data.get("response_format"):
            return False  # structured output: a footer would break the JSON
        return not is_openwebui_task(data)

    def strip_incoming(self, data: Any) -> bool:
        """On an `auto` request, strip footers we emitted from incoming
        assistant messages, in place, so the model never sees (and imitates)
        them and the router's signals never read them. True when something
        was stripped. Never raises."""
        try:
            if not self.enabled or not isinstance(data, dict) or data.get("model") != AUTO_MODEL:
                return False
            n = strip_footers_from_messages(data.get("messages"))
            if not n:
                return False
            self.stats["footers_stripped"] += n
            return True
        except Exception as exc:
            self.error("pre_call", exc)
            return False

    def decorate(self, data: Any, response: Any) -> Any:
        """Non-streaming: append the footer to each qualifying choice's message.
        Returns the response when it changed it, else None. Never raises."""
        try:
            if not self.wanted(data):
                return None
            choices = getattr(response, "choices", None)
            if not isinstance(choices, list) or not choices:
                return None  # not a chat completion (embeddings, responses API, ...)
            hidden = getattr(response, "_hidden_params", None)
            if isinstance(hidden, dict) and hidden.get("zeo_auto_footer"):
                return None  # already done -- never twice
            label = self.label(headers_of(response), data)
            if not label:
                return None
            footer = footer_text(label)
            edits: List[Tuple[Any, str]] = []
            for choice in choices:
                message = get_field(choice, "message")
                if message is None or choice_has_tool_calls(message):
                    continue
                content = get_field(message, "content")
                if not isinstance(content, str):
                    continue
                body = strip_footer(content)  # an imitated footer is replaced, not doubled
                if not body.strip():
                    continue
                edits.append((message, body + footer))
            if not edits:
                return None
            for message, new in edits:
                set_field(message, "content", new)
            if isinstance(hidden, dict):
                hidden["zeo_auto_footer"] = True
            self.stats["footers"] += 1
            return response
        except Exception as exc:
            self.error("success", exc)
            return None

    def label(self, headers: Dict[str, Any], data: Any) -> Optional[str]:
        """'qwen3.8-solo · local-general', or None when the backend is unknown.

        Inner LiteLLM (behind the router) -> llm_provider-x-litellm-model-id /
        -model-name / -model-group; the id is mapped through the proxy's own
        router because inner and outer are the same proxy. The OUTER deployment
        is data["deployment"] (common_request_processing.py:1707): when its
        group is not `auto`, `auto` fell back past the router to local-private.
        """
        headers = headers or {}
        outer_group, outer_model = deployment_fields(data.get("deployment") if isinstance(data, dict) else None)
        bare_group = headers.get("x-litellm-model-group")
        if not outer_group and isinstance(bare_group, str) and bare_group:
            outer_group = bare_group
        has_vsr = any(k.startswith("llm_provider-x-vsr-") for k in headers)

        chain: Optional[str] = None
        served: Optional[str] = None
        inner_id = headers.get("llm_provider-x-litellm-model-id")
        if inner_id:
            try:
                router = self.router_getter()
                dep = router.get_deployment(model_id=str(inner_id)) if router is not None else None
                chain, served = deployment_fields(dep)
            except Exception:
                chain = served = None
        if not served:
            served = headers.get("llm_provider-x-litellm-model-name") or None
        if not chain:
            chain = headers.get("llm_provider-x-litellm-model-group") or headers.get("llm_provider-x-vsr-selected-model") or None
        inner_known = bool(served or chain)

        bypassed = bool(outer_group) and outer_group != AUTO_MODEL
        if not bypassed and not has_vsr and not inner_known and outer_model and "vllm-sr/" not in outer_model:
            bypassed = True
        if bypassed:
            served, chain = outer_model, (outer_group if outer_group != AUTO_MODEL else None)

        name = clean(strip_provider(served if isinstance(served, str) else None))
        chain = clean(chain if isinstance(chain, str) else None)
        if not name or not chain:
            return None
        parts = [name, chain]
        if isinstance(served, str) and (served.startswith("anthropic/") or name.startswith("claude")):
            parts.append(self.overflow_tag(chain))
        if bypassed:
            parts.append("router bypassed")
        return FOOTER_SEP.join(parts)

    def overflow_tag(self, chain: str) -> str:
        """'overflow (local busy)' / '(local down)' from the overflow filter's
        CURRENT view of the chain's local backends -- an approximation of why
        Claude answered."""
        try:
            def find(name: str) -> Any:
                router = self.router_getter()
                return router.get_model_list(model_name=name) if router is not None else None

            verdicts = self.overflow.local_verdicts(chain, find)
            if verdicts and all(v == DOWN for v in verdicts):
                return "overflow (local down)"
            if any(v == BUSY for v in verdicts):
                return "overflow (local busy)"
        except Exception:
            pass
        return "overflow"

    def error(self, where: str, exc: BaseException) -> None:
        try:
            self.stats["footer_errors"] += 1
            self._log("auto", "footer", where, "auto footer (%s) skipped after an error: %r", where, exc)
        except Exception:
            pass
