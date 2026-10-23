"""Reading a JSON integer field's probability distribution out of a token stream.

The implementation behind ``common.logprobs`` (the package docstring is the
guide). Three steps:

1. **Find the value.** The token texts are concatenated into one string. The
   answer's start is the LAST place where the stream matches the start of the
   answer ``content`` (a reasoning model's logprob stream carries its thinking
   before the answer, and that thinking may itself write ``"score": 3``). After
   it, the LAST ``"<key>":`` is the field; quotes and whitespace after the
   colon are skipped, and the token holding the value's first digit is "the
   value position".
2. **Read the alternatives there.** Every candidate (the position's
   ``top_logprobs`` plus the sampled token) whose text, past whitespace and
   the sampled token's own prefix (``:`` in a ``:9`` token), starts with
   digits is a number; ``exp(logprob)`` is its probability. Everything else
   (``' '``, ``<|start|>``, ``' tenth'``) is the non-numeric mass that makes
   ``coverage`` less than 1.
3. **Resolve multi-token numbers.** A tokenizer either has ``10`` as one
   token (muse-glimmer) or splits digits (``1``, ``0``: the qwen family). For
   the SAMPLED first token the next position says which: its alternatives
   that start with a digit continue the number, everything else ends it, so
   ``P(1)`` and ``P(10)`` split exactly. A NON-sampled alternative that is a
   strict prefix of a valid value (``1`` when 10 is in range) has no next
   position to read; under a digit-splitting tokenizer its split is unknown,
   and its mass is reported as ``unresolved`` instead of being guessed.

``number_tokens`` says which tokenizer to assume when the stream itself does
not show it: ``auto`` reads it from the stream (a multi-digit candidate means
whole-number tokens; a sampled digit followed by a digit means split digits;
neither means "assume split", which only ever moves mass to ``unresolved``);
``single`` / ``digits`` pin it. A pinned mode the stream contradicts is
logged (WARNING) and the stream wins — a misconfigured entry degrades its
signal, it never makes one up.

Values outside ``[lo, hi]`` are clamped exactly the way the caller clamps the
parsed answer (the classifier: 0 → 1, 11+ → 10), so the distribution and the
reported score describe the same thing.

Never raises on a garbled stream: anything it cannot read is ``None``.
"""

from __future__ import annotations

import bisect
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger("common.logprobs")

NUMBER_TOKEN_MODES: tuple[str, ...] = ("auto", "single", "digits")

# Characters skipped between a key's colon and its value: whitespace and the
# quote of a number written as a string (``"score": "7"`` — the classifier's
# clamp accepts that, so the distribution should too).
_VALUE_LEAD = " \t\r\n\""

# The answer prefixes tried, longest first, when anchoring on ``content``.
_ANCHOR_LENGTHS = (64, 32, 16, 8)

_DIGITS = re.compile(r"(\d+)(.*)\Z", re.S)
_LEADING_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class Unresolved:
    """Probability mass on a token whose value the stream cannot settle: a
    non-sampled ``1`` under a digit-splitting tokenizer is 1 or 10."""

    values: tuple[int, ...]
    mass: float

    def as_dict(self) -> dict[str, Any]:
        return {"values": list(self.values), "mass": self.mass}


