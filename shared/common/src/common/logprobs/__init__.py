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

Measured on the box (2026-10-22): vLLM (muse-glimmer — ``10`` is ONE token;
qwen3.8-solo — digits split, and its stream includes the reasoning tokens)
and llama.cpp (qwen3.5-4b — digits split) all answer the request. Claude and
OpenAI reasoning models return no token probabilities; a caller decides per
model whether to ask (the classifier: ``models.yaml`` ``confidence.method``).

Stdlib only, pure, never raises on a garbled stream (only on a bad argument).
Logs one WARNING (logger ``common.logprobs``) when a pinned ``number_tokens``
contradicts the stream — never the text.

Used by: ``classifier`` (``ai/classifier/llm/client.py`` → ``llm/confidence.py``).
"""

from common.logprobs.extract import (
    NUMBER_TOKEN_MODES,
    Distribution,
    Unresolved,
    from_samples,
    int_field_distribution,
    resolve_path,
)

__all__ = [
    "NUMBER_TOKEN_MODES",
    "Distribution",
    "Unresolved",
    "from_samples",
    "int_field_distribution",
    "resolve_path",
]
