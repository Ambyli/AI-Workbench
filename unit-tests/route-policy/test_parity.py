"""The `default` ruleset routes like today's two steps, on conversation shapes.

The reference is built from the real sources, not restated here:

1. the semantic router: ai/semantic-router/config.yaml's ``decisions``,
   highest priority first, each a conjunction of keyword signals (compiled by
   common.keywordmatch, the Go router's semantics) on the LATEST user
   message, catch-all last;
2. the LiteLLM hook: when step 1 gave ``local-general``,
   ``hooks.routing.code_route.code_route_decision`` with the hook's default
   tool regex and cooldown and the router's code_kw.

The phase-2 success query in Trino measures the same thing on real traffic
(ROUTE_POLICY_PLAN § 7); this is the offline version. Skipped if the hooks
package cannot be imported.
"""

from __future__ import annotations

import sys

import pytest
import yaml

from rp_helpers import ROOT, assistant, default_ruleset, run, system, tool_round, user

from common.conversation import split_exchanges
from common.keywordmatch import compile_keyword_rule, find_keyword_rule

ROUTER = yaml.safe_load((ROOT / "ai" / "semantic-router" / "config.yaml").read_text(encoding="utf-8"))


def _reference():
    litellm_dir = str(ROOT / "ai" / "litellm")
    if litellm_dir not in sys.path:
        sys.path.append(litellm_dir)
    try:
        from hooks.routing.code_route import (  # noqa: PLC0415
            DEFAULT_CODE_ROUTE_COOLDOWN,
            DEFAULT_CODE_ROUTE_TOOL_REGEX,
            code_route_decision,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"ai/litellm/hooks is not importable here: {exc!r}")
    rules = {}
    decisions = sorted(ROUTER["routing"]["decisions"], key=lambda d: -d["priority"])
    for d in decisions:
        for cond in d["rules"]["conditions"]:
            rules[cond["name"]] = compile_keyword_rule(find_keyword_rule(ROUTER, cond["name"]))

    def route(messages):
        exchanges = split_exchanges(messages)
        latest = exchanges[-1][0] if exchanges else ""
        chosen = None
        for d in decisions:
            conds = d["rules"]["conditions"]
            if all(rules[c["name"]].matches(latest) for c in conds):
                chosen = d["modelRefs"][0]["model"]
                break
        if chosen == "local-general":
            remap, _ = code_route_decision(messages, DEFAULT_CODE_ROUTE_TOOL_REGEX, rules["code_kw"],
                                           DEFAULT_CODE_ROUTE_COOLDOWN)
            if remap:
                chosen = "local-code"
        return chosen

    return route


PLAIN = [user("what's the weather like"), assistant("sunny")]
CODING = [user("start"), *tool_round("sandbox-mcp_write_files")]
CONVERSATIONS = {
    "small talk": [system(), user("hello there")],
    "code keywords": [user("Make me a scientific calculator!")],
    "code fence": [user("```python\nprint(1)\n```")],
    "file name": [user("why does app.py fail")],
    "reasoning": [user("prove that the integral converges")],
    "code beats reasoning": [user("write a python function to solve this equation")],
    "privacy beats code": [user("store the customer address in a python dict")],
    "privacy beats tool history": CODING + [user("now add the credit card field")],
    "reasoning beats tool history": CODING + [user("derive the formula step by step")],
    "tool call this exchange": [user("go ahead"), *tool_round("run_command")],
    "tool call 1 ago": CODING + [user("Looks the same")],
    "tool call 3 ago": CODING + PLAIN + PLAIN + [user("ok")],
    "tool call 4 ago": CODING + PLAIN * 3 + [user("ok")],
    "code text 2 ago": [user("debug this traceback"), assistant()] + PLAIN + [user("thanks")],
    "unrelated tool": [user("look it up"), *tool_round("search_web"), user("and?")],
    "offered tools only": [user("hi")],
    "no user message": [system("you are a bot")],
    "multimodal": [user([{"type": "text", "text": "refactor this class"},
                         {"type": "image_url", "image_url": {"url": "data:,"}}])],
    "phrase literal": [user("show your work please")],
    "whole word literal": [user("I love physics")],
    "longer word is not the literal": [user("ask the physicists")],
}


@pytest.mark.parametrize("name", sorted(CONVERSATIONS))
def test_default_ruleset_matches_router_plus_remap(name):
    reference = _reference()
    messages = CONVERSATIONS[name]
    assert run(default_ruleset(), messages).route == reference(messages), name
