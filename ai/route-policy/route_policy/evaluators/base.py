"""The evaluator plugin contract: base class, declared output, registry.

**The standard** (the classifier's ``@result_spec``, ai/classifier/analysis/
result_specs.py): an evaluator's ``detail`` shape is declared NEXT TO THE
CODE THAT PRODUCES IT and registers itself under the key consumers look it up
by -- here the ruleset's ``type``::

    @declares(OutputSpec(type="regex_text", fields={...}, ...))
    class RegexText(Evaluator):
        Config = RegexTextConfig          # pydantic, extra="forbid"
        async def evaluate(self, ctx): ...

``GET /evaluator-types`` serves every registered type's config schema and
declared output. ``unit-tests/route-policy/test_declarations.py`` runs every
real evaluator -- votes, abstains, edge cases -- and fails on an undeclared
``detail`` key, a missing always-present key, or a value of the wrong kind.
Adding a type means: a module here with ``@declares``, one line in
``_DECLARING_MODULES``, and cases in that test, in the same change.

**What an evaluator returns** is an :class:`Outcome`: it voted (for the
ruleset entry's ``vote.route``, unless the evaluator names its own) or it
abstained, with a human reason and a ``detail`` dict. It never sees roles,
weights or other evaluators' votes -- those belong to the pipeline and the
combiner. An evaluator never raises on odd input; if it does anyway, the
pipeline records an abstain with the error.

**Exchange relevance.** An evaluator that can judge ONE past exchange (its
user text and the rounds after it) overrides :meth:`Evaluator.exchange_relevance`;
the ``cooldown`` evaluator asks the evaluators its ``relevant_if`` names. That
is how ``sticky-code`` reuses ``code-tools`` and ``code-text`` instead of
repeating their patterns.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, List, Mapping, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel, ConfigDict

# -- Declared output ------------------------------------------------------------

# JSON kinds a detail value may have. bool is excluded from the numeric kinds
# on purpose (in Python it is an int).
KINDS = ("string", "number", "integer", "boolean", "array", "object")


def _is_kind(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return False


@dataclass(frozen=True)
class FieldSpec:
    """One ``detail`` key."""

    description: str
    kind: str
    nullable: bool = False          # the value may be null
    when: Optional[str] = None      # present only in this case; None = always present

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; expected one of {KINDS}")


@dataclass(frozen=True)
class OutputSpec:
    """What one evaluator type returns: when it votes, and its ``detail``."""

    type: str
    summary: str
    votes: str                                  # when it votes, and for which route
    fields: Dict[str, FieldSpec] = field(default_factory=dict)
    relevance: Optional[str] = None             # what one exchange's relevance means; None = not offered
    notes: Tuple[str, ...] = ()

    def required(self) -> set:
        return {k for k, f in self.fields.items() if f.when is None}

    def problems(self, detail: Any) -> List[str]:
        """Everything about ``detail`` that contradicts this declaration."""
        if not isinstance(detail, dict):
            return [f"{self.type}: detail is {type(detail).__name__}, not an object"]
        out = []
        for key in sorted(set(detail) - set(self.fields)):
            out.append(f"{self.type}: undeclared detail key {key!r}")
        for key in sorted(self.required() - set(detail)):
            out.append(f"{self.type}: always-present detail key {key!r} is missing")
        for key, spec in self.fields.items():
            if key not in detail:
                continue
            value = detail[key]
            if value is None:
                if not spec.nullable:
                    out.append(f"{self.type}: detail key {key!r} is null but not declared nullable")
            elif not _is_kind(value, spec.kind):
                out.append(f"{self.type}: detail key {key!r} is {type(value).__name__}, declared {spec.kind}")
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "votes": self.votes,
            "relevance": self.relevance,
            "detail": {
                key: {
                    "kind": f.kind,
                    "description": f.description,
                    **({"nullable": True} if f.nullable else {}),
                    **({"when": f.when} if f.when else {}),
                }
                for key, f in self.fields.items()
            },
            "notes": list(self.notes),
        }


# -- What an evaluator returns --------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    voted: bool
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    # None = the ruleset entry's vote.route. An evaluator that picks its own
    # route (phase 4's llm_classifier: `route: from_output`) sets it; the
    # pipeline still refuses a route the ruleset does not list.
    route: Optional[str] = None

    @classmethod
    def vote(cls, reason: str, detail: Dict[str, Any], *, confidence: float = 1.0,
             route: Optional[str] = None) -> "Outcome":
        return cls(True, reason, detail, confidence, route)

    @classmethod
    def abstain(cls, reason: str, detail: Dict[str, Any]) -> "Outcome":
        return cls(False, reason, detail)


# -- The base class ---------------------------------------------------------------


class EvaluatorConfig(BaseModel):
    """Base of every evaluator's ``config`` model: unknown keys are an error,
    so a typo in a ruleset fails validation instead of being ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Evaluator:
    """One configured evaluator in one compiled ruleset.

    Subclasses set ``Config`` and implement :meth:`evaluate`; ``@declares``
    sets ``type_name`` and ``output``.
    """

    type_name: ClassVar[str] = ""
    output: ClassVar[OutputSpec]
    Config: ClassVar[Type[EvaluatorConfig]] = EvaluatorConfig
    # True: the entry must carry vote.route, and a vote goes there. Every
    # phase-2 type works this way.
    needs_vote_route: ClassVar[bool] = True

    def __init__(self, evaluator_id: str, config: EvaluatorConfig) -> None:
        self.id = evaluator_id
        self.config = config

    def bind(self, siblings: Mapping[str, Optional["Evaluator"]]) -> None:
        """Resolve references to other evaluators of the same ruleset (every
        declared id, self included; None for one that failed validation).
        Raise ValueError on a bad reference; validation reports it as a
        ruleset error."""

    async def evaluate(self, ctx: Any) -> Outcome:  # ctx: engine.context.RequestContext
        raise NotImplementedError

    def exchange_relevance(self, text: str, rounds: List[dict]) -> Optional[str]:
        """Why one exchange (its user text, the messages after it) counts,
        or None. Only types whose ``output.relevance`` is set implement it."""
        raise NotImplementedError(f"{self.type_name} does not judge single exchanges")

    @classmethod
    def supports_relevance(cls) -> bool:
        return cls.exchange_relevance is not Evaluator.exchange_relevance

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        """This type as GET /evaluator-types serves it."""
        return {
            "vote_route": "required" if cls.needs_vote_route else "chosen by the evaluator",
            "config_schema": cls.Config.model_json_schema(),
            "output": cls.output.as_dict(),
        }


