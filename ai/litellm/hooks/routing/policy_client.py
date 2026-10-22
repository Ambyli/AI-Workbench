"""THE ROUTE-POLICY CLIENT (phase 3 of the route-policy plan: single pass).

On every request for model ``auto`` the pre-call hook ASKS the route-policy
service (ai/route-policy/, ``http://route-policy:8000`` on ai_shared) which
chain alias should serve it, rewrites ``data["model"]`` to that chain, and
LiteLLM serves it in the same pass -- streamed. Afterwards it tells the
service what really answered:

    pre-call   POST /decide  {decision_id, requested_model, litellm_call_id,
               (AWAITED)      chat_id, message_id, user_id, key_alias,
                              messages, tools, backends, background_task}
               <- {decision_id, route, mode, apply, ruleset, votes, scores, latency_ms}
    success    POST /observe {decision_id, litellm_call_id, response_id,
               (or stream     served_model_group, served_deployment,
                end)          overflow_reason, finish_reason, tool_calls,
                              status: "success", error: null}
    failure    POST /observe {..., status: "failure", error: "<exception>"}

THE ROUTE. /decide is awaited under ROUTE_POLICY_DECIDE_TIMEOUT_S
(asyncio.wait_for -- LiteLLM puts no timeout around a pre-call hook,
proxy/utils.py:1444-1457 at v1.95.0). The answer's route is applied only when
``apply`` is ``true`` (an ``enforce`` ruleset whose own invariants held) AND
the hook's copy of the invariants allows it (routing/invariants.py: one of the
four chain aliases). EVERYTHING ELSE goes to ``local-private`` -- the chain
with no Claude deployment, because nothing has checked such a prompt for
PII: a timeout, a transport error, a non-2xx, a body that is not a JSON
object, ``apply: false`` (a shadow ruleset), a route that is not a chain, the
client being off (ROUTE_POLICY_ENABLED=false or an empty ROUTE_POLICY_URL), a
request with no ``messages`` to decide on, or a bug here. Each fallback is
counted (``route_policy_fallbacks``, and by reason in ``state()``) and logged,
throttled per reason: ``[route-policy] auto -> local-private (<why>)``.
Requests for any other model are never decided and never rewritten.

The rewrite works because the proxy routes on the pre-call hook's ``data``
(common_request_processing.py:1274 -> route_request). ``auto`` itself is a
``router_settings.model_group_alias`` for local-private, resolved only at
deployment selection (router.py:10465) -- AFTER this hook -- so the hook
still sees ``"auto"``, and a request this hook never rewrote (the callback
list failed to load, say) is served by local-private too. The client keeps
seeing ``"model": "auto"`` in the response (requested_model_from_client).

THE STASH (routing/stash.py) records requested_model / route / source /
decision_id as ``zeo_route_policy`` in the proxy's own metadata dict
(``data["metadata"]``, or ``data["litellm_metadata"]`` on /v1/messages and
the other routes where ``metadata`` is a request parameter); the footer and
the /observe paths read it, never ``data["model"]``. LiteLLM v1.95.0 hands the
success, streaming-iterator and failure hooks the very ``data`` dict the
pre-call hook got: common_request_processing.py assigns the pre-call result to
self.data (1274) and passes self.data to post_call_success_hook (1919), to the
stream generator as request_data (1854-1859 -> proxy_server.py
async_data_generator, which hands it to the iterator hook, 7413-7419, and to
post_call_failure_hook on an upstream error, 7540) and to
post_call_failure_hook on a failed call (2538-2542). Neither dict reaches
the upstream: the openai provider forwards ``metadata`` only with
litellm.enable_preview_features, and even then only string values
(main.py:2407, utils.py:9243 add_openai_metadata) -- the stash is a dict --
and ``litellm_metadata`` is LiteLLM's own.

WHAT IS SENT. ``messages`` / ``tools`` are the request's as this proxy will
forward them (after the footer hook stripped old footers), JSON-encoded in
the hook before the await -- with every inline binary payload in a content
part (a base64 image, document or audio; any ``data:`` URI) replaced by a
short placeholder that keeps the part's type, so a chat full of images costs
the critical path kilobytes, not megabytes (slim_messages; text parts and
tool calls are never touched, the request itself is never changed).
``backends`` is the overflow poller's per-backend view
(Poller.report). ``chat_id`` / ``message_id`` / ``user_id`` are Open WebUI's
X-OpenWebUI-Chat-Id / -Message-Id / -User-Id request headers
(ENABLE_FORWARD_USER_INFO_HEADERS and patch 0005), read from the proxy
metadata dict's ``headers`` (litellm_pre_call_utils.py:1450), any case,
None when absent. ``background_task`` is the footer's own Open WebUI
background-task detection (footer/render.py ``is_openwebui_task``: a
``### Task:`` title / tags / follow-up prompt sent with ``stream`` false, or
legacy function calling's ``Available Tools:`` system message) -- computed
here because it needs ``data["stream"]``, which is not in the body; the
service then classifies no such request and never pins a turn to one. The
served chain / deployment come from
``data["deployment"]`` (common_request_processing.py:1707) through the
footer's own derivation (AutoFooter.served); ``overflow_reason`` is set only
when a Claude deployment answered, from the overflow filter's CURRENT view.

/observe IS FIRED, NEVER AWAITED: an asyncio task on the proxy's loop, at most
MAX_IN_FLIGHT at once (past that it is dropped and counted,
``route_policy_dropped``), each bounded by ROUTE_POLICY_TIMEOUT_S. It is sent
for every request whose /decide was attempted -- applied or fallen back --
exactly once (the stash's ``observed`` flag), never for a request that sent
no /decide. Nothing here raises into LiteLLM. See ai/litellm/LITELLM.md >
Route policy: single-pass `auto`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from ..footer.auto import AUTO_MODEL, Served, is_claude
from ..footer.render import get_field, headers_of, is_openwebui_task, to_int
from ..log import ThrottledLog, logger
from ..overflow.poller import Poller
from .invariants import PRIVATE_ROUTE, enforce
from .stash import ENGINE, FALLBACK_PREFIX, STASH_KEY, drop_foreign_stash, metadata_of, stash_of

DECIDE_PATH = "/decide"
OBSERVE_PATH = "/observe"
# Background /observe calls in flight at once.
MAX_IN_FLIGHT = 64
# Longest `error` string sent in a failure /observe.
ERROR_MAX_CHARS = 500
# Contract field -> the Open WebUI request header it is read from.
OPENWEBUI_ID_HEADERS = {
    "chat_id": "x-openwebui-chat-id",
    "message_id": "x-openwebui-message-id",
    "user_id": "x-openwebui-user-id",
}
SUCCESS, FAILURE = "success", "failure"
DISCONNECTED = "client disconnected before the end of the stream"

# The fallback reasons (stash `source` is "fallback:<reason>"; the counters in
# state()["route_policy_fallbacks_by_reason"] are keyed by them). An HTTP
# status is "http-<code>".
DISABLED = "disabled"          # ROUTE_POLICY_ENABLED=false or no ROUTE_POLICY_URL
NO_MESSAGES = "no-messages"    # not a chat request (no `messages` list): nothing to decide on
TIMEOUT = "timeout"            # no /decide answer within ROUTE_POLICY_DECIDE_TIMEOUT_S
ERROR = "error"                # transport error (refused, reset, DNS, ...)
BAD_RESPONSE = "bad-response"  # 2xx, but not a JSON object (or another decision's id)
NOT_APPLIED = "not-applied"    # apply is not true (a shadow ruleset, or the service's invariants)
NOT_A_CHAIN = "not-a-chain"    # the route failed the hook's own invariants
HOOK_ERROR = "hook-error"      # a bug in this module

# POST <url> <JSON body bytes> -> (HTTP status, response body); raise on a transport error.
Post = Callable[[str, bytes], Awaitable[Tuple[int, bytes]]]
# (lower-cased response headers, request data) -> who answered.
ServedFn = Callable[[Dict[str, Any], Any], Served]
# chain alias -> "busy" | "down" | None.
OverflowFn = Callable[[Any], Optional[str]]


class HttpxPost:
    """POST a JSON body; return (status, body). One httpx.AsyncClient per
    event loop, like hooks/overflow/vllm.py's clients (an AsyncClient is bound
    to the loop it first ran on). The caller bounds the whole call with
    asyncio.wait_for -- httpx's own timeout is per operation. No auth: the
    service sits on ai_shared without any, like the classifier."""

    def __init__(self, timeout: float, transport: Any = None) -> None:
        self._timeout = timeout
        self._transport = transport  # tests: an httpx.MockTransport
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def __call__(self, url: str, body: bytes) -> Tuple[int, bytes]:
        import httpx  # LiteLLM depends on it; imported lazily for the tests

        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            kwargs: Dict[str, Any] = {"timeout": self._timeout}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
            self._loop = loop
        resp = await self._client.post(url, content=body, headers={"content-type": "application/json"})
        return resp.status_code, resp.content


# -- Pure helpers (unit-tested) -------------------------------------------------
def _jsonable(value: Any) -> Any:
    """json.dumps default: pydantic objects -> plain data, anything else -> str."""
    for attr in ("model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(value)


def encode(body: Dict[str, Any]) -> bytes:
    return json.dumps(body, default=_jsonable, ensure_ascii=False).encode("utf-8")


def split_reply(reply: Any) -> Tuple[Any, bytes]:
    """(status, body) from what a Post returned; a bare status has an empty body."""
    if isinstance(reply, tuple) and len(reply) == 2:
        status, body = reply
        return status, body if isinstance(body, (bytes, bytearray, str)) else b""
    return reply, b""


def parse_decision(raw: Any) -> Optional[Dict[str, Any]]:
    """The /decide answer as a dict, or None when it is not a JSON object."""
    try:
        answer = json.loads(raw)
    except Exception:
        return None
    return answer if isinstance(answer, dict) else None


def request_ids(data: Any) -> Dict[str, Optional[str]]:
    """chat_id / message_id / user_id from the request's Open WebUI headers
    (the proxy metadata dict's ``headers``, any case); None when absent or empty."""
    out: Dict[str, Optional[str]] = {k: None for k in OPENWEBUI_ID_HEADERS}
    meta = metadata_of(data)
    headers = meta.get("headers") if meta is not None else None
    if not isinstance(headers, dict):
        return out
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for field, name in OPENWEBUI_ID_HEADERS.items():
        value = lowered.get(name)
        if isinstance(value, str) and value.strip():
            out[field] = value.strip()
    return out


def key_alias_of(user_api_key_dict: Any) -> Optional[str]:
    value = get_field(user_api_key_dict, "key_alias") if user_api_key_dict is not None else None
    return value if isinstance(value, str) and value else None


# -- Keeping /decide small ---------------------------------------------------------
# A data: URI shorter than this is left alone (it cannot be a real payload).
DATA_URI_MIN_CHARS = 128
# Content parts whose every string the evaluators may need verbatim.
VERBATIM_PART_TYPES = ("text", "input_text", "tool_use")


def omitted(value: str) -> str:
    """The placeholder for an inline payload: a data: URI keeps its header
    (``data:image/png;base64,``), so the media type is still visible."""
    head = value[: value.index(",") + 1] if value.startswith("data:") and "," in value[:256] else ""
    return f"{head}[omitted: {len(value)} chars]"


def _is_data_uri(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("data:") and len(value) >= DATA_URI_MIN_CHARS


def _slim(obj: Any) -> Tuple[Any, bool]:
    """(obj with inline payloads replaced, whether anything was). Copies only
    what it changes; never mutates `obj`."""
    if isinstance(obj, list):
        out, changed = [], False
        for item in obj:
            new, c = _slim(item)
            out.append(new)
            changed = changed or c
        return (out, True) if changed else (obj, False)
    if not isinstance(obj, dict) or obj.get("type") in VERBATIM_PART_TYPES:
        return obj, False
    kind = obj.get("type")
    new_obj: Optional[Dict[str, Any]] = None
    for key, value in obj.items():
        replacement: Any = value
        if isinstance(value, str):
            # Anthropic {"type": "base64", "data": ...} image / document sources,
            # and the raw base64 of an OpenAI input_audio part.
            raw_base64 = key == "data" and (kind == "base64" or "format" in obj) and len(value) >= DATA_URI_MIN_CHARS
            if raw_base64 or _is_data_uri(value):
                replacement = omitted(value)
        elif isinstance(value, (dict, list)):
            replacement, c = _slim(value)
            if not c:
                replacement = value
        if replacement is not value:
            if new_obj is None:
                new_obj = dict(obj)
            new_obj[key] = replacement
    return (new_obj, True) if new_obj is not None else (obj, False)


def slim_messages(messages: Any) -> Any:
    """The messages for /decide: every list-of-parts content with its inline
    binary payloads -- image_url / input_image data URIs, Anthropic image and
    document ``source.data``, input_audio data, a ``file_data`` URI, any other
    data: URI in a part -- replaced by omitted(...). The part keeps its type
    (the evaluators may still see that an image was there); text parts, string
    contents and tool calls are untouched. A changed message is a shallow copy
    with a new content list; the request's own messages are never modified."""
    if not isinstance(messages, list):
        return messages
    out: List[Any] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            new, changed = _slim(content)
            if changed:
                m = {**m, "content": new}
        out.append(m)
    return out


def build_decide_body(
    decision_id: str,
    data: Dict[str, Any],
    user_api_key_dict: Any,
    backends: Dict[str, Any],
) -> Dict[str, Any]:
    """The /decide request body (the contract in the route-policy plan)."""
    tools = data.get("tools")
    call_id = data.get("litellm_call_id")
    return {
        "decision_id": decision_id,
        "requested_model": data.get("model"),
        "litellm_call_id": call_id if isinstance(call_id, str) and call_id else None,
        **request_ids(data),
        "key_alias": key_alias_of(user_api_key_dict),
        "messages": slim_messages(data["messages"]),
        "tools": list(tools) if isinstance(tools, list) and tools else None,
        "backends": backends,
        "background_task": is_openwebui_task(data),
    }


def tool_call_names(obj: Any) -> List[str]:
    """Function names of the tool calls on a message or a streamed delta
    (`tool_calls[].function.name`, or the legacy `function_call.name`), in
    order. A streamed call names its function in its first delta only."""
    names: List[str] = []
    calls = get_field(obj, "tool_calls")
    if isinstance(calls, list):
        for call in calls:
            name = get_field(get_field(call, "function"), "name")
            if isinstance(name, str) and name:
                names.append(name)
    legacy = get_field(get_field(obj, "function_call"), "name")
    if isinstance(legacy, str) and legacy:
        names.append(legacy)
    return names


def response_outcome(response: Any) -> Tuple[Optional[str], List[str]]:
    """(finish_reason, called tool names) of a non-streamed answer: an OpenAI
    chat completion (choice 0's finish_reason, every choice's tool calls) or an
    Anthropic /v1/messages response (stop_reason, its tool_use blocks)."""
    choices = get_field(response, "choices")
    if isinstance(choices, list):
        finish: Optional[str] = None
        names: List[str] = []
        for choice in sorted(choices, key=lambda c: to_int(get_field(c, "index"))):
            if finish is None:
                finish = _str_or_none(get_field(choice, "finish_reason"))
            names.extend(tool_call_names(get_field(choice, "message")))
        return finish, names
    content = get_field(response, "content")
    names = []
    if isinstance(content, list):
        for block in content:
            name = get_field(block, "name")
            if get_field(block, "type") == "tool_use" and isinstance(name, str) and name:
                names.append(name)
    return _str_or_none(get_field(response, "stop_reason")), names


def error_text(exc: Any) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:ERROR_MAX_CHARS]


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _ruleset_label(answer: Dict[str, Any]) -> str:
    ruleset = answer.get("ruleset")
    if isinstance(ruleset, dict) and ruleset.get("id") is not None:
        return f"{ruleset.get('id')}@{ruleset.get('version')}"
    return "?"


# -- The client -----------------------------------------------------------------
class PolicyClient:
    """Routes `auto` requests (awaited /decide) and fires /observe after them.
    Never raises. `served` / `overflow_reason` are the footer's derivations
    (AutoFooter.served / .overflow_reason); `poller` supplies `backends`."""

    def __init__(
        self,
        enabled: bool,
        base_url: str,
        timeout: float,
        decide_timeout: float,
        post: Post,
        poller: Poller,
        served: ServedFn,
        overflow_reason: OverflowFn,
        clock: Callable[[], float],
        stats: Dict[str, int],
        log: ThrottledLog,
        max_in_flight: int = MAX_IN_FLIGHT,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.enabled = bool(enabled) and bool(self.base_url)
        self.timeout = timeout
        self.decide_timeout = decide_timeout
        self.max_in_flight = max(1, int(max_in_flight))
        self.stats = stats
        self.fallbacks: Dict[str, int] = {}
        self._post = post
        self._poller = poller
        self._served = served
        self._overflow_reason = overflow_reason
        self._clock = clock
        self._log = log
        self._tasks: Set["asyncio.Task[None]"] = set()
        self._announced: Set[str] = set()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

    # -- pre-call: the route ---------------------------------------------------------
    async def route(self, data: Any, user_api_key_dict: Any) -> bool:
        """Every request: drop a stash the client sent. An `auto` request:
        await /decide and rewrite data["model"] to the chosen chain, or to
        local-private (see the module docstring), and stash what happened.
        True when data["model"] was rewritten -- every `auto` request; other
        requests are never rewritten. Never raises (a cancelled request's
        CancelledError still propagates)."""
        try:
            drop_foreign_stash(data)
            if not isinstance(data, dict) or data.get("model") != AUTO_MODEL:
                return False
        except Exception:
            return False
        try:
            return await self._route(data, user_api_key_dict)
        except Exception as exc:
            self._error("route", exc)
            return self._fallback(data, HOOK_ERROR, f"internal error: {exc!r}", None)

    async def _route(self, data: Dict[str, Any], user_api_key_dict: Any) -> bool:
        if not self.enabled:
            return self._fallback(data, DISABLED, "client off: ROUTE_POLICY_ENABLED=false or no ROUTE_POLICY_URL",
                                  None)
        if not isinstance(data.get("messages"), list):
            return self._fallback(data, NO_MESSAGES, "not a chat request: no `messages` to decide on", None)
        decision_id = str(uuid.uuid4())
        body = encode(build_decide_body(decision_id, data, user_api_key_dict, self._poller.report(self._clock())))
        try:
            reply = await asyncio.wait_for(self._post(self.base_url + DECIDE_PATH, body), timeout=self.decide_timeout)
        except asyncio.TimeoutError:
            return self._failed_decide(data, TIMEOUT, f"no /decide answer in {self.decide_timeout:g}s", decision_id)
        except Exception as exc:
            return self._failed_decide(data, ERROR, f"/decide failed: {error_text(exc)}", decision_id)
        status, raw = split_reply(reply)
        if not (isinstance(status, int) and 200 <= status < 300):
            return self._failed_decide(data, f"http-{status}", f"/decide answered HTTP {status}", decision_id)
        self.stats["route_policy_decides"] += 1
        answer = parse_decision(raw)
        if answer is None:
            return self._failed_decide(data, BAD_RESPONSE, "/decide answered something that is not a JSON object",
                                       decision_id)
        if answer.get("decision_id") not in (None, decision_id):
            return self._failed_decide(data, BAD_RESPONSE,
                                       f"/decide answered for decision {answer.get('decision_id')!r}", decision_id)
        route = answer.get("route")
        if answer.get("apply") is not True:
            return self._fallback(data, NOT_APPLIED,
                                  f"/decide chose {route!r} but apply={answer.get('apply')!r} "
                                  f"(ruleset {_ruleset_label(answer)}, mode {answer.get('mode')!r})", decision_id)
        checked = enforce(AUTO_MODEL, route)
        if not checked.allowed:
            return self._failed_decide(data, NOT_A_CHAIN, "; ".join(checked.notes) or f"{route!r} refused",
                                       decision_id)
        # The rewrite is the last step: every failure above left the request
        # for the fallback instead.
        data["model"] = checked.route
        self.stats["route_policy_applied"] += 1
        self._stash(data, checked.route, ENGINE, decision_id)
        if "decide" not in self._announced:  # one positive line per process
            self._announced.add("decide")
            try:
                logger.info("[route-policy] first decision applied: auto -> %s (ruleset %s) -- single pass is live",
                            checked.route, _ruleset_label(answer))
            except Exception:
                pass
        return True

    def _failed_decide(self, data: Dict[str, Any], why: str, detail: str, decision_id: Optional[str]) -> bool:
        """A /decide that did not produce a usable answer: an error, then the fallback."""
        try:
            self.stats["route_policy_errors"] += 1
        except Exception:
            pass
        return self._fallback(data, why, detail, decision_id)

    def _fallback(self, data: Any, why: str, detail: str, decision_id: Optional[str]) -> bool:
        """data["model"] = local-private, stash it as a fallback, count and log it."""
        try:
            data["model"] = PRIVATE_ROUTE  # first: whatever fails below, the request is safe
        except Exception:
            return False  # not a dict we can write: the alias still serves `auto` from local-private
        try:
            self.stats["route_policy_fallbacks"] += 1
            self.fallbacks[why] = self.fallbacks.get(why, 0) + 1
            self._stash(data, PRIVATE_ROUTE, FALLBACK_PREFIX + why, decision_id)
            self._log("route-policy", "fallback", why, "[route-policy] auto -> %s (%s)", PRIVATE_ROUTE, detail)
        except Exception:
            pass
        return True

    @staticmethod
    def _stash(data: Dict[str, Any], route: str, source: str, decision_id: Optional[str]) -> None:
        meta = metadata_of(data)  # the proxy's own dict: metadata, or litellm_metadata on /v1/messages
        if meta is None and data.get("metadata") is None:
            meta = data["metadata"] = {}
        if meta is None:
            return  # nowhere to put it: no footer, no /observe for this request
        call_id = data.get("litellm_call_id")
        meta[STASH_KEY] = {
            "requested_model": AUTO_MODEL,
            "route": route,
            "source": source,
            "decision_id": decision_id,
            "litellm_call_id": call_id if isinstance(call_id, str) and call_id else None,
            "observed": False,
        }

    # -- post-call ---------------------------------------------------------------
    def _observable(self, data: Any) -> Optional[Dict[str, Any]]:
        """The stash of a request whose /decide was attempted and that has not
        been observed yet, else None."""
        stash = stash_of(data) if self.enabled else None
        if stash is None or stash.get("observed") or not isinstance(stash.get("decision_id"), str):
            return None
        return stash

    def observe_response(self, data: Any, response: Any) -> None:
        """Non-streaming success: /observe from the response object."""
        stash = self._observable(data)
        if stash is None:
            return
        try:
            finish, names = response_outcome(response)
            self._observe(data, stash, SUCCESS, headers_of(response), _str_or_none(get_field(response, "id")),
                          finish, names, None)
        except Exception as exc:
            self._error("observe", exc)

    def watch_stream(self, data: Any, wrapper: Any) -> Optional["StreamWatch"]:
        """A StreamWatch for a streamed response that has a decision to
        observe, else None (the stream is then not watched at all)."""
        stash = self._observable(data)
        return StreamWatch(self, data, stash, wrapper) if stash is not None else None

    def observe_failure(self, data: Any, exc: Any) -> None:
        """post_call_failure_hook: /observe with status failure."""
        stash = self._observable(data)
        if stash is None:
            return
        try:
            self._observe(data, stash, FAILURE, {}, None, None, [], error_text(exc))
        except Exception as err:
            self._error("observe", err)

    def _observe(
        self,
        data: Any,
        stash: Dict[str, Any],
        status: str,
        headers: Dict[str, Any],
        response_id: Optional[str],
        finish_reason: Optional[str],
        tool_calls: List[str],
        error: Optional[str],
    ) -> None:
        # Mark first: whatever happens below, this request is never observed twice.
        stash["observed"] = True
        served = Served(chain=None, model=None)
        try:
            served = self._served(headers, data)
        except Exception:
            pass
        reason = self._overflow_reason(served.chain) if is_claude(served.model) else None
        call_id = data.get("litellm_call_id") if isinstance(data, dict) else None
        body = {
            "decision_id": stash["decision_id"],
            "litellm_call_id": _str_or_none(call_id) or stash.get("litellm_call_id"),
            "response_id": response_id,
            "served_model_group": served.chain,
            "served_deployment": served.model,
            "overflow_reason": reason,
            "finish_reason": finish_reason,
            "tool_calls": tool_calls,
            "status": status,
            "error": error,
        }
        self._fire(OBSERVE_PATH, body)

    # -- background /observe ------------------------------------------------------
    def _fire(self, path: str, body: Dict[str, Any]) -> bool:
        """Start one bounded background POST. False when it was dropped."""
        if len(self._tasks) >= self.max_in_flight:
            self.stats["route_policy_dropped"] += 1
            self._log("route-policy", path, "dropped",
                      "[route-policy] %s dropped: %d calls already in flight (service slow or down?)",
                      path, len(self._tasks))
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.stats["route_policy_dropped"] += 1
            return False  # no event loop to run it on (never in the proxy)
        task = loop.create_task(self._send(path, body), name="zeo-route-policy" + path.replace("/", "-"))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _send(self, path: str, body: Dict[str, Any]) -> None:
        try:
            reply = await asyncio.wait_for(self._post(self.base_url + path, encode(body)), timeout=self.timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._failed(path, f"no answer in {self.timeout:g}s")
            return
        except Exception as exc:
            self._failed(path, error_text(exc))
            return
        status, _ = split_reply(reply)
        if isinstance(status, int) and 200 <= status < 300:
            self.stats["route_policy_observes"] += 1
            if path not in self._announced:  # one positive line per path per process
                self._announced.add(path)
                try:
                    logger.info("[route-policy] first POST %s%s answered HTTP %d", self.base_url, path, status)
                except Exception:
                    pass
        else:
            self._failed(path, f"HTTP {status}")

    def _failed(self, path: str, why: str) -> None:
        try:
            self.stats["route_policy_errors"] += 1
            self._log("route-policy", path, "error", "[route-policy] POST %s%s failed: %s",
                      self.base_url, path, why)
        except Exception:
            pass

    def _error(self, where: str, exc: BaseException) -> None:
        try:
            self.stats["route_policy_errors"] += 1
            self._log("route-policy", where, "bug", "[route-policy] %s skipped after an error: %r", where, exc)
        except Exception:
            pass


class StreamWatch:
    """Per-request state for observing a streamed `auto` answer. Fed every
    upstream chunk by Handler.async_post_call_streaming_iterator_hook (the
    same generator that adds the footer -- the stream is wrapped once);
    see() / succeeded() / failed() / closed() never raise.

    response_id is the first chunk's `id` (what the spend log keys on,
    streaming_handler.py:735-742); finish_reason is choice 0's (else the
    first choice's); tool_calls are the function names streamed in
    delta.tool_calls, in order. A /v1/messages stream (Anthropic events) has
    no `choices`: it is observed with whatever the chunks carry."""

    def __init__(self, client: PolicyClient, data: Any, stash: Dict[str, Any], wrapper: Any) -> None:
        self.client = client
        self.data = data
        self.stash = stash
        self.wrapper = wrapper
        self.ok = True
        self.response_id: Optional[str] = None
        self.finish: Dict[int, str] = {}
        self.tool_calls: List[str] = []
        self.last_chunk: Any = None

    def see(self, chunk: Any) -> None:
        if not self.ok:
            return
        try:
            self.last_chunk = chunk
            if self.response_id is None:
                self.response_id = _str_or_none(get_field(chunk, "id"))
            choices = get_field(chunk, "choices")
            if not isinstance(choices, list):
                return
            for c in choices:
                delta = get_field(c, "delta")
                if delta is not None:
                    self.tool_calls.extend(tool_call_names(delta))
                reason = _str_or_none(get_field(c, "finish_reason"))
                if reason is not None:
                    self.finish.setdefault(to_int(get_field(c, "index")), reason)
        except Exception as exc:
            self.ok = False
            self.client._error("stream", exc)

    def _finish_reason(self) -> Optional[str]:
        if 0 in self.finish:
            return self.finish[0]
        return self.finish[min(self.finish)] if self.finish else None

    def _headers(self) -> Dict[str, Any]:
        return {**headers_of(self.last_chunk), **headers_of(self.wrapper)}

    def succeeded(self) -> None:
        """The upstream stream ended normally."""
        self._end(SUCCESS, None)

    def failed(self, exc: BaseException) -> None:
        """The upstream stream raised (the proxy then also calls
        post_call_failure_hook; the stash makes that one a no-op)."""
        self._end(FAILURE, error_text(exc))

    def closed(self) -> None:
        """The generator was closed before the stream ended -- the client
        went away (the proxy closes the outermost generator on disconnect;
        this one sees GeneratorExit when it is collected). No-op once observed."""
        self._end(FAILURE, DISCONNECTED)

    def _end(self, status: str, error: Optional[str]) -> None:
        if self.stash.get("observed"):
            return
        try:
            self.client._observe(self.data, self.stash, status, self._headers(), self.response_id,
                                 self._finish_reason(), list(self.tool_calls), error)
        except Exception as err:
            self.client._error("observe", err)
