"""The `default` ruleset stays a copy of what routes `auto` today.

Until phase 3 deletes the semantic router and the hook's code remap, the
same rules live in two places. These tests fail as soon as they drift:

* rulesets/patterns/{privacy,code,reasoning}.yaml == the router's
  privacy_kw / code_kw / reasoning_kw (keywords, operator, case_sensitive,
  method) in ai/semantic-router/config.yaml;
* the `code-tools` pattern and the `sticky-code` cooldown == the defaults of
  CODE_ROUTE_TOOL_REGEX / CODE_ROUTE_COOLDOWN as ai/litellm/hooks/settings.py
  reads them with those variables unset (in a subprocess, so the hooks
  package and its env read never touch this process);
* the evaluators reference exactly those files, in the router's priority
  order.

When one fails: change BOTH sides, then bump default.yaml's `version` (the
pattern files are part of its content hash).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
import yaml

from rp_helpers import ROOT, RULESETS_DIR, default_ruleset

from common.keywordmatch import find_keyword_rule

ROUTER_CONFIG = ROOT / "ai" / "semantic-router" / "config.yaml"
RULE_KEYS = ("keywords", "operator", "case_sensitive", "method")
PATTERNS = {"privacy": "privacy_kw", "code": "code_kw", "reasoning": "reasoning_kw"}


def _router_rule(name: str) -> dict:
    rule = find_keyword_rule(yaml.safe_load(ROUTER_CONFIG.read_text(encoding="utf-8")), name)
    assert rule is not None, f"{name} is gone from {ROUTER_CONFIG}"
    return rule


@pytest.mark.parametrize("stem, name", sorted(PATTERNS.items()))
def test_pattern_files_equal_the_router_rules(stem, name):
    ours = yaml.safe_load((RULESETS_DIR / "patterns" / f"{stem}.yaml").read_text(encoding="utf-8"))
    theirs = _router_rule(name)
    assert ours["name"] == name
    for key in RULE_KEYS:
        assert ours.get(key) == theirs.get(key), f"{stem}.yaml {key} differs from the router's {name}"
    # Nothing in either that the other ignores.
    assert set(ours) <= {"name", *RULE_KEYS}
    assert set(theirs) <= {"name", *RULE_KEYS}, f"the router's {name} gained {set(theirs) - {'name', *RULE_KEYS}}"


def _hook_defaults() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CODE_ROUTE_")}
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from hooks import settings as s; "
        "print(json.dumps({'regex': s.CODE_ROUTE_TOOL_REGEX.pattern, 'cooldown': s.CODE_ROUTE_COOLDOWN}))"
    )
    done = subprocess.run([sys.executable, "-c", script, str(ROOT / "ai" / "litellm")], env=env,
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        pytest.fail(f"ai/litellm/hooks/settings.py did not import: {done.stderr[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_tool_regex_and_cooldown_equal_the_hook_defaults():
    defaults = _hook_defaults()
    entries = {e.id: e for e in default_ruleset().entries}
    assert entries["code-tools"].evaluator.config.pattern == defaults["regex"]
    assert entries["code-tools"].evaluator.config.scope == "current_exchange"
    assert entries["sticky-code"].evaluator.config.exchanges == defaults["cooldown"]


def test_default_ruleset_wiring_mirrors_the_router_priorities():
    router = yaml.safe_load(ROUTER_CONFIG.read_text(encoding="utf-8"))
    decisions = sorted(router["routing"]["decisions"], key=lambda d: -d["priority"])
    order = []
    for d in decisions:
        names = [c["name"] for c in d["rules"]["conditions"]]
        order.append((names[0] if names else None, d["modelRefs"][0]["model"]))
    assert order == [("privacy_kw", "local-private"), ("code_kw", "local-code"),
                     ("reasoning_kw", "local-reasoning"), (None, "local-general")]
    ruleset = default_ruleset()
    keyword_entries = [(e.evaluator.rule_spec.name, e.route) for e in ruleset.entries if e.type == "regex_text"]
    assert keyword_entries == order[:3]
    assert ruleset.default_route == order[3][1]
    assert [e.id for e in ruleset.entries][:3] == ["privacy", "code-text", "reasoning-text"]
    assert ruleset.entries[0].role == "force"
    refs = [e["config"].get("patterns_ref") for e in ruleset.document["evaluators"][:3]]
    assert refs == ["patterns/privacy.yaml", "patterns/code.yaml", "patterns/reasoning.yaml"]
    # The cooldown judges in the hook's order: tool calls first, then code_kw.
    assert ruleset.entries[4].evaluator.config.relevant_if == ["code-tools", "code-text"]
