"""How sure the model was — measured, not asked.

An ``llm`` criterion's top-level ``confidence`` has always been a number the
model types into its JSON after it has written the score. On the box's stored
jobs (2026-10) 68% of them say 95-100: the number is not tied to how often the
answer is right. This module turns what the model's SAMPLING actually did
into signals, and — once a calibration is fitted — into a 0-100 number that
means "right about this often".

Where the measurement comes from is per-model configuration
(``models.yaml`` ``confidence.method``, parsed by ``llm.models``; no code here
branches on a model's name):

    logprobs   the score token's distribution, read by ``llm.client`` from the
               response's token log-probabilities through
               ``common.logprobs.int_field_distribution`` (both whole-number
               and split-digit tokenizers, reasoning prefixes skipped)
    sampling   the empirical distribution of n answers to the same prompt
               (``common.logprobs.from_samples``); the REPORTED answer is
               still the first sample's, so sampling never moves a score
    stated     nothing measured: the model's own number (the default)

Every result carries the block :func:`assess` builds as
``detail.confidence``::

    method             what the entry is configured to measure
    source             what actually produced the signals: logprobs |
                       sampling | stated (the fallback — the response had no
                       logprobs, coverage was under ``min_coverage``, …;
                       ``note`` says why)
    headline           which number is the top-level ``confidence``:
                       stated | calibrated
    stated             the model's own 0-100 number (clamped)
    stated_defaulted   true when the model omitted or garbled it (it is 50)
    calibrated         the calibration curve at its signal, 0-100, or null
                       (no calibration, or its signal is unavailable here)
    verdict_probability  probability mass on the reported verdict's band
                       (PASS 7-10, MARGINAL 4-6, FAIL 1-3) — a LOWER bound:
                       ``unresolved`` mass that could fall in two bands
                       counts in neither
    band               that mass per band
    distribution       {"1": p, …, "10": p} — the resolved probabilities
    expected_score     the distribution's mean (unresolved split evenly)
    entropy_bits       its entropy (0 = certain, 3.32 = uniform)
    certainty          1 - entropy_bits / log2(10)
    coverage           the numeric share of the probability at the score
                       position, before renormalising (1.0 for samples)
    unresolved         [{values, mass}] — a non-sampled ``1`` under a
                       split-digit tokenizer is 1 or 10
    samples            sampling only: {asked, answered, agreement,
                       stated_mean}; else null
    note               why the source or the headline fell back, else null

**Shadow first.** ``headline`` stays ``stated`` — the top-level number does
not change — until an entry is given ``headline: calibrated`` AND a
``calibration`` (``bin/confidence_calibration.py`` fits one: isotonic
regression, :func:`fit_isotonic`, of "the fast verdict was right" on one
signal). Then the top-level ``confidence`` is ``round(apply(points, x))`` for
that signal's value x, and the stated number stays in this block.

Combining reference-guided calls: ``any`` / ``all`` keep the chosen call's
block; ``mean`` scores the averaged answer against :func:`mixture` of the
calls' distributions.

The calibration helpers are pure python (pool-adjacent-violators and a
monotone piecewise-linear interpolation), shared by the harness and the
runtime so a fitted curve is applied exactly as it was fitted.

Process flow position: called by ``analysis.llm_eval`` with the scoring
answer ``llm.client.call_model`` returned (its private ``_score_dist`` /
``_samples`` / ``_confidence_note`` keys); imports ``llm.models`` (the
config) and ``common.logprobs`` only.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Optional, Sequence

from common.logprobs import Distribution, Unresolved

from config import DETAIL_FLOAT_DECIMALS
from llm.models import ConfidenceConfig
from utils import verdict_from_score

# The scoring field, as the prompt asks for it (llm.prompts) and
# llm.validate.clamp_answer clamps it.
SCORE_KEY = "score"
SCORE_LO, SCORE_HI = 1, 10
BANDS: tuple[str, ...] = ("PASS", "MARGINAL", "FAIL")

# The private keys llm.client puts on a scoring answer for this module. They
# never reach a result: analysis.llm_eval builds the Outcome from named keys.
DIST_KEY = "_score_dist"
SAMPLES_KEY = "_samples"
NOTE_KEY = "_confidence_note"
PRIVATE_KEYS: tuple[str, ...] = (DIST_KEY, SAMPLES_KEY, NOTE_KEY)

# The detail.confidence keys, in order (declared in analysis.llm_eval's
# LLM_RESULT; checked by unit-tests/classifier/test_result_specs.py).
BLOCK_KEYS: tuple[str, ...] = (
    "method", "source", "headline", "stated", "stated_defaulted", "calibrated",
    "verdict_probability", "band", "distribution", "expected_score", "entropy_bits",
    "certainty", "coverage", "unresolved", "samples", "note",
)
SAMPLES_KEYS: tuple[str, ...] = ("asked", "answered", "agreement", "stated_mean")


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), DETAIL_FLOAT_DECIMALS)


# ---------------------------------------------------------------------------
# Distribution → signals
# ---------------------------------------------------------------------------


def band_mass(dist: Distribution) -> dict[str, float]:
    """The probability of each verdict band (``utils.verdict_from_score``).

    A LOWER bound per band: unresolved mass whose possible values fall in
    two bands ({1, 10}: FAIL or PASS) counts in neither.
    """
    out = {band: 0.0 for band in BANDS}
    for value, p in dist.probs.items():
        out[verdict_from_score(value)] += p
    for u in dist.unresolved:
        bands = {verdict_from_score(v) for v in u.values}
        if len(bands) == 1:
            out[bands.pop()] += u.mass
    return out


def mixture(dists: Sequence[Distribution]) -> Distribution:
    """The average of several distributions (the guided calls under
    ``combine: mean``). ``coverage`` is the mean coverage; ``sampled`` is the
    mixture's most probable value (unresolved split evenly)."""
    if not dists:
        raise ValueError("mixture needs at least one distribution")
    k = len(dists)
    lo, hi = dists[0].lo, dists[0].hi
    probs: dict[int, float] = {}
    for d in dists:
        for v, p in d.probs.items():
            probs[v] = probs.get(v, 0.0) + p / k
    unresolved: dict[tuple[int, ...], float] = {}
    for d in dists:
        for u in d.unresolved:
            unresolved[u.values] = unresolved.get(u.values, 0.0) + u.mass / k
    mixed = Distribution(
        probs={v: probs[v] for v in sorted(probs)},
        unresolved=tuple(Unresolved(vals, m) for vals, m in unresolved.items()),
        coverage=sum(d.coverage for d in dists) / k,
        sampled=dists[0].sampled,
        lo=lo,
        hi=hi,
    )
    split = mixed.even_split()
    best = max(split, key=lambda v: (split[v], -v))
    return Distribution(mixed.probs, mixed.unresolved, mixed.coverage, best, lo, hi)