@dataclass(frozen=True)
class Distribution:
    """A distribution over the integer values ``lo..hi`` of one field.

    ``probs`` (value → probability) and ``unresolved`` together sum to 1:
    both are renormalised over the NUMERIC mass at the value position.
    ``coverage`` is that numeric mass before renormalising (1.0 for an
    empirical distribution of samples) — how much of the model's probability
    went to some number at all. ``sampled`` is the value the model actually
    produced (clamped), i.e. what the parsed answer says.
    """

    probs: dict[int, float]
    unresolved: tuple[Unresolved, ...]
    coverage: float
    sampled: int
    lo: int
    hi: int

    def even_split(self) -> dict[int, float]:
        """``probs`` with each unresolved mass spread evenly over its values —
        the assumption ``expected`` and ``entropy_bits`` make."""
        out = {v: self.probs.get(v, 0.0) for v in range(self.lo, self.hi + 1)}
        for u in self.unresolved:
            share = u.mass / len(u.values)
            for v in u.values:
                out[v] = out.get(v, 0.0) + share
        return out

    def mass(self, values: Iterable[int]) -> float:
        """The probability of ``values`` — a LOWER bound: unresolved mass
        counts only when every value it could be is in the set."""
        wanted = set(values)
        total = sum(p for v, p in self.probs.items() if v in wanted)
        total += sum(u.mass for u in self.unresolved if set(u.values) <= wanted)
        return total

    def expected(self) -> float:
        """The mean value (unresolved mass split evenly)."""
        return sum(v * p for v, p in self.even_split().items())

    def entropy_bits(self) -> float:
        """Shannon entropy in bits (unresolved mass split evenly); 0 = certain,
        ``log2(hi - lo + 1)`` = uniform."""
        return -sum(p * math.log2(p) for p in self.even_split().values() if p > 0)

    def certainty(self) -> float:
        """``1 - entropy / max entropy`` — 1 when all mass is on one value."""
        span = self.hi - self.lo + 1
        if span <= 1:
            return 1.0
        return max(0.0, 1.0 - self.entropy_bits() / math.log2(span))


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def resolve_path(data: Any, path: str) -> Any:
    """``data`` at a dotted ``path`` (``choices.0.logprobs.content``): a
    segment of digits indexes a list, any other segment reads a mapping key.
    ``None`` when any step is missing or of the wrong type."""
    current = data
    for part in (path or "").split("."):
        if part == "":
            continue
        if isinstance(current, list) and part.lstrip("-").isdigit():
            index = int(part)
            if not -len(current) <= index < len(current):
                return None
            current = current[index]
        elif isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _logprob(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or value > 0.0 + 1e-6:
        return None
    return float(value)


def _token(entry: Any) -> str:
    if isinstance(entry, Mapping) and isinstance(entry.get("token"), str):
        return entry["token"]
    return ""


def _candidates(entry: Any, top_key: str) -> list[tuple[str, float]]:
    """The position's alternatives plus its sampled token, deduplicated by
    exact token text (the top list normally contains the sampled one), each
    with its probability. Entries with no usable logprob are dropped."""
    seen: set[str] = set()
    out: list[tuple[str, float]] = []
    if not isinstance(entry, Mapping):
        return out
    top = entry.get(top_key)
    rows = list(top) if isinstance(top, list) else []
    rows.append({"token": entry.get("token"), "logprob": entry.get("logprob")})
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("token"), str):
            continue
        lp = _logprob(row.get("logprob"))
        if lp is None or row["token"] in seen:
            continue
        seen.add(row["token"])
        out.append((row["token"], math.exp(lp)))
    return out


def _anchor(stream: str, content: Optional[str]) -> int:
    """Where the answer starts in ``stream``: the last match of a prefix of
    ``content`` (longest that matches), else 0 (the whole stream)."""
    if not content:
        return 0
    text = content.strip()
    for n in _ANCHOR_LENGTHS:
        prefix = text[:n]
        if not prefix:
            continue
        at = stream.rfind(prefix)
        if at >= 0:
            return at
    return 0


def _split_digits(text: str, prefix: str) -> Optional[tuple[str, bool]]:
    """A candidate token's ``(digits, complete)``, or None when it is not a
    number: leading whitespace and the sampled token's own ``prefix`` are
    skipped; ``complete`` is True when something follows the digits inside
    the token (the number ends there)."""
    s = text.lstrip(_VALUE_LEAD)
    if prefix and s.startswith(prefix):
        s = s[len(prefix):].lstrip(_VALUE_LEAD)
    match = _DIGITS.match(s)
    if not match:
        return None
    digits, rest = match.groups()
    return digits, rest != ""


# ---------------------------------------------------------------------------
# The two constructors
# ---------------------------------------------------------------------------


