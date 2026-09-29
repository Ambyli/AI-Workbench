"""What a criterion's ``detail`` contains, declared per type.

``cv`` detectors declare their own shape beside each detector
(``cv.result.describes``, served by ``GET /cv-detectors``). This module does
the same for every criterion TYPE, and ``GET /criterion-types`` serves it as
each type's ``result`` block:

    llm       the model's headline number is its score; detail says what the
              model was sent
    text      the match record; the headline number is the hit count
    detector  the open-vocabulary detector's boxes; headline is the best box score
    cv        the detector's measurement (its keys: GET /cv-detectors)

Two keys are common to every type, so a consumer can read the headline number
without knowing which type ran:

    metric   the name of the headline number
    value    that number

For ``text`` and ``detector`` the metric is itself a detail key (``count``,
``best_score``) and ``value`` equals it. For ``cv`` it is a key of
``detail.measurements``. For ``llm`` it is ``score`` — the judgement — which
is why a ``score: false`` llm criterion has ``value: null``
(``clear_judgement``).

**Aggregation** (``analysis.aggregate``) keeps the shape and adds one block,
``detail.aggregate`` (``AGGREGATE_BLOCK``), instead of replacing ``detail``:

    any / worst  the chosen member's detail, whole
    mean         only the type's STABLE keys (the ones that mean the same on
                 every member — the pattern searched, the thresholds used) plus
                 ``value`` = the mean of the members' values; per-member keys
                 (measurements, snippets, what text was sent) are in ``items[]``
    sum          (text) the full match record, counts added

A single member passes through with no aggregate block — the pre-aggregation
result exactly.

``unit-tests/classifier/test_result_specs.py`` runs every type through
``/assess`` and fails if a ``detail`` carries a key its declaration does not
list, or misses one it requires.

Process flow position: read by the evaluators (``METRIC``), by
``analysis.aggregate`` and ``analysis.scheduler``, and by
``api.criterion_options`` for ``GET /criterion-types``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


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


_METRIC = FieldSpec("Name of the headline number", "string", stable=True)
_VALUE = FieldSpec("The headline number (the mean of the members' values under `mean`)", "number")
_AGG = FieldSpec(
    "How several members were combined — see `aggregate_detail`", "object",
    when="the criterion ran on more than one item or document",
)

SPECS: dict[str, ResultSpec] = {
    "llm": ResultSpec(
        type="llm",
        metric="score",
        metric_from="score",
        fields={
            "metric": _METRIC,
            "value": FieldSpec(
                "The model's score, 1-10 — null for a `score: false` criterion, whose "
                "judgement is not reported", "number",
            ),
            "hint": FieldSpec("The rubric the model was asked to use", "string", stable=True),
            "image_sent": FieldSpec("Whether the page image was attached", "boolean", stable=True),
            "text_sent": FieldSpec(
                "The text attached: `{chars, truncated, budget, source}`", "object",
            ),
            "aggregate": _AGG,
        },
        notes=(
            "The model's answer is the top-level score / verdict / confidence / reason; "
            "detail records what it was shown.",
            "Box-loop results are in `localization`, not `detail`.",
        ),
    ),
    "text": ResultSpec(
        type="text",
        metric="count",
        metric_from="detail",
        fields={
            "metric": _METRIC,
            "value": _VALUE,
            "found": FieldSpec("Whether count reached min_count", "boolean"),
            "count": FieldSpec("Hits found (summed across members under `sum`)", "integer"),
            "best_ratio": FieldSpec(
                "Best similarity seen, 0-1 — 1.0 for a literal/regex hit", "number",
            ),
            "mode": FieldSpec("The match mode used", "string", stable=True),
            "pattern": FieldSpec("What was searched for", "string", stable=True),
            "snippets": FieldSpec("Up to 8 context excerpts around the hits", "array"),
            "searched_chars": FieldSpec("Characters searched", "integer"),
            "case_sensitive": FieldSpec("Whether matching was case-sensitive", "boolean", stable=True),
            "min_count": FieldSpec("Hits required to PASS", "integer", stable=True),
            "fuzzy_threshold": FieldSpec(
                "Similarity a fuzzy window must reach; null for other modes", "number", stable=True,
            ),
            "text_source": FieldSpec(
                "native | ocr | none — or `mixed` when summed across members that differ",
                "string",
            ),
            "scope": FieldSpec(
                "`document` when the pages were searched joined", "string",
                stable=True, when="options.scope is document",
            ),
            "separator": FieldSpec(
                "What joined the pages", "string", stable=True, when="options.scope is document",
            ),
            "items_with_hits": FieldSpec(
                "Items the hits landed on", "array", when="options.scope is document",
            ),
            "aggregate": _AGG,
        },
        notes=("The full text searched is the `text.<key>.json` artifact the result links to.",),
    ),
    "detector": ResultSpec(
        type="detector",
        metric="best_score",
        metric_from="detail",
        fields={
            "metric": _METRIC,
            "value": _VALUE,
            "detector_matches": FieldSpec("Boxes found at or above min_score", "integer"),
            "best_score": FieldSpec("The best box's confidence, 0-1; 0 when none", "number"),
            "min_score": FieldSpec("The confidence floor used", "number", stable=True),
            "aggregate": _AGG,
        },
    ),
    "cv": ResultSpec(
        type="cv",
        metric="(per detector — GET /cv-detectors)",
        metric_from="measurements",
        fields={
            "metric": _METRIC,
            "value": _VALUE,
            "detector": FieldSpec("The OpenCV function that ran", "string", stable=True),
            "measurements": FieldSpec(
                "Everything the detector counted — its keys: GET /cv-detectors", "object",
            ),
            "thresholds": FieldSpec("The lines that decide the verdict", "object", stable=True),
            "parameters": FieldSpec("Config values it measured with", "object", stable=True),
            "state": FieldSpec(
                "A categorical outcome — its values: GET /cv-detectors", "string",
                when="the detector declares states",
            ),
            "image": FieldSpec(
                "The working-image frame the *_px measurements are in", "object", stable=True,
            ),
            "aggregate": _AGG,
        },
        notes=(
            "A cv criterion answered by its `fallback` has the llm or detector shape; "
            "`method` says which.",
        ),
    ),
}

AGGREGATE_BLOCK: dict[str, str] = {
    "rule": "any | worst | all | mean | sum — the rule that combined the members",
    "level": "pages (one document's items) | documents (the request's documents)",
    "from": "the member whose detail this is (any / worst), else null",
    "values": "each member's value, by member label (`item n` / `document n`)",
}


def spec_for(method: Optional[str]) -> Optional[ResultSpec]:
    """The result spec for the path that answered (``Outcome.method``)."""
    return SPECS.get(method or "")


def with_metric(detail: dict, method: str, value: Any) -> dict:
    """``detail`` with the type's ``metric`` / ``value`` added."""
    spec = SPECS[method]
    return {"metric": spec.metric, "value": value, **{k: v for k, v in detail.items()
                                                       if k not in ("metric", "value")}}


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
