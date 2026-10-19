"""The two invariants no ruleset can override (ROUTE_POLICY_PLAN § 2).

1. **A request for local-private stays local-private.** Whatever the engine
   chose, ``requested_model == "local-private"`` returns ``local-private``.
   PII routing may only ever be made stricter by the engine, never looser.
2. **The route is one of the ruleset's chain aliases.** A combined route that
   is not in ``routes`` (only possible through a bug, or a phase-4 evaluator
   naming its own route) is replaced by the fail-safe route -- ``local-private``
   when the ruleset lists it, else ``default_route`` -- and the decision may
   not be applied.

The LiteLLM hook enforces its own copy of both before it rewrites a request
(phase 3, ``ai/litellm/hooks/routing/invariants.py``); this module is pure
and stdlib-only so the two copies can be tested against the same cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

PRIVATE_ROUTE = "local-private"


@dataclass(frozen=True)
class Checked:
    route: str
    allowed: bool                   # False: the decision must not be applied
    overrides: Tuple[str, ...] = ()  # metric labels: private | not_a_chain
    notes: Tuple[str, ...] = ()


def fail_safe_route(routes: Sequence[str], default_route: str) -> str:
    return PRIVATE_ROUTE if PRIVATE_ROUTE in routes else default_route


def enforce(requested_model: str, route: str, routes: Sequence[str], default_route: str) -> Checked:
    """Apply both invariants to the combined ``route``."""
    if requested_model == PRIVATE_ROUTE:
        if route == PRIVATE_ROUTE:
            return Checked(route, True)
        return Checked(PRIVATE_ROUTE, True, ("private",),
                       (f"requested {PRIVATE_ROUTE} stays {PRIVATE_ROUTE} (the ruleset chose {route})",))
    if route not in routes:
        safe = fail_safe_route(routes, default_route)
        return Checked(safe, False, ("not_a_chain",),
                       (f"{route!r} is not one of this ruleset's chain aliases; {safe} instead, not applied",))
    return Checked(route, True)
