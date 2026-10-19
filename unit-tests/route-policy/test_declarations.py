"""Every evaluator type's ``detail`` matches its declaration.

The classifier's standard (unit-tests/classifier/test_result_specs.py), for
route-policy: each type's output is declared beside its code (``@declares``
in ``route_policy/evaluators/*.py``) and served by GET /evaluator-types.
This file runs every REAL registered evaluator through cases that vote,
abstain and hit the edge paths, and checks each ``detail``:

  * no key outside the declaration; every always-present key present;
  * every value of its declared kind (or null where declared nullable);
  * every declared key shows up in at least one case -- a declaration may
    not promise a key nothing produces.

A type with no cases here fails ``test_every_registered_type_has_cases``,
so a new evaluator cannot land without being checked.
"""

from __future__ import annotations

import asyncio

import pytest

from rp_helpers import ROOT, assistant, context, tool_round, user

from route_policy.evaluators.base import Outcome, registry

import yaml

CODE_RULE = yaml.safe_load((ROOT / "ai/route-policy/rulesets/patterns/code.yaml").read_text(encoding="utf-8"))
SOLO = "http://qwen3.8-solo:8000/v1"

# type -> (config, extra siblings for bind, [(messages, backends), ...])
CASES = {
    "regex_text": ({"rule": CODE_RULE, "patterns_ref": "patterns/code.yaml"}, {}, [
        ([user("write a python script")], None),
        ([user("hello")], None),
        ([], None),
    ]),
    "tool_calls": ({"pattern": "sandbox-mcp_.+"}, {}, [
        ([user("go"), *tool_round("sandbox-mcp_exec")], None),
        ([user("go"), *tool_round("search_web")], None),
        ([user("go")], None),
    ]),
    "cooldown": ({"exchanges": 1, "relevant_if": ["judge"]}, {"judge": ("tool_calls", {"pattern": "run_command"}), }, [
        ([user("go"), *tool_round("run_command")], None),
        ([user("go"), *tool_round("run_command"), user("next")], None),
        ([user("go"), *tool_round("run_command"), user("a"), assistant(), user("b")], None),
        ([user("plain")], None),
        ([], None),
    ]),
    "backend_load": ({"backends": ["solo"]}, {}, [
        ([user("x")], {SOLO: {"verdict": "busy", "waiting": 3.0}}),
        ([user("x")], {SOLO: {"verdict": "ok"}}),
        ([user("x")], None),
    ]),
    "static": ({}, {}, [([user("x")], None)]),
}


def _build(type_name: str, config: dict, siblings: dict):
    types = registry()
    built = {}
    for sid, (stype, sconfig) in siblings.items():
        cls = types[stype]
        built[sid] = cls(sid, cls.Config.model_validate(sconfig))
    cls = types[type_name]
    evaluator = cls("under-test", cls.Config.model_validate(config))
    evaluator.bind({**built, "under-test": evaluator})
    return evaluator


def _outcomes(type_name: str):
    config, siblings, cases = CASES[type_name]
    evaluator = _build(type_name, config, siblings)
    for messages, backends in cases:
        out = asyncio.run(evaluator.evaluate(context(messages, backends=backends)))
        assert isinstance(out, Outcome)
        yield out


def test_every_registered_type_has_cases():
    assert set(registry()) == set(CASES)


@pytest.mark.parametrize("type_name", sorted(CASES))
def test_detail_matches_the_declaration(type_name):
    spec = registry()[type_name].output
    seen = set()
    outcomes = list(_outcomes(type_name))
    for out in outcomes:
        assert spec.problems(out.detail) == [], (type_name, out.reason, out.detail)
        seen |= set(out.detail)
    assert seen == set(spec.fields), f"{type_name}: declared but never produced: {set(spec.fields) - seen}"
    # Every case set has at least one vote and (where the type can) one abstain.
    assert any(o.voted for o in outcomes), type_name
    if type_name != "static":
        assert any(not o.voted for o in outcomes), type_name


@pytest.mark.parametrize("type_name", sorted(CASES))
def test_relevance_is_declared_exactly_when_offered(type_name):
    cls = registry()[type_name]
    assert (cls.output.relevance is not None) == cls.supports_relevance(), type_name


def test_problems_catches_each_kind_of_mismatch():
    spec = registry()["tool_calls"].output
    assert spec.problems({"scope": "x", "pattern": "p", "called": [], "matched": None}) == []
    assert any("undeclared" in p for p in spec.problems({"scope": "x", "pattern": "p", "called": [],
                                                         "matched": None, "extra": 1}))
    assert any("missing" in p for p in spec.problems({"scope": "x", "pattern": "p", "called": []}))
    assert any("declared array" in p for p in spec.problems({"scope": "x", "pattern": "p", "called": "no",
                                                             "matched": None}))
    cooldown = registry()["cooldown"].output
    assert any("declared integer" in p for p in cooldown.problems({
        "exchanges": True, "relevant_if": [], "relevant_now": False, "last_relevant_ago": None,
        "irrelevant_since": None, "why": None, "expired": False}))
    assert any("not declared nullable" in p for p in cooldown.problems({
        "exchanges": 1, "relevant_if": [], "relevant_now": None, "last_relevant_ago": None,
        "irrelevant_since": None, "why": None, "expired": False}))


def test_evaluator_types_describe_config_schema_and_output():
    for name, cls in registry().items():
        described = cls.describe()
        assert described["config_schema"]["type"] == "object", name
        assert set(described["output"]) == {"summary", "votes", "relevance", "detail", "notes"}, name
        assert set(described["output"]["detail"]) == set(cls.output.fields), name
