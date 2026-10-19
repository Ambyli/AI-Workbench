"""THE ROUTE-POLICY CLIENT, SHADOW MODE (phase 2 of the route-policy plan).

On every request for model ``auto`` it tells the route-policy service
(ai/route-policy/, ``http://route-policy:8000`` on ai_shared) what came in,
and afterwards what answered -- and it changes NOTHING about routing:

    pre-call   POST /decide  {decision_id, requested_model, litellm_call_id,
                              chat_id, message_id, user_id, key_alias,
                              messages, tools, backends}
    success    POST /observe {decision_id, litellm_call_id, response_id,
               (or stream     served_model_group, served_deployment,
                end)          overflow_reason, finish_reason, tool_calls,
                              status: "success", error: null}
    failure    POST /observe {..., status: "failure", error: "<exception>"}

Both calls are FIRED, never awaited: each is an asyncio task on the proxy's
own event loop, so the hook returns as fast as it did without them, and the
service's answer is ignored (it logs the decision itself). The two may reach
the service in either order -- the contract says it must not need /decide
first. Phase 3 is when the hook starts awaiting /decide (with a timeout and
a fail-safe route); nothing here does.

THE DECISION ID. The hook makes it (uuid4) and stashes it, with the request's
litellm_call_id, in ``data["metadata"]["zeo_route_policy"]`` (replacing any
the client sent: the proxy keeps unknown client metadata). LiteLLM v1.95.0
hands the success, streaming-iterator and failure hooks the very ``data``
dict the pre-call hook got, so the stash survives to them:
common_request_processing.py assigns the pre-call result to self.data
(1274) and passes self.data to post_call_success_hook (1919), to the stream
generator as request_data (1854-1859 -> proxy_server.py async_data_generator,
which hands it to the iterator hook, 7413-7419, and to post_call_failure_hook
on an upstream error, 7540) and to post_call_failure_hook on a failed call
(2538-2542). Where the proxy copies it (_merge_guardrails_with_existing, only
with model-level guardrails) the copy is shallow, so ``metadata`` -- and the
stash in it -- is the same object. ``metadata`` never reaches the upstream:
the openai provider forwards it only with litellm.enable_preview_features,
and even then only string values (main.py:2407, utils.py:9243
add_openai_metadata) -- the stash is a dict.
The stash also records that an /observe went out, so a request is observed
exactly once even when two paths see its end (a stream that fails is seen by
the iterator hook and by post_call_failure_hook).

WHAT IS SENT. ``messages`` / ``tools`` are the request's as this proxy will
forward them (after the footer hook stripped old footers), snapshotted in the
hook (a shallow copy of each message dict -- O(messages), no serialisation)
and JSON-encoded in the background task. ``backends`` is the overflow
poller's per-backend view (Poller.report). ``chat_id`` / ``message_id`` /
``user_id`` are Open WebUI's X-OpenWebUI-Chat-Id / -Message-Id / -User-Id
request headers (ENABLE_FORWARD_USER_INFO_HEADERS and patch 0005), read from
``data["metadata"]["headers"]`` (litellm_pre_call_utils.py:1450), any case,
None when absent. The served chain / deployment come from the inner LiteLLM's
response headers through the footer's own derivation (AutoFooter.served);
``overflow_reason`` is set only when a Claude deployment answered, from the
overflow filter's CURRENT view (AutoFooter.overflow_reason) -- the same
approximation the footer prints.

BOUNDED. At most MAX_IN_FLIGHT calls are in flight; past that a call is
dropped and counted (``route_policy_dropped``), so a hung service costs at
most that many tasks, each ending after ROUTE_POLICY_TIMEOUT_S. A /decide
that is dropped stashes nothing, so its /observe is never sent either.
Nothing here raises into LiteLLM: every error is counted
(``route_policy_errors``) and logged, throttled. ROUTE_POLICY_ENABLED=false
(or an empty ROUTE_POLICY_URL) makes every entry point a no-op: no task, no
stash. See ai/litellm/LITELLM.md > Route-policy shadow mode.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from ..footer.auto import AUTO_MODEL, Served, is_claude
from ..footer.render import get_field, headers_of, to_int
from ..log import ThrottledLog, logger
from ..overflow.poller import Poller

DECIDE_PATH = "/decide"
OBSERVE_PATH = "/observe"
# data["metadata"][STASH_KEY] = {"decision_id", "litellm_call_id", "observed"}
STASH_KEY = "zeo_route_policy"
# Background calls in flight at once, /decide and /observe together.
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

# POST <url> <JSON body bytes> -> the HTTP status (raise on a transport error).
Post = Callable[[str, bytes], Awaitable[int]]
# (lower-cased response headers, request data) -> who answered.
ServedFn = Callable[[Dict[str, Any], Any], Served]
# chain alias -> "busy" | "down" | None.
OverflowFn = Callable[[Any], Optional[str]]


class HttpxPost:
    """POST a JSON body; return the HTTP status. One httpx.AsyncClient per
    event loop, like hooks/overflow/vllm.py's clients (an AsyncClient is bound
    to the loop it first ran on). The caller bounds the whole call with
    asyncio.wait_for -- httpx's own timeout is per operation. No auth: the
    service sits on ai_shared without any, like the classifier."""

    def __init__(self, timeout: float, transport: Any = None) -> None:
        self._timeout = timeout
        self._transport = transport  # tests: an httpx.MockTransport
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def __call__(self, url: str, body: bytes) -> int:
        import httpx  # LiteLLM depends on it; imported lazily for the tests

        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            kwargs: Dict[str, Any] = {"timeout": self._timeout}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
            self._loop = loop
        resp = await self._client.post(url, content=body, headers={"content-type": "application/json"})
        return resp.status_code


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


def metadata_of(data: Any) -> Optional[Dict[str, Any]]:
    """data["metadata"] when it is a dict, else None."""
    meta = data.get("metadata") if isinstance(data, dict) else None
    return meta if isinstance(meta, dict) else None


def stash_of(data: Any) -> Optional[Dict[str, Any]]:
    """The pre-call hook's stash, or None (no decision was sent for this request)."""
    meta = metadata_of(data)
    stash = meta.get(STASH_KEY) if meta is not None else None
    return stash if isinstance(stash, dict) and isinstance(stash.get("decision_id"), str) else None


