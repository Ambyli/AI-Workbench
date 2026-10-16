"""Context fit: drop a local chain deployment the prompt does not fit, on the
backend's own token count. Runs BEFORE the busy/down rule (filter.py).

LiteLLM's own pre-call context check (router.py::_pre_call_checks, 10163)
counts the prompt with::

    return litellm.token_counter(messages=messages)   # router.py:10147

-- OpenAI's tiktoken, over ``messages`` alone. Tool definitions are not
counted (Open WebUI's native tool calling sends them on every turn) and
Qwen's tokenizer is not tiktoken, so on 2026-10-12 a prompt vLLM counted at
122,881 tokens passed as under qwen3.8-solo's 114,688, vLLM refused it with
ContextWindowExceededError, and LiteLLM does not try the next order for that
error (router.py:6134-6137). So the hook counts for itself, with the
backend's own tokenizer: vLLM's ``POST <api_base minus /v1>/tokenize``
renders ``messages`` + ``tools`` through the same chat template and
``preprocess_chat`` as /v1/chat/completions and returns ``count`` and
``max_model_len`` -- exactly the numbers vLLM checks. A local deployment
(an ``api_base`` and an int ``model_info.max_input_tokens``) FITS iff::

    count <= max_input_tokens  and  count + max_out <= max_model_len

with max_out the request's max_completion_tokens / max_tokens, else the
deployment's own. One that does not fit is dropped as ``too-long``, and that
is a HARD drop: the busy/down rule runs on what is left, so its "keep the
busy ones" fallback can never bring it back. If NO local deployment fits, a
group with a Claude deployment keeps only Claude (the prompt is past every
local window -- the same outcome as before); local-private, with nothing
unpolled, gets the list back unchanged, so vLLM refuses it with its own
error and PII stays on the box. (Those two outcomes are decided in
filter.py; this module only says which deployments do not fit.)

/tokenize is only called when it could matter: a deliberately HIGH estimate
(JSON length of messages + tools / OVERFLOW_TOKENIZE_CHARS_PER_TOKEN) must
reach OVERFLOW_TOKENIZE_MIN_FRACTION x that deployment's max_input_tokens.
Each distinct backend is asked at most once per request, all of them
concurrently, within OVERFLOW_TOKENIZE_TIMEOUT_S, and answers are kept in a
small LRU (``auto``'s context_window_fallbacks and the mid-stream order
fallback re-enter with the same messages). ANY tokenize failure -- timeout,
non-200, an image part on a text-only model, a body without ``count`` --
KEEPS the deployment: LiteLLM then behaves exactly as it did before, and
_pre_call_checks stays in place as the backstop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .. import deployments
from ..log import ThrottledLog
from .vllm import ProbeError, Tokenize, tokenize_url

# (api_base, sha256 of the body) -> (count, max_model_len), least recent out.
# Read at run time (not bound at init), so a test can shrink it.
TOKENIZE_CACHE_SIZE = 64


# -- Pure helpers (unit-tested) -------------------------------------------------
def _jsonable(value: Any) -> Any:
    """json.dumps default: pydantic messages / tool objects -> plain data."""
    for attr in ("model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(value)


def request_tools(request_kwargs: Any) -> Optional[List[Any]]:
    tools = request_kwargs.get("tools") if isinstance(request_kwargs, dict) else None
    return tools if isinstance(tools, list) and tools else None


def estimate_tokens(messages: Any, tools: Optional[List[Any]], chars_per_token: float) -> int:
    """A deliberately HIGH token estimate: JSON characters / chars_per_token.

    json.dumps' default separators and ASCII escaping only add characters,
    so non-English text and markup push the estimate up, never down."""
    chars = len(json.dumps(messages, default=_jsonable))
    if tools:
        chars += len(json.dumps(tools, default=_jsonable))
    return int(chars / max(chars_per_token, 0.1)) + 1


def max_output_tokens(request_kwargs: Any, deployment: Any) -> int:
    """The completion budget vLLM will reserve: the request's own
    max_completion_tokens / max_tokens, else the deployment's (LiteLLM lays
    the request's kwargs over the deployment's litellm_params, and vLLM
    prefers max_completion_tokens). 0 when neither sets one -- vLLM then
    gives the rest of the window to the answer and only the input must fit."""
    for src in (request_kwargs if isinstance(request_kwargs, dict) else {}, deployments.params(deployment)):
        for key in ("max_completion_tokens", "max_tokens"):
            value = deployments.positive_int(src.get(key))
            if value is not None:
                return value
    return 0


def chat_template_kwargs(deployment: Any, request_kwargs: Any) -> Optional[Dict[str, Any]]:
    """What vLLM's chat template will be rendered with, besides the server's
    --default-chat-template-kwargs (which /tokenize applies by itself).

    LiteLLM REPLACES rather than merges (measured on the outgoing request
    body with both 1.93.0 and 1.95.0): the Router builds the call as {**deployment litellm_params,
    **request kwargs} (router.py::_acompletion), so a request `extra_body`
    replaces the deployment's whole extra_body; then, for an openai/ provider,
    a top-level request `chat_template_kwargs` is written over
    extra_body["chat_template_kwargs"] as one value
    (utils.py::add_provider_specific_params_to_optional_params). So the
    template is rendered with exactly ONE of: the request's top-level
    chat_template_kwargs, else the request's extra_body's, else -- only when
    the request sent no extra_body at all -- the deployment's. Mirroring
    that is what keeps /tokenize counting the prompt vLLM will see (the
    local-code deployments set reasoning_effort / reasoning_strength here,
    and a client that sends its own chat_template_kwargs drops them)."""
    req = request_kwargs if isinstance(request_kwargs, dict) else {}
    top = req.get("chat_template_kwargs")
    if top is not None:
        return dict(top) if isinstance(top, dict) and top else None
    extra = req["extra_body"] if "extra_body" in req else deployments.params(deployment).get("extra_body")
    value = extra.get("chat_template_kwargs") if isinstance(extra, dict) else None
    return dict(value) if isinstance(value, dict) and value else None


def system_prompt_messages(deployment: Any, messages: Any) -> Any:
    """`messages` as LiteLLM will send them to this deployment: a
    `litellm_system_prompt` litellm_param is merged IN FRONT of the first
    system message (or becomes a new first system message) by
    main.py::completion -> add_system_prompt_to_messages(...,
    merge_with_first_system=True), AFTER the router has picked the
    deployment -- so this hook must add it itself to count it."""
    prompt = deployments.params(deployment).get("litellm_system_prompt")
    if not isinstance(prompt, str) or not prompt or not isinstance(messages, list):
        return messages
    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") == "system":
        merged = dict(first)
        content = first.get("content", "")
        if isinstance(content, str):
            merged["content"] = f"{prompt.strip()}\n\n{content}"
        elif isinstance(content, list):
            merged["content"] = [{"type": "text", "text": prompt.strip()}] + list(content)
        else:
            merged["content"] = [{"type": "text", "text": prompt.strip()}]
        return [merged] + list(messages[1:])
    return [{"role": "system", "content": prompt}] + list(messages)


def tokenize_body(
    deployment: Any, messages: Any, tools: Optional[List[Any]], request_kwargs: Any
) -> Dict[str, Any]:
    """The /tokenize request (vLLM TokenizeChatRequest): the same messages
    (with the deployment's litellm_system_prompt merged in), tools and
    template kwargs the chat completion will carry, plus the generation
    prompt, so `count` is the prompt vLLM will actually see."""
    body: Dict[str, Any] = {
        "model": deployments.strip_provider(deployments.params(deployment).get("model")) or "",
        "messages": system_prompt_messages(deployment, messages),
        "add_generation_prompt": True,
    }
    if tools:
        body["tools"] = tools
    ctk = chat_template_kwargs(deployment, request_kwargs)
    if ctk:
        body["chat_template_kwargs"] = ctk
    # Round-trip through JSON: plain data for httpx, and a stable cache key.
    return json.loads(json.dumps(body, default=_jsonable))


def tokenize_cache_key(url: str, body: Dict[str, Any]) -> Tuple[str, str]:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return url, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def context_verdict(
    count: int, max_input: int, max_model_len: Optional[int], max_out: int
) -> Optional[str]:
    """None when the prompt fits; otherwise why it does not (for the log)."""
    if count > max_input:
        return f"count={count} > max_input_tokens={max_input}"
    if max_model_len is not None and count + max_out > max_model_len:
        return f"count={count} + max_tokens={max_out} > max_model_len={max_model_len}"
    return None


# -- The step -------------------------------------------------------------------
class ContextFit:
    """Which local deployments of a chain the prompt does not fit, with the
    /tokenize answers cached. Never raises out of a single failed count."""

    def __init__(
        self,
        tokenize: Tokenize,
        min_fraction: float,
        chars_per_token: float,
        timeout: float,
        stats: Dict[str, int],
        log: ThrottledLog,
    ) -> None:
        self.min_fraction = min_fraction
        self.chars_per_token = chars_per_token
        self.timeout = timeout
        self.cache: "OrderedDict[Tuple[str, str], Tuple[int, Optional[int]]]" = OrderedDict()
        self.stats = stats
        self._tokenize = tokenize
        self._log = log

    async def check(
        self, model: str, candidates: List, messages: List, request_kwargs: dict
    ) -> Dict[int, str]:
        """{index in `candidates`: why} for every local deployment the prompt
        does not fit. Deployments that cannot be measured are never listed."""
        tools = request_tools(request_kwargs)
        estimate: Optional[int] = None
        pending: Dict[Tuple[str, str], Tuple[str, Dict[str, Any]]] = {}
        measured: List[Tuple[int, Tuple[str, str], int, int]] = []
        for i, d in enumerate(candidates):
            base = deployments.api_base(d)
            limit = deployments.max_input_tokens(d)
            if base is None or limit is None:
                continue  # Claude, or a local deployment with no declared window
            if estimate is None:
                estimate = estimate_tokens(messages, tools, self.chars_per_token)
            if estimate < self.min_fraction * limit:
                continue  # cannot be close: no /tokenize call
            url = tokenize_url(base)
            body = tokenize_body(d, messages, tools, request_kwargs)
            key = tokenize_cache_key(url, body)
            pending.setdefault(key, (url, body))
            measured.append((i, key, limit, max_output_tokens(request_kwargs, d)))
        if not measured:
            return {}
        keys = list(pending)
        counted = await asyncio.gather(*(self._count(model, k, *pending[k]) for k in keys))
        results = dict(zip(keys, counted))
        out: Dict[int, str] = {}
        for i, key, limit, max_out in measured:
            res = results.get(key)
            if res is None:
                continue  # tokenize failed: keep it, LiteLLM decides as before
            reason = context_verdict(res[0], limit, res[1], max_out)
            if reason is not None:
                out[i] = reason
        return out

    async def _count(
        self, model: str, key: Tuple[str, str], url: str, body: Dict[str, Any]
    ) -> Optional[Tuple[int, Optional[int]]]:
        """(count, max_model_len) from the cache or one bounded /tokenize call;
        None on any failure."""
        hit = self.cache.get(key)
        if hit is not None:
            self.cache.move_to_end(key)
            self.stats["tokenize_cache_hits"] += 1
            return hit
        self.stats["tokenize_calls"] += 1
        try:
            data = await asyncio.wait_for(self._tokenize(url, body), timeout=self.timeout)
            count = data.get("count") if isinstance(data, dict) else None
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ProbeError("no integer `count` in the response")
            result = (count, deployments.positive_int(data.get("max_model_len")))
        except Exception as exc:
            self.stats["tokenize_errors"] += 1
            self._log(model, url, "tokenize-failed",
                      "%s: /tokenize at %s failed (%s); keeping that backend's deployment(s)",
                      model, url, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
            return None
        self.cache[key] = result
        while len(self.cache) > TOKENIZE_CACHE_SIZE:
            self.cache.popitem(last=False)
        return result
