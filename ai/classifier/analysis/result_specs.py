"""What a criterion's ``detail`` contains — the declarations and their registry.

**The standard.** A result shape is declared NEXT TO THE CODE THAT PRODUCES IT
and registers itself under the key its consumers look it up by:

    cv detectors   ``@describes(DetectorSpec(...))`` on each detector function
                   (``cv.result``); the detector REGISTRY is the lookup;
                   served by ``GET /cv-detectors``
    criterion      ``@result_spec(ResultSpec(...))`` on each evaluator's
    types          ``evaluate`` (``analysis.llm_eval`` / ``text_eval`` /
                   ``detector_eval`` / ``cv_eval``); registered HERE by type
                   name, the key the aggregator, the scheduler and
                   ``GET /criterion-types`` start from (a result's ``method``)

Every declaration has a test that runs the real producer and compares:
``unit-tests/classifier/test_result_specs.py`` for the types,
``test_cv_measurements.py`` for the detectors.

This module holds what the declarations share — ``FieldSpec``,
``ResultSpec``, the three fields every type has (``METRIC_FIELD``,
``VALUE_FIELD``, ``AGGREGATE_FIELD``), the aggregate block — the registry, and
the helpers that read it. It defines no type's shape itself.

Two keys are common to every type, so a consumer can read the headline number
without knowing which type ran:

    metric   the name of the headline number
    value    that number

``metric_from`` says where it lives: a detail key (``text``: ``count``,
``detector``: ``best_score``), a ``cv`` measurement, or the ``llm`` score —
which is why a ``score: false`` llm criterion has ``value: null``
(``clear_judgement``).

**Aggregation** (``analysis.aggregate``) keeps the shape and adds one block,
``detail.aggregate`` (``AGGREGATE_BLOCK``), instead of replacing ``detail``:

    any / worst  the chosen member's detail, whole
    mean         only the type's STABLE keys (the ones that mean the same on
                 every member — the pattern searched, the thresholds used) plus
                 ``value`` = the mean of the members' values; per-member keys
                 (measurements, snippets, what text was sent) are in ``items[]``
    sum          (text) the full match record, counts added

A single member passes through with no aggregate block.

Process flow position: imported by the evaluators (to declare), by
``analysis.aggregate`` and ``analysis.scheduler`` (to look up), and by
``api.introspection`` for ``GET /criterion-types``. It imports no evaluator at
module level — they import it — so the lookups load them on first use
(``specs()``).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class FieldSpec:
    """One ``detail`` key."""

    description: str
    kind: str                       # JSON kind: number | integer | string | boolean | array | object
    stable: bool = False            # same meaning on every member → kept under `mean`
    when: Optional[str] = None      # present only in this case; None = always present


@dataclass(frozen=True)
class ResultSpec:
    type: str
    metric: str
    metric_from: str                # "detail" | "measurements" | "score"
    fields: dict[str, FieldSpec]
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        missing = {"metric", "value", "aggregate"} - set(self.fields)
        if missing:
            raise ValueError(f"{self.type}: every type declares {sorted(missing)}")
        if self.metric_from == "detail" and self.metric not in self.fields:
            raise ValueError(f"{self.type}: metric {self.metric!r} is not a declared field")

    def required(self) -> set[str]:
        return {k for k, f in self.fields.items() if f.when is None}

    def stable(self) -> set[str]:
        return {k for k, f in self.fields.items() if f.stable}

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "metric_from": self.metric_from,
            "fields": {
                key: {
                    "kind": f.kind,
                    "description": f.description,
                    **({"when": f.when} if f.when else {}),
                    **({"stable": True} if f.stable else {}),
                }
                for key, f in self.fields.items()
            },
            "notes": list(self.notes),
        }


# The three fields every type declares (ResultSpec refuses one without them).
METRIC_FIELD = FieldSpec("Name of the headline number", "string", stable=True)
VALUE_FIELD = FieldSpec(
    "The headline number (the mean of the members' values under `mean`)", "number"
)
AGGREGATE_FIELD = FieldSpec(
    "How several members were combined — see `aggregate_detail`", "object",
    when="the criterion ran on more than one item or document",
)

AGGREGATE_BLOCK: dict[str, str] = {
    "rule": "any | worst | all | mean | sum — the rule that combined the members",
    "level": "pages (one document's items) | documents (the request's documents)",
    "from": "the member whose detail this is (any / worst), else null",
    "values": "each member's value, by member label (`item n` / `document n`)",
}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, ResultSpec] = {}

# The modules that declare a type. Imported on first lookup, so a caller that
# reaches for a spec before anything imported the evaluators still gets all
# four — and a new type's module only has to be listed here once.
_DECLARING_MODULES = (
    "analysis.llm_eval",
    "analysis.text_eval",
    "analysis.detector_eval",
    "analysis.cv_eval",
)
_loaded = False


def register(spec: ResultSpec) -> ResultSpec:
    """Register ``spec`` under its type. The same object twice is fine; a
    DIFFERENT spec for a type already registered is a bug."""
    existing = _REGISTRY.get(spec.type)
    if existing is not None and existing is not spec:
        raise ValueError(f"a result spec for {spec.type!r} is already registered")
    _REGISTRY[spec.type] = spec
    return spec


def result_spec(spec: ResultSpec) -> Callable[[Callable], Callable]:
    """Declare the ``detail`` shape an evaluator produces, beside it.

    Registers ``spec`` under ``spec.type`` and attaches it as ``fn.result_spec``;
    returns the function unchanged.
    """
    register(spec)

    def attach(fn: Callable) -> Callable:
        fn.result_spec = spec  # type: ignore[attr-defined]
        return fn

    return attach


def specs() -> dict[str, ResultSpec]:
    """Every registered type's spec, loading the declaring modules once."""
    global _loaded
    if not _loaded:
        for module in _DECLARING_MODULES:
            importlib.import_module(module)
        _loaded = True
    return dict(_REGISTRY)