def request_ids(data: Any) -> Dict[str, Optional[str]]:
    """chat_id / message_id / user_id from the request's Open WebUI headers
    (data["metadata"]["headers"], any case); None when absent or empty."""
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


def snapshot_messages(messages: List[Any]) -> List[Any]:
    """A shallow copy of each message dict: the request goes on being
    processed while the /decide task waits for its turn, and a key replaced
    in a message meanwhile must not change what was sent. O(messages)."""
    return [dict(m) if isinstance(m, dict) else m for m in messages]


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
        "messages": snapshot_messages(data["messages"]),
        "tools": list(tools) if isinstance(tools, list) and tools else None,
        "backends": backends,
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


def error_text(exc: Any) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:ERROR_MAX_CHARS]


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


# -- The client -----------------------------------------------------------------
class PolicyClient:
    """Fires /decide on `auto` requests and /observe after them; never awaits,
    never raises. `served` / `overflow_reason` are the footer's derivations
    (AutoFooter.served / .overflow_reason); `poller` supplies `backends`."""

    def __init__(
        self,
        enabled: bool,
        base_url: str,
        timeout: float,
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
        self.max_in_flight = max(1, int(max_in_flight))
        self.stats = stats
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

    # -- pre-call ----------------------------------------------------------------
    def decide(self, data: Any, user_api_key_dict: Any) -> bool:
        """On an `auto` chat request: make a decision id, fire /decide, and
        stash the id in data["metadata"] for the post-call hooks. Changes
        nothing else in `data`. True when a /decide was fired."""
        if not self.enabled:
            return False
        try:
            if not isinstance(data, dict) or data.get("model") != AUTO_MODEL:
                return False
            if not isinstance(data.get("messages"), list):
                return False  # not a chat completion (responses API, ...)
            meta = data.get("metadata")
            if meta is None:
                meta = data["metadata"] = {}
            if not isinstance(meta, dict):
                return False
            # Never trust a stash that arrived in the request body: the
            # proxy keeps client metadata keys it does not know.
            meta.pop(STASH_KEY, None)
            decision_id = str(uuid.uuid4())
            body = build_decide_body(decision_id, data, user_api_key_dict, self._poller.report(self._clock()))
            if not self._fire(DECIDE_PATH, body):
                return False
            meta[STASH_KEY] = {
                "decision_id": decision_id,
                "litellm_call_id": body["litellm_call_id"],
                "observed": False,
            }
            return True
        except Exception as exc:
            self._error("decide", exc)
            return False

    # -- post-call ---------------------------------------------------------------
    def observe_response(self, data: Any, response: Any) -> None:
        """Non-streaming success: /observe from the response object."""
        stash = stash_of(data) if self.enabled else None
        if stash is None or stash.get("observed"):
            return
        try:
            choices = get_field(response, "choices")
            choices = choices if isinstance(choices, list) else []
            finish: Optional[str] = None
            names: List[str] = []
            for choice in sorted(choices, key=lambda c: to_int(get_field(c, "index"))):
                if finish is None:
                    finish = _str_or_none(get_field(choice, "finish_reason"))
                names.extend(tool_call_names(get_field(choice, "message")))
            self._observe(data, stash, SUCCESS, headers_of(response), _str_or_none(get_field(response, "id")),
                          finish, names, None)
        except Exception as exc:
            self._error("observe", exc)

    def watch_stream(self, data: Any, wrapper: Any) -> Optional["StreamWatch"]:
        """A StreamWatch for a streamed response that has a decision to
        observe, else None (the stream is then not watched at all)."""
        stash = stash_of(data) if self.enabled else None
        if stash is None or stash.get("observed"):
            return None
        return StreamWatch(self, data, stash, wrapper)

    def observe_failure(self, data: Any, exc: Any) -> None:
        """post_call_failure_hook: /observe with status failure."""
        stash = stash_of(data) if self.enabled else None
        if stash is None or stash.get("observed"):
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
        served = Served(chain=None, model=None, bypassed=False)
        if status == SUCCESS or headers:
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

    # -- background calls ---------------------------------------------------------
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
            status = await asyncio.wait_for(self._post(self.base_url + path, encode(body)), timeout=self.timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._failed(path, f"no answer in {self.timeout:g}s")
            return
        except Exception as exc:
            self._failed(path, error_text(exc))
            return
        if isinstance(status, int) and 200 <= status < 300:
            self.stats["route_policy_decides" if path == DECIDE_PATH else "route_policy_observes"] += 1
            if path not in self._announced:  # one positive line per path per process
                self._announced.add(path)
                try:
                    logger.info("[route-policy] first POST %s%s answered HTTP %d -- shadow mode is live",
                                self.base_url, path, status)
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
    delta.tool_calls, in order."""

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
        if self.stash.get("observed"):
            return
        try:
            self.client._observe(self.data, self.stash, SUCCESS, self._headers(), self.response_id,
                                 self._finish_reason(), list(self.tool_calls), None)
        except Exception as exc:
            self.client._error("observe", exc)

    def failed(self, exc: BaseException) -> None:
        """The upstream stream raised (the proxy then also calls
        post_call_failure_hook; the stash makes that one a no-op)."""
        self._end(FAILURE, error_text(exc))

    def closed(self) -> None:
        """The generator was closed before the stream ended -- the client
        went away (the proxy closes the outermost generator on disconnect;
        this one sees GeneratorExit when it is collected). No-op once observed."""
        self._end(FAILURE, DISCONNECTED)

    def _end(self, status: str, error: str) -> None:
        if self.stash.get("observed"):
            return
        try:
            self.client._observe(self.data, self.stash, status, self._headers(), self.response_id,
                                 self._finish_reason(), list(self.tool_calls), error)
        except Exception as err:
            self.client._error("observe", err)