def _samples_block(samples: Optional[dict]) -> Optional[dict]:
    """``{asked, answered, agreement, stated_mean}`` from the client's raw
    ``_samples`` (``asked``, ``verdicts``, ``stated``)."""
    if not samples:
        return None
    verdicts = list(samples.get("verdicts") or [])
    stated = [s for s in samples.get("stated") or [] if isinstance(s, (int, float))]
    answered = len(verdicts)
    agreement = (Counter(verdicts).most_common(1)[0][1] / answered) if answered else None
    return {
        "asked": int(samples.get("asked") or answered),
        "answered": answered,
        "agreement": _r(agreement),
        "stated_mean": _r(sum(stated) / len(stated)) if stated else None,
    }


def signals(
    dist: Optional[Distribution],
    verdict: str,
    stated: int,
    stated_defaulted: bool,
    *,
    method: str = "stated",
    source: str = "stated",
    samples: Optional[dict] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """The ``detail.confidence`` block for one answer, before the headline is
    resolved (``calibrated`` null, ``headline`` "stated") — :func:`assess`
    fills those. With no ``dist`` every distribution key is null."""
    block: dict[str, Any] = {key: None for key in BLOCK_KEYS}
    block.update(
        method=method,
        source=source,
        headline="stated",
        stated=int(stated),
        stated_defaulted=bool(stated_defaulted),
        samples=_samples_block(samples),
        note=note,
    )
    if dist is None:
        return block
    bands = band_mass(dist)
    block.update(
        verdict_probability=_r(bands.get(verdict, 0.0)),
        band={b: _r(p) for b, p in bands.items()},
        distribution={str(v): _r(dist.probs.get(v, 0.0)) for v in range(SCORE_LO, SCORE_HI + 1)},
        expected_score=_r(dist.expected()),
        entropy_bits=_r(dist.entropy_bits()),
        certainty=_r(dist.certainty()),
        coverage=_r(dist.coverage),
        unresolved=[{"values": list(u.values), "mass": _r(u.mass)} for u in dist.unresolved],
    )
    return block


def signal_value(block: dict[str, Any], signal: str) -> Optional[float]:
    """The 0-1 value of calibration ``signal`` in a block, or None."""
    if signal == "stated":
        return block["stated"] / 100.0 if block.get("stated") is not None else None
    if signal == "agreement":
        samples = block.get("samples") or {}
        return samples.get("agreement")
    value = block.get(signal)
    return float(value) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------------------
# The block and the headline for one scoring answer
# ---------------------------------------------------------------------------


def assess(answer: dict[str, Any], conf: ConfidenceConfig) -> tuple[dict[str, Any], int]:
    """``(detail.confidence, the top-level confidence)`` for one answer.

    ``answer`` is a clamped scoring answer (``llm.validate.clamp_answer``:
    score, verdict, confidence, stated_defaulted) with the private keys
    ``llm.client.call_model`` attached when it measured something. An answer
    without them (a test that replaces ``call_model``) reports source
    ``stated``. Never raises for a missing signal: the fallback is reported,
    and the block's ``note`` says why.
    """
    method = conf.method
    dist: Optional[Distribution] = answer.get(DIST_KEY)
    note: Optional[str] = answer.get(NOTE_KEY)
    if method != "stated" and dist is None and note is None:
        note = f"{method}: the scoring call measured nothing; the fallback ({conf.fallback}) is reported"
    if (
        method == "logprobs" and dist is not None
        and dist.coverage < conf.logprobs.min_coverage
    ):
        note = (
            f"logprobs: numeric coverage {dist.coverage:.3f} is under min_coverage "
            f"{conf.logprobs.min_coverage}; the fallback ({conf.fallback}) is reported"
        )
        dist = None
    if method == "stated":
        dist = None
    source = method if dist is not None else conf.fallback
    block = signals(
        dist,
        answer["verdict"],
        answer["confidence"],
        bool(answer.get("stated_defaulted", False)),
        method=method,
        source=source,
        samples=answer.get(SAMPLES_KEY) if method == "sampling" else None,
        note=note,
    )
    stated = block["stated"]
    cal = conf.calibration
    if cal is not None:
        x = signal_value(block, cal.signal)
        if x is not None:
            block["calibrated"] = int(round(apply(cal.points, x)))
    if conf.headline == "calibrated":
        if block["calibrated"] is not None:
            block["headline"] = "calibrated"
        else:
            extra = (
                f"headline calibrated on {cal.signal!r}, which this answer does not have "
                f"(source {source}); the stated number is the headline"
            )
            block["note"] = f"{block['note']}; {extra}" if block["note"] else extra
    headline = block["calibrated"] if block["headline"] == "calibrated" else stated
    return block, int(headline)


def combined_answer(answer: dict[str, Any], members: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """A ``combine: mean`` answer with the members' measurements merged in:
    the mixture of their distributions (when any has one) and
    ``stated_defaulted`` when any member's was."""
    members = list(members)
    dists = [m[DIST_KEY] for m in members if m.get(DIST_KEY) is not None]
    out = dict(answer)
    out["stated_defaulted"] = any(m.get("stated_defaulted") for m in members)
    if dists:
        out[DIST_KEY] = mixture(dists)
        if len(dists) < len(members):
            out[NOTE_KEY] = (
                f"the mixture of {len(dists)} of {len(members)} calls' distributions "
                "(the others measured nothing)"
            )
    else:
        notes = [m.get(NOTE_KEY) for m in members if m.get(NOTE_KEY)]
        if notes:
            out[NOTE_KEY] = notes[0]
    return out


# ---------------------------------------------------------------------------
# Calibration: fit (the harness) and apply (the runtime)
# ---------------------------------------------------------------------------


def fit_isotonic(xs: Sequence[float], ys: Sequence[float]) -> list[list[float]]:
    """The isotonic (non-decreasing) least-squares fit of ``ys`` on ``xs``,
    as ``[[x, y], ...]`` points — one per pooled block, x the block's mean x,
    y its mean y. Pool-adjacent-violators; equal xs are pooled first.
    Rounded to ``DETAIL_FLOAT_DECIMALS``.

    Raises:
        ValueError: Empty or mismatched inputs.
    """
    if len(xs) != len(ys) or not xs:
        raise ValueError("fit_isotonic needs equally many xs and ys, at least one")
    grouped: dict[float, list[float]] = {}
    for x, y in zip(xs, ys):
        grouped.setdefault(float(x), []).append(float(y))
    blocks: list[list[float]] = []  # [Σx·w, Σy, w]
    for x in sorted(grouped):
        ys_at = grouped[x]
        blocks.append([x * len(ys_at), sum(ys_at), float(len(ys_at))])
        while len(blocks) >= 2 and blocks[-2][1] / blocks[-2][2] > blocks[-1][1] / blocks[-1][2]:
            last = blocks.pop()
            for i in range(3):
                blocks[-1][i] += last[i]
    points = [
        [round(sx / w, DETAIL_FLOAT_DECIMALS), round(sy / w, DETAIL_FLOAT_DECIMALS)]
        for sx, sy, w in blocks
    ]
    # Rounding must not break monotonicity.
    for i in range(1, len(points)):
        points[i][0] = max(points[i][0], points[i - 1][0])
        points[i][1] = max(points[i][1], points[i - 1][1])
    return points


def apply(points: Sequence[Sequence[float]], x: float) -> float:
    """The calibrated value at ``x``: monotone piecewise-linear between the
    points, flat beyond the first and the last."""
    if not points:
        raise ValueError("apply needs at least one calibration point")
    x = float(x)
    if x <= points[0][0]:
        return float(points[0][1])
    if x >= points[-1][0]:
        return float(points[-1][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y1)
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(points[-1][1])  # unreachable for sorted points