def spec_for(method: Optional[str]) -> Optional[ResultSpec]:
    """The result spec for the path that answered (``Outcome.method``)."""
    if not method:
        return None
    return _REGISTRY.get(method) or specs().get(method)


# ---------------------------------------------------------------------------
# Helpers the producers and consumers share
# ---------------------------------------------------------------------------


def with_metric(detail: dict, method: str, value: Any) -> dict:
    """``detail`` with the type's ``metric`` / ``value`` added, first."""
    spec = spec_for(method)
    if spec is None:
        raise KeyError(f"no result spec registered for {method!r}")
    rest = {k: v for k, v in detail.items() if k not in ("metric", "value")}
    return {"metric": spec.metric, "value": value, **rest}


def clear_judgement(detail: Any, method: Optional[str]) -> Any:
    """For ``score: false``: drop ``value`` when the metric IS the judgement (llm)."""
    spec = spec_for(method)
    if isinstance(detail, dict) and spec is not None and spec.metric_from == "score":
        return {**detail, "value": None}
    return detail


def member_value(detail: Any) -> Any:
    return detail.get("value") if isinstance(detail, dict) else None


def aggregated_detail(
    *,
    method: Optional[str],
    rule: str,
    level: str,
    members: list[tuple[str, Any]],
    chosen: Optional[tuple[str, Any]] = None,
    base: Optional[dict] = None,
    value: Any = None,
) -> Optional[dict]:
    """The ``detail`` of an aggregate.

    Args:
        method:  The members' method (they share one; the first is used).
        rule:    The rule as sent (``all`` stays ``all``).
        level:   "pages" | "documents".
        members: ``[(label, detail), ...]`` of the members that were counted.
        chosen:  ``(label, detail)`` for any / worst — that detail is kept whole.
        base:    A complete detail to use instead (sum builds its own record).
        value:   The aggregated value, for mean and sum.
    """
    values = {label: member_value(d) for label, d in members}
    block = {
        "rule": rule,
        "level": level,
        "from": chosen[0] if chosen else None,
        "values": values,
    }
    if chosen is not None:
        source = chosen[1]
        if not isinstance(source, dict):
            return {"aggregate": block}
        return {**source, "aggregate": block}
    if base is not None:
        return {**base, "aggregate": block}

    # mean: only what means the same on every member, plus the mean value.
    first = next((d for _, d in members if isinstance(d, dict)), None)
    spec = spec_for(method)
    if first is None or spec is None:
        return {"value": value, "aggregate": block}
    kept = {k: v for k, v in first.items() if k in spec.stable()}
    return {**kept, "metric": first.get("metric", spec.metric), "value": value, "aggregate": block}
