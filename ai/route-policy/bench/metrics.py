"""The benchmark's metric maths -- pure functions, no I/O.

Everything takes PAIRS ``(predicted, label)``: ``label`` is a route; a
``predicted`` of ``None`` (the classifier abstained or failed) is a
prediction that matches no label -- it counts against accuracy and recall,
never for precision. Definitions (``unit-tests/route-policy/bench/
test_bench_metrics.py`` pins them):

* **accuracy** -- matches / pairs; None when there are no pairs;
* **per-route precision / recall / F1** -- precision = tp / (tp + fp), None
  when the route was never predicted; recall = tp / (tp + fn), None when no
  label has the route; F1 = 2PR / (P + R), 0 when P + R = 0, and P or R
  taken as 0 when it is None (scikit-learn's ``zero_division=0``);
* **macro-F1** -- the mean F1 over every route that is a label or a
  prediction (None excluded);
* **confusion** -- {label: {predicted: count}}, an abstain under ``(none)``;
* **calibration** -- accuracy per confidence bucket of width ``step``
  (``[0, 0.1)``, ... the last bucket closed: 1.0 goes in ``[0.9, 1.0]``);
* **percentile** -- linear interpolation between the closest ranks (numpy's
  default), None for no values;
* **the promotion rule** (ROUTE_POLICY_PLAN § 10), :func:`promotion`: a
  candidate passes when, on the GOLD labels, its accuracy is at least the
  rules' on the same points and its ``local-private`` recall at least the
  privacy keywords' alone; its failure rate is under 1 %; and its p95
  latency is under the median gap between user messages. A check that
  cannot be computed (no gold labels, no gold ``local-private`` point, no
  latency or gap) is ``None`` -- not passed -- so the candidate fails with
  the reason printed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROUTE_ORDER = ("local-general", "local-code", "local-reasoning", "local-private")
PRIVATE_ROUTE = "local-private"
NONE_LABEL = "(none)"
# Result statuses that produced scores (`ClassifierClient.assess`): the rest
# are failures.
SCORED_STATUSES = ("ok", "incomplete")
MAX_FAILURE_RATE = 0.01

Pair = Tuple[Optional[str], str]


def ordered_routes(routes: Iterable[Optional[str]]) -> List[str]:
    """Known routes in ROUTE_ORDER, then any other in name order; None dropped."""
    present = {r for r in routes if r is not None}
    return [r for r in ROUTE_ORDER if r in present] + sorted(present - set(ROUTE_ORDER))


def ratio(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def accuracy(pairs: Sequence[Pair]) -> Optional[float]:
    return ratio(sum(1 for p, l in pairs if p == l), len(pairs))


@dataclass(frozen=True)
class RouteScore:
    precision: Optional[float]
    recall: Optional[float]
    f1: float
    support: int       # labels with this route
    predicted: int     # predictions of this route


def per_route(pairs: Sequence[Pair]) -> Dict[str, RouteScore]:
    out: Dict[str, RouteScore] = {}
    for route in ordered_routes([l for _, l in pairs] + [p for p, _ in pairs]):
        tp = sum(1 for p, l in pairs if p == route and l == route)
        predicted = sum(1 for p, _ in pairs if p == route)
        support = sum(1 for _, l in pairs if l == route)
        precision, recall = ratio(tp, predicted), ratio(tp, support)
        p0, r0 = precision or 0.0, recall or 0.0
        f1 = 2 * p0 * r0 / (p0 + r0) if p0 + r0 else 0.0
        out[route] = RouteScore(precision, recall, f1, support, predicted)
    return out


def macro_f1(pairs: Sequence[Pair]) -> Optional[float]:
    scores = per_route(pairs)
    return sum(s.f1 for s in scores.values()) / len(scores) if scores else None


def recall_for(pairs: Sequence[Pair], route: str) -> Optional[float]:
    support = [p for p, l in pairs if l == route]
    return ratio(sum(1 for p in support if p == route), len(support))


def confusion(pairs: Sequence[Pair]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for p, l in pairs:
        row = out.setdefault(l, {})
        key = p if p is not None else NONE_LABEL
        row[key] = row.get(key, 0) + 1
    return out


@dataclass(frozen=True)
class Bucket:
    low: float
    high: float
    n: int
    accuracy: Optional[float]
    mean_confidence: Optional[float]

    @property
    def name(self) -> str:
        return f"{self.low:.1f}-{self.high:.1f}"


def calibration(triples: Sequence[Tuple[Optional[str], str, Optional[float]]], *,
                step: float = 0.1) -> List[Bucket]:
    """Accuracy per confidence bucket over (predicted, label, confidence);
    a triple with no confidence is left out. Empty buckets are omitted."""
    count = int(round(1.0 / step))
    groups: Dict[int, List[Tuple[bool, float]]] = {}
    for p, l, c in triples:
        if c is None or isinstance(c, bool) or not isinstance(c, (int, float)) or math.isnan(c):
            continue
        c = min(max(float(c), 0.0), 1.0)
        index = min(int(c / step + 1e-9), count - 1)
        groups.setdefault(index, []).append((p == l, c))
    return [Bucket(round(i * step, 10), round((i + 1) * step, 10), len(g),
                   sum(1 for hit, _ in g if hit) / len(g), sum(c for _, c in g) / len(g))
            for i, g in sorted(groups.items())]


def percentile(values: Iterable[Optional[float]], q: float) -> Optional[float]:
    """The q-th percentile (0-100), linear interpolation; None when empty."""
    data = sorted(float(v) for v in values if v is not None and not isinstance(v, bool))
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (q / 100.0) * (len(data) - 1)
    low = math.floor(rank)
    high = min(low + 1, len(data) - 1)
    return data[low] + (data[high] - data[low]) * (rank - low)


def median(values: Iterable[Optional[float]]) -> Optional[float]:
    return percentile(values, 50)


def failure_rate(statuses: Sequence[Optional[str]]) -> Optional[float]:
    """Share of results whose status is not ``ok`` / ``incomplete``."""
    return ratio(sum(1 for s in statuses if s not in SCORED_STATUSES), len(statuses))


# -- the promotion rule ---------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    passed: Optional[bool]     # None = could not be computed (not passed)
    detail: str


@dataclass(frozen=True)
class Promotion:
    checks: List[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed is True for c in self.checks)

    @property
    def verdict(self) -> str:
        return "PASS" if self.passed else "FAIL"


def _fmt(value: Optional[float], spec: str = ".3f") -> str:
    return "n/a" if value is None else format(value, spec)


def promotion(*, gold_accuracy: Optional[float], rules_gold_accuracy: Optional[float], gold_n: int,
              private_recall: Optional[float], privacy_kw_recall: Optional[float], private_support: int,
              failure: Optional[float], p95_latency_ms: Optional[float],
              median_gap_s: Optional[float]) -> Promotion:
    """ROUTE_POLICY_PLAN § 10's rule, one :class:`Check` per condition."""
    checks: List[Check] = []

    if gold_n == 0 or gold_accuracy is None or rules_gold_accuracy is None:
        checks.append(Check("accuracy", None, "no gold labels for this candidate's points"))
    else:
        checks.append(Check("accuracy", gold_accuracy >= rules_gold_accuracy,
                            f"gold accuracy {_fmt(gold_accuracy)} vs rules {_fmt(rules_gold_accuracy)} "
                            f"on the same {gold_n} points"))

    if private_support == 0 or private_recall is None or privacy_kw_recall is None:
        checks.append(Check("private_recall", None, "no gold local-private point among this candidate's points"))
    else:
        checks.append(Check("private_recall", private_recall >= privacy_kw_recall,
                            f"local-private recall {_fmt(private_recall)} vs privacy keywords alone "
                            f"{_fmt(privacy_kw_recall)} on {private_support} gold local-private points"))

    if failure is None:
        checks.append(Check("failure_rate", None, "no results"))
    else:
        checks.append(Check("failure_rate", failure < MAX_FAILURE_RATE,
                            f"failure rate {failure:.2%} (must be under {MAX_FAILURE_RATE:.0%})"))

    if p95_latency_ms is None or median_gap_s is None:
        checks.append(Check("latency", None, "no latency measured" if p95_latency_ms is None
                            else "no gap between user messages in the points"))
    else:
        checks.append(Check("latency", p95_latency_ms / 1000.0 < median_gap_s,
                            f"p95 latency {p95_latency_ms / 1000.0:.1f} s vs median gap between "
                            f"user messages {median_gap_s:.1f} s"))
    return Promotion(checks)


def summary(pairs: Sequence[Pair]) -> Dict[str, Any]:
    """accuracy, macro-F1, per-route scores and the confusion matrix of ``pairs``."""
    return {
        "n": len(pairs),
        "accuracy": accuracy(pairs),
        "macro_f1": macro_f1(pairs),
        "per_route": {r: asdict(s) for r, s in per_route(pairs).items()},
        "private_recall": recall_for(pairs, PRIVATE_ROUTE),
        "private_support": sum(1 for _, l in pairs if l == PRIVATE_ROUTE),
        "confusion": confusion(pairs),
    }