# -- The registry ------------------------------------------------------------------

_REGISTRY: Dict[str, Type[Evaluator]] = {}

# The modules that declare a type, imported on first lookup -- so asking for
# the registry before anything imported the evaluators still finds them all.
# phase 4 adds llm_classifier; context_size and semantic_router are listed in
# ROUTE_POLICY_PLAN § 4 but not built.
_DECLARING_MODULES = (
    "route_policy.evaluators.regex_text",
    "route_policy.evaluators.tool_calls",
    "route_policy.evaluators.cooldown",
    "route_policy.evaluators.backend_load",
    "route_policy.evaluators.static",
)
_loaded = False

E = TypeVar("E", bound=Type[Evaluator])


def declares(spec: OutputSpec) -> Callable[[E], E]:
    """Class decorator: declare what an evaluator type returns, and register
    the class under ``spec.type``. The same class twice is fine; a different
    class for a registered type is a bug."""

    def attach(cls: E) -> E:
        if not (isinstance(cls, type) and issubclass(cls, Evaluator)):
            raise TypeError("@declares decorates an Evaluator subclass")
        existing = _REGISTRY.get(spec.type)
        if existing is not None and existing is not cls:
            raise ValueError(f"an evaluator type {spec.type!r} is already registered")
        cls.type_name = spec.type
        cls.output = spec
        _REGISTRY[spec.type] = cls
        return cls

    return attach


def registry() -> Dict[str, Type[Evaluator]]:
    """Every registered evaluator type, loading the declaring modules once."""
    global _loaded
    if not _loaded:
        for module in _DECLARING_MODULES:
            importlib.import_module(module)
        _loaded = True
    return dict(_REGISTRY)


def evaluator_type(name: str) -> Optional[Type[Evaluator]]:
    return registry().get(name)
