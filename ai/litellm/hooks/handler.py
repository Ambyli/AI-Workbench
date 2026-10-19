"""Handler -- the one CustomLogger LiteLLM calls, wiring the four jobs.

Every hook LiteLLM dispatches on is defined DIRECTLY in Handler's class body,
and each only delegates:

    async_filter_deployments                -> OverflowFilter.filter_with_context
    async_pre_call_hook                     -> CodeRoute.apply, AutoFooter.strip_incoming,
                                               PolicyClient.decide
    async_post_call_success_hook            -> AutoFooter.decorate, PolicyClient.observe_response
    async_post_call_streaming_iterator_hook -> FooterStream.before / .finish, StreamWatch
    async_post_call_failure_hook            -> PolicyClient.observe_failure

That placement is load-bearing: the proxy decides once per callback list
whether to call async_post_call_streaming_iterator_hook and
async_pre_call_hook by looking the NAME up in type(callback).__dict__
(proxy/utils.py:1718-1745 at v1.95.0) -- one inherited from a mixin or a base
class would silently never run. async_post_call_success_hook is called on
every CustomLogger in litellm.callbacks (proxy/utils.py:2404-2410) and a
non-None return replaces the response; async_post_call_failure_hook likewise
(proxy/utils.py:2136-2166), where a returned or raised HTTPException replaces
the client's error -- so it always returns None; async_filter_deployments is
called on every CustomLogger by the Router (router.py:7340-7387), which
RE-RAISES what it throws (7366-7384) -- so none of them ever raises.

The footer parts and the route-policy client are no-ops unless the client
asked for model `auto`, the remap is a no-op unless it asked for
local-general, and the filter touches only chain groups (>= 2 distinct
`order` values).
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

try:  # LiteLLM only treats CustomLogger instances as deployment filters.
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover -- litellm is always present in the image
    CustomLogger = object  # type: ignore[assignment,misc]

from . import settings
from .footer.auto import AutoFooter, proxy_router
from .footer.stream import FooterStream
from .log import ThrottledLog, logger
from .overflow.context_fit import ContextFit
from .overflow.filter import OverflowFilter
from .overflow.poller import Poller
from .overflow.vllm import Fetch, HttpxFetch, HttpxTokenize, Tokenize
from .routing.code_route import CodeRoute, compile_code_route_regex, load_code_text_signal, parse_code_route_cooldown
from .routing.policy_client import MAX_IN_FLIGHT, HttpxPost, PolicyClient, Post


def new_stats() -> Dict[str, int]:
    """The counters handler.state()["stats"] reports, shared by every job."""
    return {
        "spills": 0, "kept_all_overloaded": 0, "errors": 0, "poller_restarts": 0,
        "context_drops": 0, "tokenize_calls": 0, "tokenize_cache_hits": 0, "tokenize_errors": 0,
        "footers": 0, "footers_stripped": 0, "footer_errors": 0,
        "code_routes": 0, "code_route_cooldowns": 0, "code_route_errors": 0,
        "route_policy_decides": 0, "route_policy_observes": 0,
        "route_policy_errors": 0, "route_policy_dropped": 0,
    }


def _code_route_options(regex_opt: Any, cooldown_opt: Any, signal_opt: Any) -> Tuple[Any, int, Any]:
    """(regex, cooldown, text signal) from Handler's code_route_* arguments,
    logging what is wrong with them.

    regex:    "env" -> CODE_ROUTE_TOOL_REGEX as read at import; a str is
              compiled here; a compiled pattern is used as is; None disables.
    cooldown: "env" -> CODE_ROUTE_COOLDOWN as read at import; an int is used as is.
    signal:   "env" -> the router's code_kw from CODE_ROUTE_ROUTER_CONFIG; a str
              is a path to load; a KeywordRule is used as is; None = tool calls only.
    """
    if isinstance(regex_opt, str) and regex_opt == "env":
        regex, route_error = settings.CODE_ROUTE_TOOL_REGEX, settings.CODE_ROUTE_TOOL_REGEX_ERROR
    elif isinstance(regex_opt, str):
        regex, route_error = compile_code_route_regex(regex_opt)
    else:
        regex, route_error = regex_opt, ""
    if route_error:
        try:
            logger.warning("%s -- local-general -> local-code tool remap disabled", route_error)
        except Exception:
            pass
    if isinstance(cooldown_opt, str) and cooldown_opt == "env":
        cooldown, cooldown_error = settings.CODE_ROUTE_COOLDOWN, settings.CODE_ROUTE_COOLDOWN_ERROR
    else:
        cooldown, cooldown_error = parse_code_route_cooldown(cooldown_opt)
    signal_error = ""
    if isinstance(signal_opt, str):
        path = settings.CODE_ROUTE_ROUTER_CONFIG if signal_opt == "env" else signal_opt
        signal, signal_error = load_code_text_signal(path)
    else:
        signal = signal_opt
    for problem, consequence in ((cooldown_error, ""),
                                 (signal_error, " -- code cooldown judges exchanges by tool calls only")):
        if problem and regex is not None:
            try:
                logger.warning("%s%s", problem, consequence)
            except Exception:
                pass
    return regex, cooldown, signal


class Handler(CustomLogger):  # type: ignore[misc,valid-type]
    """The four jobs, as components sharing one `stats` dict and one log throttle:

        self.overflow    OverflowFilter (.poller, .context_fit)
        self.footer      AutoFooter
        self.code_route  CodeRoute
        self.policy      PolicyClient (route-policy shadow mode)

    Every argument has a default from hooks.settings (the environment); a
    test passes its own fetch / tokenize / post / clock instead of touching
    the network or the environment."""

    def __init__(
        self,
        fetch: Optional[Fetch] = None,
        clock: Callable[[], float] = time.monotonic,
        busy_after: float = settings.BUSY_AFTER_S,
        idle_after: float = settings.IDLE_AFTER_S,
        poll_interval: float = settings.POLL_INTERVAL_S,
        probe_timeout: float = settings.PROBE_TIMEOUT_S,
        start_poller: bool = True,
        auto_footer: Optional[bool] = None,
        router_getter: Optional[Callable[[], Any]] = None,
        tokenize: Optional[Tokenize] = None,
        tokenize_min_fraction: float = settings.TOKENIZE_MIN_FRACTION,
        tokenize_chars_per_token: float = settings.TOKENIZE_CHARS_PER_TOKEN,
        tokenize_timeout: float = settings.TOKENIZE_TIMEOUT_S,
        code_route_regex: Any = "env",
        code_route_cooldown: Any = "env",
        code_text_signal: Any = "env",
        route_policy: Optional[bool] = None,
        route_policy_url: str = settings.ROUTE_POLICY_URL,
        route_policy_timeout: float = settings.ROUTE_POLICY_TIMEOUT_S,
        policy_post: Optional[Post] = None,
        policy_max_in_flight: int = MAX_IN_FLIGHT,
    ) -> None:
        try:
            super().__init__()
        except Exception:  # pragma: no cover -- CustomLogger signature drift
            pass
        self.stats = new_stats()
        self._clock = clock
        log = ThrottledLog(clock)
        enabled = settings.AUTO_FOOTER_ENABLED if auto_footer is None else bool(auto_footer)
        regex, cooldown, signal = _code_route_options(code_route_regex, code_route_cooldown, code_text_signal)
        self.code_route = CodeRoute(regex, cooldown, signal, self.stats, log)
        poller = Poller(fetch or HttpxFetch(probe_timeout), clock, busy_after, idle_after,
                        poll_interval, probe_timeout, start_poller, self.stats)
        context_fit = ContextFit(tokenize or HttpxTokenize(tokenize_timeout), tokenize_min_fraction,
                                 tokenize_chars_per_token, tokenize_timeout, self.stats, log)
        self.overflow = OverflowFilter(poller, context_fit, clock, self.stats, log)
        self.footer = AutoFooter(enabled, router_getter or proxy_router, self.overflow, self.stats, log)
        policy_on = settings.ROUTE_POLICY_ENABLED if route_policy is None else bool(route_policy)
        self.policy = PolicyClient(policy_on, route_policy_url, route_policy_timeout,
                                   policy_post or HttpxPost(route_policy_timeout), poller,
                                   self.footer.served, self.footer.overflow_reason, clock, self.stats, log,
                                   policy_max_in_flight)

    # -- Router: deployment filter ----------------------------------------------
    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: List,
        messages: Optional[List] = None,
        request_kwargs: Optional[dict] = None,
        parent_otel_span: Any = None,
    ) -> List[dict]:
        # request_kwargs are the completion kwargs Router._acompletion was
        # called with (router.py:2757-2762 -> 10793 -> 10686 at v1.95.0): the
        # request's `tools`, `max_tokens`, `extra_body`, ... -- not yet merged
        # with the chosen deployment's litellm_params.
        try:
            return await self.overflow.filter_with_context(model, healthy_deployments, messages, request_kwargs)
        except Exception as exc:  # never fail a request because of this hook
            self.stats["errors"] += 1
            try:
                logger.warning("filter error for %s, deployments unchanged: %r", model, exc)
            except Exception:
                pass
            return healthy_deployments

    # -- Proxy: request / response hooks (MUST stay in this class body) ---------
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> Any:
        """Two independent edits and one background call, each guarded on its own:

        * local-general that looks like code -> local-code (CodeRoute.apply);
        * `auto`: strip footers we emitted from incoming assistant messages
          (AutoFooter.strip_incoming);
        * `auto`: fire /decide at the route-policy service, never awaited, and
          stash its decision id in data["metadata"] (PolicyClient.decide) --
          after the strip, so the service sees what the router sees.

        Returns `data` (modified in place; proxy/utils.py:913-917 keeps a
        returned dict) when either edit changed it, else None. The stash is
        an in-place edit of data["metadata"] and needs no return."""
        changed = self.code_route.apply(data)
        stripped = self.footer.strip_incoming(data)
        self.policy.decide(data, user_api_key_dict)
        return data if (changed or stripped) else None

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: Any, response: Any) -> Any:
        """Non-streaming: append the `auto` footer to each qualifying choice's
        message, then fire /observe (it reads nothing the footer changes)."""
        out = self.footer.decorate(data, response)
        self.policy.observe_response(data, response)
        return out

    async def async_post_call_failure_hook(
        self, request_data: dict, original_exception: Exception, user_api_key_dict: Any,
        traceback_str: Optional[str] = None,
    ) -> Any:
        """A failed `auto` call: fire /observe with status failure. Always None
        -- a returned HTTPException would replace the client's error."""
        self.policy.observe_failure(request_data, original_exception)
        return None

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict
    ) -> AsyncIterator[Any]:
        """Streaming, ONE wrapper for both stream jobs:

        * the `auto` footer, in front of each qualifying choice's finish chunk
          (or on its content, when the finish chunk carries text);
        * the route-policy /observe, fired when the upstream stream ends (or
          fails, or the generator is closed early -- a client disconnect).

        The proxy wraps the stream once (proxy_server.py:7413-7419) and writes
        `data: [DONE]` only after this generator ends. Upstream errors still
        propagate; only our own work is guarded."""
        try:
            active = self.footer.wanted(request_data)
        except Exception as exc:
            self.footer.error("stream", exc)
            active = False
        watch = self.policy.watch_stream(request_data, response)
        if not active and watch is None:
            async for chunk in response:
                yield chunk
            return
        stream = FooterStream(self.footer, response, request_data) if active else None
        try:
            async for chunk in response:
                if watch is not None:
                    watch.see(chunk)
                if stream is not None:
                    for extra in stream.before(chunk):
                        yield extra
                yield chunk
            if watch is not None:
                watch.succeeded()  # the upstream ended normally
            if stream is not None:
                for extra in stream.finish():
                    yield extra
        except Exception as exc:
            if watch is not None:
                watch.failed(exc)
            raise
        finally:
            if watch is not None:
                watch.closed()  # no-op once observed; else the client went away

    # -- Debugging ---------------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        """A JSON-able snapshot of what the hooks believe: config, counters and
        every backend's verdict. Lives in the proxy process (a REPL or a
        temporary log line), not an endpoint."""
        now = self._clock()
        poller, fit = self.overflow.poller, self.overflow.context_fit
        return {
            "config": {
                "busy_after_s": poller.busy_after,
                "idle_after_s": poller.idle_after,
                "poll_interval_s": poller.poll_interval,
                "probe_timeout_s": poller.probe_timeout,
                "stale_after_s": poller.stale_after,
                "tokenize_min_fraction": fit.min_fraction,
                "tokenize_chars_per_token": fit.chars_per_token,
                "tokenize_timeout_s": fit.timeout,
                "route_policy_enabled": self.policy.enabled,
                "route_policy_url": self.policy.base_url,
                "route_policy_timeout_s": self.policy.timeout,
                "route_policy_max_in_flight": self.policy.max_in_flight,
            },
            "tokenize_cache_entries": len(fit.cache),
            "route_policy_in_flight": self.policy.in_flight,
            "poller_running": poller.running(),
            "stats": dict(self.stats),
            "backends": poller.snapshot(now),
        }
