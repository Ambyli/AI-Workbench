"""Validate a resolved ruleset document and compile it.

``compile_ruleset(document)`` either returns a :class:`CompiledRuleset` or
raises :class:`RulesetInvalid` with EVERY problem found, each prefixed with
where it is (``evaluators[2] (code-tools).config.pattern: ...``):

* the document shape (``RulesetDocument``);
* ``routes`` unique and well formed, ``default_route`` one of them;
* evaluator ids unique; each ``type`` registered; each ``config`` valid for
  its type (``extra="forbid"`` -- a typo is an error, never ignored);
* ``vote.route`` present where the type needs it, and one of ``routes`` --
  so a ruleset can only ever vote for its own chain aliases; absent where
  the evaluator chooses the route itself (``llm_classifier``);
* routes a ``config`` names (``llm_classifier.criteria`` / ``otherwise``)
  are routes of the ruleset (``Evaluator.check_routes``);
* cross-references (``cooldown.relevant_if``) resolve;
* the combiner type registered and its options valid.

Refs (``patterns_ref``) must already be inlined (``rulesets.loader``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from ..combiners.base import combiner_registry
from ..evaluators.base import Evaluator, registry
from .model import ROUTE_PATTERN, CompiledEntry, CompiledRuleset, RulesetDocument, content_sha256


class RulesetInvalid(ValueError):
    def __init__(self, errors: List[str], ruleset: Optional[str] = None) -> None:
        self.errors = errors
        self.ruleset = ruleset
        where = f"ruleset {ruleset}" if ruleset else "ruleset"
        super().__init__(f"{where} is invalid: " + "; ".join(errors))


def _pydantic_errors(exc: ValidationError, prefix: str = "") -> List[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        where = f"{prefix}.{loc}" if prefix and loc else (prefix or loc or "document")
        out.append(f"{where}: {err.get('msg', 'invalid')}")
    return out


def compile_ruleset(document: Dict[str, Any], *, sha256: str = "") -> CompiledRuleset:
    """Validate ``document`` and build its evaluators and combiner."""
    name = None
    if isinstance(document, dict) and "id" in document:
        name = f"{document.get('id')}@{document.get('version')}"
    try:
        doc = RulesetDocument.model_validate(document)
    except ValidationError as exc:
        raise RulesetInvalid(_pydantic_errors(exc), name) from None

    errors: List[str] = []
    routes = list(doc.routes)
    if len(set(routes)) != len(routes):
        errors.append("routes: a route is listed twice")
    for route in routes:
        if not re.match(ROUTE_PATTERN, route):
            errors.append(f"routes: {route!r} is not a valid route name")
    if doc.default_route not in routes:
        errors.append(f"default_route: {doc.default_route!r} is not one of routes")

    types = registry()
    seen: Dict[str, int] = {}
    built: Dict[str, Evaluator] = {}
    entries: List[CompiledEntry] = []
    for i, entry in enumerate(doc.evaluators):
        where = f"evaluators[{i}] ({entry.id})"
        if entry.id in seen:
            errors.append(f"{where}: id is already used by evaluators[{seen[entry.id]}]")
            continue
        seen[entry.id] = i
        cls = types.get(entry.type)
        if cls is None:
            errors.append(f"{where}.type: unknown evaluator type {entry.type!r}; known: {sorted(types)}")
            continue
        try:
            config = cls.Config.model_validate(entry.config)
        except ValidationError as exc:
            errors.extend(_pydantic_errors(exc, f"{where}.config"))
            continue
        route = entry.vote.route
        if cls.needs_vote_route and route is None:
            errors.append(f"{where}.vote.route: required for a {entry.type} evaluator")
            continue
        if not cls.needs_vote_route and route is not None:
            errors.append(f"{where}.vote.route: a {entry.type} evaluator chooses its own route; "
                          "remove vote.route")
            continue
        if route is not None and route not in routes:
            errors.append(f"{where}.vote.route: {route!r} is not one of routes")
            continue
        evaluator = cls(entry.id, config)
        try:
            evaluator.check_routes(routes)
        except ValueError as exc:
            errors.append(f"{where}.config: {exc}")
            continue
        built[entry.id] = evaluator
        entries.append(CompiledEntry(entry.id, entry.type, entry.role, route, entry.vote.weight, evaluator))

    # Cross-references, once every evaluator exists (a reference may point
    # forward or back). An evaluator that failed above is offered as None, so
    # a reference to it is reported as "invalid", not "missing".
    siblings: Dict[str, Optional[Evaluator]] = {**{eid: None for eid in seen}, **built}
    for entry in entries:
        try:
            entry.evaluator.bind(siblings)
        except ValueError as exc:
            errors.append(f"evaluators[{seen[entry.id]}] ({entry.id}).config: {exc}")

    combiners = combiner_registry()
    combiner = None
    combiner_cls = combiners.get(doc.combiner.type)
    if combiner_cls is None:
        errors.append(f"combiner.type: unknown combiner {doc.combiner.type!r}; known: {sorted(combiners)}")
    else:
        try:
            combiner = combiner_cls(combiner_cls.Config.model_validate(doc.combiner.options()))
        except ValidationError as exc:
            errors.extend(_pydantic_errors(exc, "combiner"))

    if errors or combiner is None:
        raise RulesetInvalid(errors or ["combiner: could not be built"], name)

    return CompiledRuleset(
        id=doc.id,
        version=doc.version,
        mode=doc.mode,
        description=doc.description,
        routes=routes,
        default_route=doc.default_route,
        entries=entries,
        combiner=combiner,
        document=document,
        sha256=sha256 or content_sha256(document),
    )
