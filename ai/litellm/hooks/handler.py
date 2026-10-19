"""Handler -- the one CustomLogger LiteLLM calls, wiring the three jobs.

Every hook LiteLLM dispatches on is defined DIRECTLY in Handler's class body,
and each only delegates:

    async_filter_deployments                -> OverflowFilter.filter_with_context
    async_pre_call_hook                     -> AutoFooter.strip_incoming, PolicyClient.route
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

The footer and the route-policy client act only on requests for model `auto`
(the client rewrites them to a chain alias in the pre-call hook; see
routing/policy_client.py), and the filter touches only chain groups (>= 2
distinct `order` values).
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

try:  # LiteLLM only treats CustomLogger instances as deployment filters.
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover -- litellm is always present in the image
    CustomLogger = object  # type: ignore[assignment,misc]

from . import settings
from .footer.auto import AutoFooter
from .footer.stream import FooterStream
from .log import ThrottledLog, logger
from .overflow.context_fit import ContextFit
from .overflow.filter import OverflowFilter
from .overflow.poller import Poller
from .overflow.vllm import Fetch, HttpxFetch, HttpxTokenize, Tokenize
from .routing.policy_client import MAX_IN_FLIGHT, HttpxPost, PolicyClient, Post


def new_stats() -> Dict[str, int]:
    """The counters handler.state()["stats"] reports, shared by every job."""
    return {
        "spills": 0, "kept_all_overloaded": 0, "errors": 0, "poller_restarts": 0,
        "context_drops": 0, "context_auto_kept_local": 0,
        "tokenize_calls": 0, "tokenize_cache_hits": 0, "tokenize_errors": 0,
        "footers": 0, "footers_stripped": 0, "footer_errors": 0,
        "route_policy_decides": 0, "route_policy_applied": 0, "route_policy_fallbacks": 0,
        "route_policy_observes": 0, "route_policy_errors": 0, "route_policy_dropped": 0,
    }


class Handler(CustomLogger):  # type: ignore[misc,valid-type]
    """The three jobs, as components sharing one `stats` dict and one log throttle:

        self.overflow    OverflowFilter (.poller, .context_fit)
        self.footer      AutoFooter
        self.policy      PolicyClient (route-policy: `auto` -> a chain alias)

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
        tokenize: Optional[Tokenize] = None,
        tokenize_min_fraction: float = settings.TOKENIZE_MIN_FRACTION,
        tokenize_chars_per_token: float = settings.TOKENIZE_CHARS_PER_TOKEN,
        tokenize_timeout: float = settings.TOKENIZE_TIMEOUT_S,
        route_policy: Optional[bool] = None,
        route_policy_url: str = settings.ROUTE_POLICY_URL,
        route_policy_timeout: float = settings.ROUTE_POLICY_TIMEOUT_S,
        route_policy_decide_timeout: float = settings.ROUTE_POLICY_DECIDE_TIMEOUT_S,
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
        poller = Poller(fetch or HttpxFetch(probe_timeout), clock, busy_after, idle_after,
                        poll_interval, probe_timeout, start_poller, self.stats)
        context_fit = ContextFit(tokenize or HttpxTokenize(tokenize_timeout), tokenize_min_fraction,
                                 tokenize_chars_per_token, tokenize_timeout, self.stats, log)
        self.overflow = OverflowFilter(poller, context_fit, clock, self.stats, log)
        self.footer = AutoFooter(enabled, self.overflow, self.stats, log)
        policy_on = settings.ROUTE_POLICY_ENABLED if route_policy is None else bool(route_policy)
        self.policy = PolicyClient(policy_on, route_policy_url, route_policy_timeout, route_policy_decide_timeout,
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
        """On an `auto` request, in this order, each guarded on its own:

        * strip footers we emitted from incoming assistant messages
          (AutoFooter.strip_incoming) -- first, so the engine never reads them;
        * await /decide and rewrite data["model"] to the chosen chain alias, or
          to local-private when the engine cannot or may not decide
          (PolicyClient.route), and stash what happened in the proxy's
          metadata (routing/stash.py).

        Returns `data` (modified in place; proxy/utils.py:1454-1457 keeps a
        returned dict) when either changed it -- every `auto` request -- else
        None. Any other request is left alone apart from dropping a
        route-policy stash its body may have carried."""
        stripped = self.footer.strip_incoming(data)
        routed = await self.policy.route(data, user_api_key_dict)
        return data if (stripped or routed) else None

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
                "route_policy_decide_timeout_s": self.policy.decide_timeout,
                "route_policy_max_in_flight": self.policy.max_in_flight,
            },
            "tokenize_cache_entries": len(fit.cache),
            "route_policy_in_flight": self.policy.in_flight,
            "route_policy_fallbacks_by_reason": dict(self.policy.fallbacks),
            "poller_running": poller.running(),
            "stats": dict(self.stats),
            "backends": poller.snapshot(now),
        }