def int_field_distribution(
    entries: Any,
    key: str,
    lo: int,
    hi: int,
    *,
    content: Optional[str] = None,
    top_key: str = "top_logprobs",
    number_tokens: str = "auto",
) -> Optional[Distribution]:
    """The distribution of JSON field ``key``'s integer value in ``lo..hi``.

    Args:
        entries: The logprob entries, ``[{token, logprob, <top_key>: [{token,
                 logprob}]}]`` — an OpenAI-style ``choices[0].logprobs.content``
                 (read it with :func:`resolve_path`).
        key:     The JSON key whose value is wanted (``"score"``).
        lo, hi:  The valid range; values outside it are clamped into it.
        content: The answer text (``choices[0].message.content``), used to
                 skip a reasoning prefix. Optional: without it the last
                 ``"<key>":`` in the whole stream is used.
        top_key: The alternatives list's key in each entry.
        number_tokens: ``auto`` | ``single`` | ``digits`` (module docstring).

    Returns:
        A :class:`Distribution`, or None when there are no entries, no
        ``"<key>":`` in the answer part, or its value is not a number.
    """
    if number_tokens not in NUMBER_TOKEN_MODES:
        raise ValueError(f"number_tokens must be one of {NUMBER_TOKEN_MODES}, got {number_tokens!r}")
    if lo > hi:
        raise ValueError(f"lo {lo} > hi {hi}")
    if not isinstance(entries, list) or not entries:
        return None

    texts = [_token(e) for e in entries]
    starts: list[int] = []
    offset = 0
    for text in texts:
        starts.append(offset)
        offset += len(text)
    stream = "".join(texts)

    pattern = re.compile('"' + re.escape(key) + r'"\s*:')
    found = list(pattern.finditer(stream, _anchor(stream, content)))
    if not found:
        return None
    i = found[-1].end()
    while i < len(stream) and stream[i] in _VALUE_LEAD:
        i += 1
    if i >= len(stream) or not stream[i].isdigit():
        return None

    t = bisect.bisect_right(starts, i) - 1
    sampled_text = texts[t]
    k = i - starts[t]
    prefix = sampled_text[:k].strip(_VALUE_LEAD)
    first = _LEADING_DIGITS.match(sampled_text, k)
    sampled_digits = first.group() if first else ""
    sampled_complete = first is not None and first.end() < len(sampled_text)
    if not sampled_digits:
        return None

    candidates = _candidates(entries[t], top_key)
    if not candidates:
        return None
    numbers: list[tuple[str, bool, float, bool]] = []  # digits, complete, p, is_sampled
    for text, p in candidates:
        parsed = _split_digits(text, prefix)
        if parsed is not None:
            numbers.append((parsed[0], parsed[1], p, text == sampled_text))

    # What the stream says about the tokenizer.
    next_text = texts[t + 1] if t + 1 < len(texts) else ""
    next_digits = _LEADING_DIGITS.match(next_text)
    split_seen = not sampled_complete and next_digits is not None
    multi_seen = any(len(d) >= 2 for d, _, _, _ in numbers)
    if split_seen:
        splits = True
        if number_tokens == "single":
            logger.warning(
                "logprobs: number_tokens is 'single' but the stream splits a number "
                "across tokens (a digit followed by a digit); reading it as split"
            )
    elif multi_seen:
        splits = False
        if number_tokens == "digits":
            logger.warning(
                "logprobs: number_tokens is 'digits' but a candidate token holds a "
                "multi-digit number; reading the stream as whole-number tokens"
            )
    else:
        splits = number_tokens != "single"

    sampled_value = int(sampled_digits + (next_digits.group() if split_seen else ""))

    # The sampled first token's continuation, read from the next position.
    continuation: list[tuple[str, float]] = [("", 1.0)]
    if splits and not sampled_complete and t + 1 < len(entries):
        nxt = _candidates(entries[t + 1], top_key)
        total = sum(p for _, p in nxt)
        if total > 0:
            continuation = []
            for text, p in nxt:
                m = _LEADING_DIGITS.match(text)
                continuation.append((m.group() if m else "", p / total))

    valid = [str(v) for v in range(lo, hi + 1)]
    resolved: dict[int, float] = {}
    unresolved: list[Unresolved] = []
    for digits, complete, p, is_sampled in numbers:
        if is_sampled:
            for more, share in continuation:
                v = _clamp(int(digits + more), lo, hi)
                resolved[v] = resolved.get(v, 0.0) + p * share
            continue
        extensions = [int(s) for s in valid if s != digits and s.startswith(digits)]
        if complete or not splits or not extensions:
            v = _clamp(int(digits), lo, hi)
            resolved[v] = resolved.get(v, 0.0) + p
        else:
            values = tuple(sorted({_clamp(int(digits), lo, hi), *extensions}))
            unresolved.append(Unresolved(values, p))

    numeric = sum(resolved.values()) + sum(u.mass for u in unresolved)
    if numeric <= 0:
        return None
    # Merge unresolved entries over the same value set (two tokens " 1" / "1").
    merged: dict[tuple[int, ...], float] = {}
    for u in unresolved:
        merged[u.values] = merged.get(u.values, 0.0) + u.mass
    return Distribution(
        probs={v: resolved[v] / numeric for v in sorted(resolved)},
        unresolved=tuple(Unresolved(vals, m / numeric) for vals, m in merged.items()),
        coverage=min(1.0, numeric),
        sampled=_clamp(sampled_value, lo, hi),
        lo=lo,
        hi=hi,
    )


def from_samples(values: Sequence[Any], lo: int, hi: int) -> Distribution:
    """The empirical distribution of sampled answers (coverage 1.0), so a
    sampling method feeds the same signal code as logprobs. ``sampled`` is
    the FIRST sample's value. Values are clamped into ``lo..hi``; a value
    that is not a number is skipped.

    Raises:
        ValueError: No numeric value in ``values``.
    """
    clean: list[int] = []
    for value in values:
        try:
            clean.append(_clamp(int(round(float(value))), lo, hi))
        except (TypeError, ValueError):
            continue
    if not clean:
        raise ValueError("from_samples needs at least one numeric value")
    counts: dict[int, int] = {}
    for v in clean:
        counts[v] = counts.get(v, 0) + 1
    n = len(clean)
    return Distribution(
        probs={v: counts[v] / n for v in sorted(counts)},
        unresolved=(),
        coverage=1.0,
        sampled=clean[0],
        lo=lo,
        hi=hi,
    )
