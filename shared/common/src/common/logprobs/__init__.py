"""common.logprobs — what a model's token probabilities say about one answer field.

A model asked for ``{"score": 7, ...}`` writes one number, but an
OpenAI-compatible server that is asked for ``logprobs: true, top_logprobs: N``
also returns, for every generated token, the N tokens it could have written
there and their log-probabilities. At the position where the score is written
that list IS the model's distribution over scores — a far better confidence
signal than a ``confidence`` number the model types into the same JSON after
it has already committed to the score.

This package reads that distribution generically, for any client of any
OpenAI-style chat-completions server that answers a JSON object with an
integer field:

    int_field_distribution(entries, key, lo, hi, *, content=None,
                           top_key="top_logprobs", number_tokens="auto")
                     → Distribution | None — the field's value distribution
                       over ``lo..hi`` (``extract`` explains how: answer
                       anchoring past a reasoning prefix, single-token vs
                       split-digit numbers, clamping, ``unresolved`` mass,
                       ``coverage``). None when the stream has no such field.
    from_samples(values, lo, hi)
                     → Distribution — the empirical distribution of n sampled
                       answers (coverage 1.0), so a "sample n times" method
                       feeds the same downstream code.
    resolve_path(data, "choices.0.logprobs.content")
                     → the entries list (or None) — where a provider keeps
                       them is configuration, not code.
    Distribution     ``probs`` {value: p}, ``unresolved`` (``Unresolved``:
                     values + mass), ``coverage``, ``sampled``; ``mass()``
                     (a lower bound), ``expected()``, ``entropy_bits()``,
                     ``certainty()``, ``even_split()``.
    NUMBER_TOKEN_MODES  ("auto", "single", "digits").

And for a ONE-TOKEN answer over a fixed set of labels (a digit, a letter —
a "readout": reasoning off, one generated token, the answer read from that
token's probabilities instead of from what was written):

    option_distribution(entries, labels, *, top_key="top_logprobs")
                     → OptionReadout | None — the FIRST non-whitespace
                       generated position's distribution over ``labels``
                       (``options`` explains how): a candidate token matches
                       a label after trimming whitespace and ``[ ( ) ] . :``
                       (``[B``, ``(B``, ``B]``, `` B`` all count as ``B``,
                       summed), exact case. None when the stream has no
                       usable position.
    OptionReadout    ``probs`` {label: p} (renormalised, every label),
                     ``logprobs`` {label: raw log p} (seen labels),
                     ``coverage`` (the matched mass before renormalising),
                     ``first_token``, ``n_unseen`` (labels absent from the
                     top-k), ``p_min_listed`` (the smallest listed
                     probability), ``unseen_bound`` = min(1 - coverage,
                     n_unseen × p_min_listed) — an estimate of what the
                     unseen labels hold (one spelling each; not a guarantee,
                     ``options`` says why) — ``listed``, ``best()``.
    softmax_temperature(logprobs, t)
                     → {label: p} ∝ exp(logprob / t) — temperature scaling
                       of a readout (a fitted calibration).
    platt(p, t=1.0, bias=0.0)
                     → sigmoid(logit(p) / t + bias) — Platt scaling of one
                       probability (a yes / no readout).

Measured on the box (2026-10-22): vLLM (muse-glimmer — ``10`` is ONE token;
qwen3.8-solo — digits split, and its stream includes the reasoning tokens)
and llama.cpp (qwen3.5-4b — digits split) all answer the request. Claude and
OpenAI reasoning models return no token probabilities; a caller decides per
model whether to ask (the classifier: ``models.yaml`` ``confidence.method``).

Stdlib only, pure, never raises on a garbled stream (only on a bad argument).
Logs one WARNING (logger ``common.logprobs``) when a pinned ``number_tokens``
contradicts the stream — never the text.

Measured on the box (2026-11-12, the readout): with ``enable_thinking:
false`` and ``max_tokens: 1`` qwen3.8-solo (vLLM) and qwen3.5-4b (llama.cpp)
write the answer token in ``content``, every digit and capital letter is one
token, and ``[B``-style variants carry 0.5-12% of the mass.

Used by: ``classifier`` (``ai/classifier/llm/client.py`` → ``llm/confidence.py``;
``call_readout`` → ``option_distribution``).
"""

from common.logprobs.extract import (
    NUMBER_TOKEN_MODES,
    Distribution,
    Unresolved,
    from_samples,
    int_field_distribution,
    resolve_path,
)
from common.logprobs.options import (
    OptionReadout,
    option_distribution,
    platt,
    softmax_temperature,
)

__all__ = [
    "NUMBER_TOKEN_MODES",
    "Distribution",
    "OptionReadout",
    "Unresolved",
    "from_samples",
    "int_field_distribution",
    "option_distribution",
    "platt",
    "resolve_path",
    "softmax_temperature",
]
