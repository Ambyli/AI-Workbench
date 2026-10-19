"""The hook's own copy of the route-policy invariants (ROUTE_POLICY_PLAN § 2).

The service enforces them in ``ai/route-policy/route_policy/engine/invariants.py``
before it answers ``/decide``; the hook checks them AGAIN before it rewrites a
request, whatever the service said, so a bug or a hostile answer on the wire
can never move a request somewhere unsafe:

1. **A request for local-private stays local-private.** PII routing may only
   ever be made stricter, never looser. (The hook asks only about ``auto``, so
   this one is defensive -- it is here so the two copies agree case for case.)
2. **The route is one of the four chain aliases** -- ``local-general``,
   ``local-code``, ``local-reasoning``, ``local-private``. Anything else (a
   concrete alias such as ``claude-sonnet-5``, ``auto`` itself, a typo, a
   non-string) becomes ``local-private``, and the decision is not applied.

Unlike the service's copy, the allowed routes are FIXED here, not taken from
the ruleset: the hook never routes outside the four chains, whatever a ruleset
lists. ``unit-tests/litellm/test_hook_invariants.py`` loads both files by path
and runs them over the same cases. Pure and stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence, Tuple

PRIVATE_ROUTE = "local-private"
# The four chain aliases in ai/litellm/litellm_config.yaml -- the only routes
# the hook will ever rewrite `auto` to. test_hook_invariants.py checks this
# against the config's model groups.
CHAIN_ROUTES: Tuple[str, ...] = ("local-general", "local-code", "local-reasoning", PRIVATE_ROUTE)


@dataclass(frozen=True)
class Checked:
    route: str
    allowed: bool                   # False: the decision must not be applied
    overrides: Tuple[str, ...] = ()  # private | not_a_chain (the service's labels)
    notes: Tuple[str, ...] = ()


def fail_safe_route(routes: Sequence[str] = CHAIN_ROUTES, default_route: str = PRIVATE_ROUTE) -> str:
    """local-private when it is an allowed route (always, in the hook)."""
    return PRIVATE_ROUTE if PRIVATE_ROUTE in routes else default_route


def enforce(requested_model: Any, route: Any) -> Checked:
    """Apply both invariants to the route the engine returned. Never raises."""
    if requested_model == PRIVATE_ROUTE:
        if route == PRIVATE_ROUTE:
            return Checked(PRIVATE_ROUTE, True)
        return Checked(PRIVATE_ROUTE, True, ("private",),
                       (f"requested {PRIVATE_ROUTE} stays {PRIVATE_ROUTE} (the engine chose {route!r})",))
    if not isinstance(route, str) or route not in CHAIN_ROUTES:
        safe = fail_safe_route()
        return Checked(safe, False, ("not_a_chain",),
                       (f"{route!r} is not one of the chain aliases; {safe} instead, not applied",))
    return Checked(route, True)
