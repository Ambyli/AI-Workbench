"""bench.metrics: the metric maths and the § 10 promotion rule, pinned."""

from __future__ import annotations

import pytest

from bench import metrics

G, C, R, P = "local-general", "local-code", "local-reasoning", "local-private"

# 10 pairs (predicted, label):
#   general: 4 labels, predicted right 3 times, once as code
#   code:    3 labels, right 2, once abstained (None)
#   private: 2 labels, right 1, once predicted general
#   reasoning: 1 label, predicted code
PAIRS = [
    (G, G), (G, G), (G, G), (C, G),
    (C, C), (C, C), (None, C),
    (P, P), (G, P),
    (C, R),
]


def test_accuracy_counts_an_abstain_as_wrong():
    assert metrics.accuracy(PAIRS) == pytest.approx(6 / 10)
    assert metrics.accuracy([]) is None
    assert metrics.accuracy([(None, G)]) == 0.0


def test_per_route_precision_recall_f1():
    scores = metrics.per_route(PAIRS)
    assert list(scores) == [G, C, R, P]          # ROUTE_ORDER, None dropped
    g, c, r, p = scores[G], scores[C], scores[R], scores[P]
    assert (g.precision, g.recall, g.support, g.predicted) == (pytest.approx(3 / 4), pytest.approx(3 / 4), 4, 4)
    assert g.f1 == pytest.approx(0.75)
    assert (c.precision, c.recall) == (pytest.approx(2 / 4), pytest.approx(2 / 3))
    assert c.f1 == pytest.approx(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))
    assert (r.precision, r.recall, r.f1, r.predicted) == (None, 0.0, 0.0, 0)   # never predicted
    assert (p.precision, p.recall, p.f1) == (1.0, 0.5, pytest.approx(2 / 3))


def test_a_route_only_predicted_has_no_recall():
    scores = metrics.per_route([(C, G)])
    assert scores[C].recall is None and scores[C].precision == 0.0 and scores[C].f1 == 0.0


def test_macro_f1_is_the_mean_over_routes_seen():
    s = metrics.per_route(PAIRS)
    assert metrics.macro_f1(PAIRS) == pytest.approx(sum(v.f1 for v in s.values()) / 4)
    assert metrics.macro_f1([]) is None
    assert metrics.macro_f1([(G, G)]) == 1.0


def test_unknown_routes_sort_after_the_known_ones():
    assert metrics.ordered_routes(["zeta", P, None, "alpha", G]) == [G, P, "alpha", "zeta"]


def test_private_recall_and_confusion():
    assert metrics.recall_for(PAIRS, P) == 0.5
    assert metrics.recall_for([(G, G)], P) is None
    assert metrics.confusion(PAIRS) == {
        G: {G: 3, C: 1},
        C: {C: 2, metrics.NONE_LABEL: 1},
        P: {P: 1, G: 1},
        R: {C: 1},
    }


def test_calibration_buckets_and_edges():
    triples = [
        (G, G, 0.7), (C, G, 0.75),       # 0.7-0.8: one right of two
        (C, C, 1.0),                      # 1.0 goes in the LAST bucket
        (C, C, 0.9),
        (G, G, 0.05),
        (G, G, None),                     # no confidence: left out
        (G, G, 1.5),                      # clamped to 1.0
    ]
    buckets = metrics.calibration(triples)
    assert [(b.name, b.n) for b in buckets] == [("0.0-0.1", 1), ("0.7-0.8", 2), ("0.9-1.0", 3)]
    assert buckets[1].accuracy == 0.5 and buckets[1].mean_confidence == pytest.approx(0.725)
    assert buckets[2].accuracy == 1.0
    assert metrics.calibration([]) == []


def test_percentile_interpolates_like_numpy():
    values = list(range(1, 11))  # 1..10
    assert metrics.percentile(values, 50) == pytest.approx(5.5)
    assert metrics.percentile(values, 95) == pytest.approx(9.55)
    assert metrics.percentile(values, 0) == 1 and metrics.percentile(values, 100) == 10
    assert metrics.percentile([7.0], 95) == 7.0
    assert metrics.percentile([], 50) is None
    assert metrics.percentile([None, 3, True, 1], 50) == pytest.approx(2.0)   # None and bools ignored
    assert metrics.median([30, 10, 20]) == 20


def test_failure_rate():
    assert metrics.failure_rate(["ok", "incomplete", "timeout", "not_local"]) == 0.5
    assert metrics.failure_rate(["ok", "skipped"]) == 0.5
    assert metrics.failure_rate([]) is None


def test_summary_shape():
    summary = metrics.summary(PAIRS)
    assert set(summary) == {"n", "accuracy", "macro_f1", "per_route", "private_recall", "private_support",
                            "confusion"}
    assert summary["private_support"] == 2
    assert set(summary["per_route"][G]) == {"precision", "recall", "f1", "support", "predicted"}


# -- the promotion rule ---------------------------------------------------------------------

GOOD = dict(gold_accuracy=0.8, rules_gold_accuracy=0.75, gold_n=150, private_recall=0.9, privacy_kw_recall=0.6,
            private_support=10, failure=0.004, p95_latency_ms=9_000.0, median_gap_s=40.0)


def promote(**changes):
    return metrics.promotion(**{**GOOD, **changes})


def test_promotion_passes_when_every_condition_holds():
    result = promote()
    assert result.passed and result.verdict == "PASS"
    assert [c.name for c in result.checks] == ["accuracy", "private_recall", "failure_rate", "latency"]
    assert all(c.passed is True for c in result.checks)


@pytest.mark.parametrize("changes, failing", [
    (dict(gold_accuracy=0.74), "accuracy"),
    (dict(private_recall=0.5), "private_recall"),
    (dict(failure=0.01), "failure_rate"),             # exactly 1 % is not "under 1 %"
    (dict(p95_latency_ms=40_000.0), "latency"),       # equal to the gap is not "below"
])
def test_promotion_fails_on_each_condition(changes, failing):
    result = promote(**changes)
    assert not result.passed and result.verdict == "FAIL"
    assert [c.name for c in result.checks if c.passed is False] == [failing]


def test_promotion_ties_pass():
    assert promote(gold_accuracy=0.75).passed
    assert promote(private_recall=0.6).passed


@pytest.mark.parametrize("changes, missing", [
    (dict(gold_n=0, gold_accuracy=None, rules_gold_accuracy=None), "accuracy"),
    (dict(private_support=0, private_recall=None, privacy_kw_recall=None), "private_recall"),
    (dict(failure=None), "failure_rate"),
    (dict(p95_latency_ms=None), "latency"),
    (dict(median_gap_s=None), "latency"),
])
def test_promotion_cannot_pass_on_what_it_cannot_compute(changes, missing):
    result = promote(**changes)
    assert not result.passed
    (check,) = [c for c in result.checks if c.passed is None]
    assert check.name == missing and check.detail
