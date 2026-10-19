"""Shared helpers for the route-policy tests: conversation builders, the
baked-in rulesets, a synchronous engine runner, and the app with no database.

Imported by the test modules (conftest.py puts this directory on sys.path).
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "route-policy"
RULESETS_DIR = SERVICE_DIR / "rulesets"

# The API contract (ROUTE_POLICY_PLAN § 7): exact key sets.
DECIDE_KEYS = {"decision_id", "route", "mode", "apply", "ruleset", "votes", "scores", "latency_ms"}
VOTE_KEYS = {"evaluator", "type", "route", "weight", "confidence", "role", "abstained", "reason", "latency_ms"}


def user(text: Any) -> dict:
    return {"role": "user", "content": text}


def assistant(text: str = "ok") -> dict:
    return {"role": "assistant", "content": text}


def system(text: str = "You are helpful.") -> dict:
    return {"role": "system", "content": text}


def tool_round(name: str, call_id: str = "c1") -> List[dict]:
    """An assistant tool call to ``name`` and its result."""
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "done"},
    ]


def seed():
    from route_policy.rulesets.loader import load_seed

    return load_seed(RULESETS_DIR)


def default_ruleset():
    (ruleset,) = [r for r in seed().compiled if r.id == "default"]
    return ruleset


def compile_doc(doc: Dict[str, Any]):
    from route_policy.rulesets.loader import resolve_refs
    from route_policy.rulesets.validate import compile_ruleset

    return compile_ruleset(resolve_refs(doc, RULESETS_DIR))


def context(messages: List[Any], requested_model: str = "auto", tools=None,
            backends: Optional[Dict[str, Dict[str, Any]]] = None):
    from route_policy.engine.context import BackendView, RequestContext

    views = {base: BackendView(base, **b) for base, b in (backends or {}).items()}
    return RequestContext(requested_model=requested_model, messages=messages, tools=tools, backends=views)


def run(ruleset, messages: List[Any], **kw):
    from route_policy.engine.pipeline import run as run_pipeline

    return asyncio.run(run_pipeline(ruleset, context(messages, **kw)))


def evaluate(evaluator, messages: List[Any], **kw):
    """One evaluator's Outcome on a conversation."""
    return asyncio.run(evaluator.evaluate(context(messages, **kw)))


def dbless_app():
    """The app with the baked-in rulesets preloaded and NO database: the
    lifespan touches nothing, writes are skipped (decide) or 503 (observe)."""
    from route_policy.app import create_app
    from route_policy.rulesets.store import RulesetStore
    from route_policy.service import Service

    s = seed()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    store.set_bindings(s.bindings)
    return create_app(service=Service(store=store, database=None, rulesets_dir=RULESETS_DIR),
                      instrument=False)
