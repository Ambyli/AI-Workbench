"""The two copies of the route-policy invariants agree, case for case.

The service enforces them before it answers /decide
(ai/route-policy/route_policy/engine/invariants.py); the LiteLLM hook enforces
its own copy again before it rewrites an `auto` request
(ai/litellm/hooks/routing/invariants.py). Neither can import the other (the
hook runs in the LiteLLM container with only its own mounts), so this test
loads BOTH files by path and runs them over the same cases, with the
service's `routes` set to the four chain aliases -- the hook's fixed set.

Also: the hook's chain set IS the config's four chain model groups, and the
hook copy refuses everything the service copy would only refuse for a
narrower ruleset (a concrete alias, `auto`, a non-string).

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_COPY = _ROOT / "ai" / "litellm" / "hooks" / "routing" / "invariants.py"
SERVICE_COPY = _ROOT / "ai" / "route-policy" / "route_policy" / "engine" / "invariants.py"
CONFIG = _ROOT / "ai" / "litellm" / "litellm_config.yaml"

CHAINS = ("local-general", "local-code", "local-reasoning", "local-private")


def _load(path: pathlib.Path, name: str):
    """Load a stdlib-only module from its file. Registered in sys.modules
    while it executes (a @dataclass needs that), removed afterwards."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


hook = _load(HOOK_COPY, "_zeo_hook_invariants")
service = _load(SERVICE_COPY, "_zeo_service_invariants")

REQUESTED = ("auto", "local-private", "local-general", "local-code", "qwen3.8-solo", "", None)
ROUTES = CHAINS + ("claude-sonnet-5", "claude-opus-5-5", "auto", "qwen3.8-solo", "local-vision", "LOCAL-CODE",
                   " local-code", "", None)


def test_both_copies_expose_the_same_names():
    for name in ("PRIVATE_ROUTE", "Checked", "enforce", "fail_safe_route"):
        assert hasattr(hook, name) and hasattr(service, name), name
    assert hook.PRIVATE_ROUTE == service.PRIVATE_ROUTE == "local-private"
    assert hook.CHAIN_ROUTES == CHAINS


@pytest.mark.parametrize("requested", REQUESTED)
@pytest.mark.parametrize("route", ROUTES)
def test_the_two_copies_agree(requested, route):
    """Same route, same allowed, same override labels -- with the service's
    ruleset listing exactly the four chains (the `default` ruleset does)."""
    ours = hook.enforce(requested, route)
    theirs = service.enforce(requested, route, CHAINS, "local-general")
    assert (ours.route, ours.allowed, ours.overrides) == (theirs.route, theirs.allowed, theirs.overrides), (
        requested, route, ours, theirs)


@pytest.mark.parametrize("route", CHAINS)
def test_a_chain_alias_is_applied_as_is(route):
    checked = hook.enforce("auto", route)
    assert (checked.route, checked.allowed, checked.overrides) == (route, True, ())


@pytest.mark.parametrize("route", ["claude-sonnet-5", "auto", "qwen3.8-solo", "", None, 7, ["local-code"],
                                   {"route": "local-code"}])
def test_anything_else_is_local_private_and_not_applied(route):
    checked = hook.enforce("auto", route)
    assert (checked.route, checked.allowed, checked.overrides) == ("local-private", False, ("not_a_chain",))
    assert checked.notes  # says why, for the fallback log line


@pytest.mark.parametrize("route", CHAINS + ("claude-sonnet-5",))
def test_a_requested_local_private_stays_local_private(route):
    checked = hook.enforce("local-private", route)
    assert checked.route == "local-private" and checked.allowed is True


def test_the_hooks_routes_are_fixed_whatever_a_ruleset_lists():
    """The service accepts whatever its ruleset lists; the hook only the four
    chains. A ruleset that listed a concrete alias would pass the service and
    still be refused here."""
    wider = CHAINS + ("claude-sonnet-5",)
    assert service.enforce("auto", "claude-sonnet-5", wider, "local-general").allowed is True
    assert hook.enforce("auto", "claude-sonnet-5").allowed is False
    assert hook.fail_safe_route() == service.fail_safe_route(CHAINS, "local-general") == "local-private"


def test_the_hook_imported_normally_is_the_same_file():
    from hooks.routing import invariants

    assert pathlib.Path(invariants.__file__).resolve() == HOOK_COPY.resolve()
    assert invariants.CHAIN_ROUTES == hook.CHAIN_ROUTES


def test_the_chain_routes_are_the_configs_chain_groups():
    """Every allowed route is a model group with >= 2 `order` levels in
    litellm_config.yaml, and every such group is an allowed route."""
    yaml = pytest.importorskip("yaml")
    orders = {}
    for m in yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["model_list"]:
        order = (m.get("litellm_params") or {}).get("order")
        if order is not None:
            orders.setdefault(m["model_name"], set()).add(order)
    chains = {name for name, levels in orders.items() if len(levels) >= 2}
    assert chains == set(hook.CHAIN_ROUTES)
