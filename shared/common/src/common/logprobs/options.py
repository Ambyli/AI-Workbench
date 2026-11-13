"""Reading a one-token answer's distribution over a fixed set of labels.

The implementation behind ``common.logprobs.option_distribution`` (the
package docstring is the guide). A model asked to "answer with one digit"
(or one letter) and sent ``logprobs: true, top_logprobs: K`` returns, at the
first generated position, the K tokens it could have written there with
their log-probabilities. That list IS the model's distribution over the
labels — no reasoning, no parsing of what it wrote:

1. **The position.** The first generated position whose sampled token is not
   whitespace (a model that opens with a space or a newline answers at the
   next one). When every position is whitespace, the first one is read: its
   alternatives may still hold the labels (``" 7"``).
2. **The match.** Each candidate there (the ``top_logprobs`` alternatives
   plus the sampled token, deduplicated by exact text) is trimmed of
   whitespace and of the bracket / punctuation characters ``[ ( ) ] . :``
   at both ends, then compared with every label in EXACT case. So ``B``,
   ``[B``, ``(B``, ``B]``, ``B.`` and ``" B"`` all count as ``B``, and their
   probabilities are summed (measured on the box: a model writes ``[B`` as
   one token for 0.5-12% of the mass).
3. **The report.** ``coverage`` is the matched mass before renormalising
   (the convention ``int_field_distribution`` uses), at most 1; ``probs`` is
   that mass renormalised over the labels (every label present, 0.0 when
   unseen; it sums to 1 even on a stream whose matched rows add up past 1);
   ``logprobs`` is each SEEN label's raw log-probability (the log of its
   summed variants — what a temperature is fitted on). A label with no
   candidate is UNSEEN: its probability is not known. Every token outside
   the returned top-K is at most as probable as the smallest one listed
   (``p_min_listed``), so ``unseen_bound = min(1 - coverage, n_unseen *
   p_min_listed)`` ESTIMATES what the unseen labels hold together — a
   confident answer whose other labels simply fell off the list gets a tiny
   one; a spread answer over more labels than K can show, a large one. It is
   an estimate, not a guarantee: it counts one spelling per unseen label,
   while the match above accepts several (``3``, `` 3``, ``[3``, ``3.``, …),
   each of which could sit just under ``p_min_listed``, and a seen label's
   unlisted spellings are not counted at all. The only hard ceiling is
   ``1 - coverage``. What to do with it is the caller's decision.

Labels are expected to be single tokens (digits, letters): a label the
tokenizer splits is only ever matched by its first piece, which this reader
does not try to guess.

Never raises on a garbled stream (only on a bad argument): no entries, or a
position with no usable candidate, is ``None``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

# What a candidate token is trimmed of before it is compared with a label:
# whitespace and the bracket / punctuation characters a model writes around
# a one-character answer ("[B", "(B", "B]", "B.", "B:").
TRIM_CHARS = " \t\r\n[]().:"


@dataclass(frozen=True)
class OptionReadout:
    """One readout: the first answer position's distribution over ``labels``.

    Attributes:
        labels:       The labels asked about, in the caller's order.
        probs:        ``{label: p}`` for EVERY label — the matched mass
                      renormalised to sum to 1 (all 0.0 when nothing matched);
                      an unseen label is 0.0, a lower bound.
        logprobs:     ``{label: log p}`` for the SEEN labels only — raw (the
                      log of the summed variants' probabilities, before
                      renormalising), what a temperature is applied to.
        coverage:     The matched mass before renormalising, clamped to 0-1.
        first_token:  The sampled token at the position read.
        n_unseen:     How many labels had no candidate in the list.
        p_min_listed: The smallest probability in the returned alternatives
                      (0.0 when the position listed none).
        unseen_bound: An ESTIMATE of what the unseen labels hold together,
                      one spelling each under the smallest listed
                      probability: ``min(1 - coverage, n_unseen *
                      p_min_listed)``; 0.0 when every label was seen. Not a
                      guarantee (module docstring); ``1 - coverage`` is the
                      only hard ceiling.
        listed:       How many alternatives the position listed (the top-K
                      the server returned, which may be fewer than asked).
    """

    labels: tuple[str, ...]
    probs: dict[str, float]
    logprobs: dict[str, float]
    coverage: float
    first_token: str
    n_unseen: int
    p_min_listed: float
    unseen_bound: float
    listed: int

    def best(self) -> Optional[str]:
        """The most probable label (the first in label order on a tie), or
        None when nothing matched."""
        if self.coverage <= 0.0:
            return None
        return max(self.labels, key=lambda label: (self.probs[label], -self.labels.index(label)))


def _logprob(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or value > 0.0 + 1e-6:
        return None
    return min(0.0, float(value))


def _token(entry: Any) -> Optional[str]:
    if isinstance(entry, Mapping) and isinstance(entry.get("token"), str):
        return entry["token"]
    return None


def _position(entries: list) -> Optional[Mapping]:
    """The first entry whose sampled token is not whitespace, else the first
    entry; None when there is no mapping entry at all."""
    mappings = [e for e in entries if isinstance(e, Mapping)]
    if not mappings:
        return None
    for entry in mappings:
        token = _token(entry)
        if token is not None and token.strip():
            return entry
    return mappings[0]


def _alternatives(entry: Mapping, top_key: str) -> list[tuple[str, float]]:
    """The position's listed alternatives, ``(token, p)``, in list order;
    malformed rows are dropped."""
    top = entry.get(top_key)
    out: list[tuple[str, float]] = []
    for row in top if isinstance(top, list) else []:
        if not isinstance(row, Mapping) or not isinstance(row.get("token"), str):
            continue
        lp = _logprob(row.get("logprob"))
        if lp is None:
            continue
        out.append((row["token"], math.exp(lp)))
    return out


def option_distribution(
    entries: Any,
    labels: Sequence[str],
    *,
    top_key: str = "top_logprobs",
) -> Optional[OptionReadout]:
    """The first answer position's distribution over ``labels``.

    Args:
        entries: The logprob entries, ``[{token, logprob, <top_key>: [{token,
                 logprob}]}]`` — an OpenAI-style ``choices[0].logprobs.content``
                 (read it with :func:`common.logprobs.resolve_path`).
        labels:  The answers, in order — single tokens ("0".."9", "A".."Z").
                 Matched in exact case after trimming (module docstring).
        top_key: The alternatives list's key in each entry.

    Returns:
        An :class:`OptionReadout`, or None when there are no entries or the
        position read has no usable candidate.

    Raises:
        ValueError: ``labels`` is empty, holds a non-string or an empty
            string, or repeats a label.
    """
    labels = tuple(labels)
    if not labels:
        raise ValueError("option_distribution needs at least one label")
    for label in labels:
        if not isinstance(label, str) or not label:
            raise ValueError(f"labels must be non-empty strings, got {label!r}")
    if len(set(labels)) != len(labels):
        raise ValueError(f"labels must be unique, got {list(labels)}")
    if not isinstance(entries, list) or not entries:
        return None
    entry = _position(entries)
    if entry is None:
        return None

    listed = _alternatives(entry, top_key)
    candidates: list[tuple[str, float]] = []
    seen_tokens: set[str] = set()
    sampled = _token(entry)
    sampled_lp = _logprob(entry.get("logprob"))
    rows = list(listed)
    if sampled is not None and sampled_lp is not None:
        rows.append((sampled, math.exp(sampled_lp)))
    for token, p in rows:
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        candidates.append((token, p))
    if not candidates:
        return None

    wanted = set(labels)
    mass: dict[str, float] = {}
    for token, p in candidates:
        key = token.strip(TRIM_CHARS)
        if key in wanted:
            mass[key] = mass.get(key, 0.0) + p

    # Renormalise by the UNCLAMPED total, so probs sums to 1 even on a stream
    # whose matched rows add up past 1 (two spellings of one label, each
    # reported near its own ceiling); only the reported coverage is clamped.
    total = sum(mass.values())
    coverage = min(1.0, total)
    probs = {
        label: (mass.get(label, 0.0) / total if total > 0.0 else 0.0)
        for label in labels
    }
    logprobs = {label: math.log(mass[label]) for label in labels if mass.get(label, 0.0) > 0.0}
    n_unseen = sum(1 for label in labels if label not in mass)
    p_min_listed = min((p for _, p in listed), default=0.0)
    if not listed:
        # No alternatives list: only the sampled token is known, so nothing
        # bounds what an unseen label could hold beyond 1 - coverage.
        unseen_bound = (1.0 - coverage) if n_unseen else 0.0
    else:
        unseen_bound = min(1.0 - coverage, n_unseen * p_min_listed) if n_unseen else 0.0
    return OptionReadout(
        labels=labels,
        probs=probs,
        logprobs=logprobs,
        coverage=coverage,
        first_token=sampled if sampled is not None else "",
        n_unseen=n_unseen,
        p_min_listed=p_min_listed,
        unseen_bound=max(0.0, unseen_bound),
        listed=len(listed),
    )


# ---------------------------------------------------------------------------
# Calibration helpers (fitted by a harness, applied at run time)
# ---------------------------------------------------------------------------


def softmax_temperature(logprobs: Mapping[str, float], t: float) -> dict[str, float]:
    """``{label: p}`` with ``p ∝ exp(logprob / t)`` over the labels given —
    temperature scaling of a readout's raw ``logprobs`` (t > 1 flattens, t < 1
    sharpens, t = 1 is plain renormalisation). An empty mapping is ``{}``.

    Raises:
        ValueError: ``t`` is not a positive finite number.
    """
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0:
        raise ValueError(f"temperature must be a positive number, got {t!r}")
    items = [(label, float(lp)) for label, lp in logprobs.items() if math.isfinite(float(lp))]
    if not items:
        return {}
    top = max(lp for _, lp in items) / t
    weights = {label: math.exp(lp / t - top) for label, lp in items}
    total = sum(weights.values())
    return {label: w / total for label, w in weights.items()}


def platt(p: float, t: float = 1.0, bias: float = 0.0) -> float:
    """Platt scaling of a probability: ``sigmoid(logit(p) / t + bias)``, with
    ``p`` clamped into (1e-12, 1 - 1e-12) so 0 and 1 stay finite.

    Raises:
        ValueError: ``t`` is not a positive finite number, or ``p`` / ``bias``
            is not a number.
    """
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0:
        raise ValueError(f"temperature must be a positive number, got {t!r}")
    for name, value in (("p", p), ("bias", bias)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
            raise ValueError(f"{name} must be a number, got {value!r}")
    eps = 1e-12
    q = min(1.0 - eps, max(eps, float(p)))
    z = math.log(q / (1.0 - q)) / t + float(bias)
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)
