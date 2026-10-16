"""The overflow filter for LiteLLM chain aliases -- spill to the next `order`
when a local vLLM backend is queueing or down, or cannot fit the prompt.

WHY IT EXISTS. The chain aliases (local-general, local-code, local-reasoning,
local-private) are model groups whose deployments carry ``order: 1 / 2 / 3``.
Plain LiteLLM only moves past order 1 when order 1 FAILS -- a vLLM that is
merely saturated queues the request instead and the caller waits. The policy
here is "local first for cost, Claude only when BOTH local models are busy or
down", so something has to notice "busy" before the call is made. That is
this filter.

WHERE IT RUNS (LiteLLM v1.95.0, router.py::async_get_healthy_deployments,
10606-10740): cooldown filter -> ``async_callback_filter_deployments`` (10686,
which calls every CustomLogger's ``async_filter_deployments``, 7340-7387, i.e.
Handler.async_filter_deployments -> OverflowFilter.filter_with_context) ->
pre-call context check (10694, a backstop now -- see context_fit.py) -> order
filter (10716-10720, keeps only the lowest ``order`` left). Dropping a
deployment here therefore lets the order filter pick the next order level.
NOTE that 7366-7384 RE-RAISES any exception a filter throws, which would fail
the request -- so the handler never lets one out.

THE RULE, per request, for a group with >= 2 distinct ``order`` values (on
whatever the context-fit step has left):
  * drop every deployment with an ``api_base`` whose backend is BUSY or DOWN
    (state.py);
  * never drop a deployment without an ``api_base`` (the Claude deployments);
  * if that would leave nothing, stay local: keep the BUSY deployments (the
    request queues inside vLLM) or, if every one is DOWN, return the list
    unchanged. For local-private, which has no Claude deployment, that is
    what keeps PII on the box.

Debugging: every spill decision logs one INFO line on the ``zeo.overflow``
logger (throttled per alias/deployment/reason), and ``handler.state()``
returns a JSON-able snapshot of what the hook believes about each backend.
See ai/litellm/LITELLM.md > Chain aliases and the overflow hook.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import deployments
from ..log import ThrottledLog
from .context_fit import ContextFit
from .poller import Poller
from .state import BUSY, DOWN, TOO_LONG


class OverflowFilter:
    """Context fit, then the busy/down rule, on chain groups only."""

    def __init__(
        self,
        poller: Poller,
        context_fit: ContextFit,
        clock: Callable[[], float],
        stats: Dict[str, int],
        log: ThrottledLog,
    ) -> None:
        self.poller = poller
        self.context_fit = context_fit
        self.stats = stats
        self._clock = clock
        self._log = log
        # chain alias -> the api_bases of its local deployments, learnt in
        # filter_deployments; the `auto` footer reads their verdicts.
        self._chain_backends: Dict[str, Tuple[str, ...]] = {}

    # -- Context fit, then the busy/down rule ------------------------------------
    async def filter_with_context(
        self,
        model: str,
        healthy_deployments: List,
        messages: Optional[List] = None,
        request_kwargs: Optional[dict] = None,
    ) -> List:
        """The whole per-request rule: drop too-long local deployments (hard),
        then apply the busy/down rule (filter_deployments) to what is left."""
        if not deployments.is_chain(healthy_deployments) or not isinstance(messages, list) or not messages:
            return self.filter_deployments(model, healthy_deployments)
        self.learn(model, healthy_deployments)
        too_long = await self.context_fit.check(model, healthy_deployments, messages, request_kwargs or {})
        if not too_long:
            return self.filter_deployments(model, healthy_deployments)

        fitting = [d for i, d in enumerate(healthy_deployments) if i not in too_long]
        why = ", ".join(f"{deployments.label(healthy_deployments[i])}: {r}" for i, r in too_long.items())
        if not any(deployments.api_base(d) is not None for d in fitting):
            if not fitting:
                # local-private: nothing unpolled to spill to. Hand LiteLLM the
                # list unchanged -- vLLM refuses the prompt with its own error
                # and it never leaves the box.
                self._log(model, "*", "none-fit",
                          "%s: no deployment fits the prompt (%s); list unchanged -- the backend will refuse it",
                          model, why)
                return healthy_deployments
            # Past every local window: only the Claude deployment(s) can take it.
            self.stats["context_drops"] += 1
            self._log(model, "*", "none-fit-local",
                      "%s: no local deployment fits the prompt (%s); keeping only %s",
                      model, why, ", ".join(deployments.label(d) for d in fitting))
            return fitting

        self.stats["context_drops"] += 1
        result = self.filter_deployments(model, fitting)
        next_order = min((o for o in (deployments.order(d) for d in result) if o is not None), default=None)
        for i, reason in too_long.items():
            d = healthy_deployments[i]
            self._log(model, deployments.label(d), TOO_LONG, "%s: dropping %s (%s, %s) -> next order %s",
                      model, deployments.label(d), TOO_LONG, reason, next_order)
        return result

    # -- Core busy/down rule (sync, so it is trivially testable) -----------------
    def learn(self, model: str, chain: List) -> None:
        """Register every local backend of a chain with the poller."""
        bases: List[str] = []
        for d in chain:
            base = deployments.api_base(d)
            if base is not None:
                bases.append(base)
                self.poller.watch(base)
        if isinstance(model, str) and bases:
            # Union: a cooled-down deployment missing from this call is still in the chain.
            self._chain_backends[model] = tuple(dict.fromkeys(self._chain_backends.get(model, ()) + tuple(bases)))
        self.poller.ensure_running()

    def filter_deployments(self, model: str, healthy_deployments: List) -> List:
        if not deployments.is_chain(healthy_deployments):
            return healthy_deployments  # not a chain: leave plain groups alone
        self.learn(model, healthy_deployments)

        now = self._clock()
        kept: List = []
        dropped: List[Tuple[dict, str]] = []
        for d in healthy_deployments:
            base = deployments.api_base(d)
            if base is None:
                kept.append(d)  # Claude (or anything unpolled): never dropped
                continue
            verdict = self.poller.verdict(base, now)
            if verdict in (BUSY, DOWN):
                dropped.append((d, verdict))
            else:
                kept.append(d)

        if not dropped:
            return healthy_deployments
        if not kept:
            # Nothing unpolled to spill to (local-private): stay local. Prefer
            # queueing on a live-but-busy backend over calling a dead one;
            # with everything down, hand LiteLLM the list unchanged.
            self.stats["kept_all_overloaded"] += 1
            busy_only = [d for d, v in dropped if v == BUSY]
            result = busy_only if busy_only else healthy_deployments
            self._log(model, "*", "all-overloaded",
                      "%s: every deployment is busy/down (%s); keeping %s -- request stays local and queues",
                      model, ", ".join(f"{deployments.label(d)}={v}" for d, v in dropped),
                      "the busy ones" if busy_only else "all")
            return result

        self.stats["spills"] += 1
        next_order = min((o for o in (deployments.order(d) for d in kept) if o is not None), default=None)
        for d, verdict in dropped:
            st = self.poller.backends[deployments.api_base(d) or ""]
            self._log(model, deployments.label(d), verdict,
                      "%s: dropping %s (%s, waiting=%s, err=%s) -> next order %s",
                      model, deployments.label(d), verdict, st.waiting, st.error or "-", next_order)
        return kept

    # -- For the `auto` footer ---------------------------------------------------
    def local_verdicts(self, chain: str, find_deployments: Callable[[str], Any]) -> List[str]:
        """The CURRENT verdicts of a chain's local backends -- the ones learnt
        from its requests, else the api_bases `find_deployments(chain)` lists.
        Backends never watched are left out. May raise; the caller guards."""
        bases = self._chain_backends.get(chain)
        if not bases:
            found = find_deployments(chain)
            bases = tuple(b for b in (deployments.api_base(d) for d in (found or [])) if b)
        now = self._clock()
        return [self.poller.verdict(b, now) for b in bases if b in self.poller.backends]
