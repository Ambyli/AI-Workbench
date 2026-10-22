"""THE `auto` FOOTER. When -- and only when -- a client calls model ``auto``
(routed by the route-policy engine), the assistant text gets a markdown footer
naming the backend that actually answered::

    <answer>

    ---
    *qwen3.8-solo · local-general*

with `` · overflow (local busy|local down)`` when a Claude deployment
answered (busy/down read from the overflow filter's backend state --
approximate), and `` · route-policy fallback`` when the engine did not route
the request and it went to local-private instead (the stash's source is
``fallback:<why>``; see hooks/routing/policy_client.py).

ONE PASS. The pre-call hook rewrites ``data["model"]`` from ``auto`` to the
chain, and the proxy serves that chain itself, so everything the footer
names comes from this one request: the deployment the proxy stores in
``data["deployment"]`` (common_request_processing.py:1707 at v1.95.0, resolved
from the response's model id BEFORE the success and streaming hooks run) --
its ``model_name`` is the chain, its ``litellm_params.model`` the deployment
(``openai/qwen3.8-solo``, ``anthropic/claude-sonnet-5``). The response's
``x-litellm-model-group`` (router.py:9388, in
``_hidden_params["additional_headers"]``) names the chain when the deployment
is missing; without a deployment there is no model name, and no footer.
"Was this request ``auto``?" is the route-policy stash (``zeo_route_policy``
in the proxy's metadata, routing/stash.py), because ``data["model"]`` is the
chain by then; a request still for ``auto`` (the hook never rewrote it -- the
``auto`` alias served it from local-private) counts too.

The three LiteLLM hooks that drive it live on hooks.handler.Handler (the
proxy's leaf-class ``__dict__`` rule); they call into AutoFooter. They skip
tool calls, ``response_format`` requests and Open WebUI background tasks
(title / tags / follow-ups / queries / autocomplete / emoji / legacy function
calling, which are detected by their default prompt templates on a
non-streaming request -- a streamed answer whose user message carries the
"### Task:" RAG template still gets its footer), strip a previous footer from
incoming assistant messages, and never throw. A response that is not an
OpenAI chat completion (``/v1/messages``, the responses API) is left alone.
AUTO_FOOTER_ENABLED=false turns the whole feature off. See
ai/litellm/LITELLM.md > The `auto` footer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..deployments import strip_provider
from ..log import ThrottledLog
from ..overflow.filter import OverflowFilter
from ..overflow.state import BUSY, DOWN
from ..routing.stash import fell_back, requested_auto
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

# The last footer part when the engine did not route an `auto` request.
FALLBACK_TAG = "route-policy fallback"


@dataclass(frozen=True)
class Served:
    """Who answered a request (AutoFooter.served).

    chain     the chain alias (model group) that served it: local-general, ...
    model     that deployment's litellm_params.model: openai/qwen3.8-solo, ...
    """

    chain: Optional[str]
    model: Optional[str]


def is_claude(model: Optional[str]) -> bool:
    """True for a Claude deployment (anthropic/..., or a bare claude-* name) --
    the only deployments that answer a chain as overflow."""
    if not isinstance(model, str) or not model:
        return False
    name = clean(strip_provider(model)) or ""
    return model.startswith("anthropic/") or name.startswith("claude")


class AutoFooter:
    """When to add the footer, what it says, and the non-streaming edit.
    The streaming edit is footer/stream.py's FooterStream."""

    def __init__(
        self,
        enabled: bool,
        overflow: OverflowFilter,
        stats: Dict[str, int],
        log: ThrottledLog,
    ) -> None:
        self.enabled = enabled
        self.overflow = overflow
        self.stats = stats
        self._log = log

    def wanted(self, data: Any) -> bool:
        """A footer for this request's answer? Only when the client asked
        for `auto` (the stash, see routing/stash.py) -- not for a direct
        chain or alias call."""
        if not self.enabled or not isinstance(data, dict) or not requested_auto(data):
            return False
        if data.get("response_format"):
            return False  # structured output: a footer would break the JSON
        return not is_openwebui_task(data)

    def strip_incoming(self, data: Any) -> bool:
        """On an `auto` request, strip footers we emitted from incoming
        assistant messages, in place, so the model never sees (and imitates)
        them and the route-policy engine never reads them. Runs before the
        route-policy rewrite, so data["model"] is still `auto`. True when
        something was stripped. Never raises."""
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
                return None  # not a chat completion (embeddings, /v1/messages, responses API, ...)
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
        """'qwen3.8-solo · local-general', or None when the backend is unknown
        (see served() for where each part comes from)."""
        served = self.served(headers, data)
        name = clean(strip_provider(served.model))
        chain = clean(served.chain)
        if not name or not chain:
            return None
        parts = [name, chain]
        if is_claude(served.model):
            parts.append(self.overflow_tag(chain))
        if fell_back(data):
            parts.append(FALLBACK_TAG)
        return FOOTER_SEP.join(parts)

    def served(self, headers: Dict[str, Any], data: Any) -> Served:
        """Which chain and deployment answered -- what the footer names, and
        what the route-policy client reports in /observe.

        data["deployment"] (common_request_processing.py:1707): model_name is
        the chain, litellm_params.model the deployment. The response's
        x-litellm-model-group names the chain when there is no deployment.
        May raise on malformed input; callers guard."""
        chain, model = deployment_fields(data.get("deployment") if isinstance(data, dict) else None)
        group = (headers or {}).get("x-litellm-model-group")
        if not chain and isinstance(group, str) and group and group != AUTO_MODEL:
            chain = group
        return Served(chain=chain or None, model=model or None)

    def overflow_tag(self, chain: str) -> str:
        """'overflow (local busy)' / '(local down)' from the overflow filter's
        CURRENT view of the chain's local backends -- an approximation of why
        Claude answered."""
        reason = self.overflow_reason(chain)
        if reason == DOWN:
            return "overflow (local down)"
        if reason == BUSY:
            return "overflow (local busy)"
        return "overflow"

    def overflow_reason(self, chain: Any) -> Optional[str]:
        """DOWN when every local backend of `chain` the filter knows is down,
        BUSY when any is busy, else None (no verdict, or a call-time failure).
        The CURRENT view, so only an approximation of why Claude answered.
        The filter learnt the chain's backends when it filtered this very
        request. Never raises."""
        try:
            if not isinstance(chain, str) or not chain:
                return None
            verdicts = self.overflow.local_verdicts(chain)
            if verdicts and all(v == DOWN for v in verdicts):
                return DOWN
            if any(v == BUSY for v in verdicts):
                return BUSY
        except Exception:
            pass
        return None

    def error(self, where: str, exc: BaseException) -> None:
        try:
            self.stats["footer_errors"] += 1
            self._log("auto", "footer", where, "auto footer (%s) skipped after an error: %r", where, exc)
        except Exception:
            pass
