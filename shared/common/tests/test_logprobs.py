"""common.logprobs — a JSON integer field's distribution from a logprob stream.

The fixtures are shaped like what the box's servers returned on 2026-10-22
(``logprobs: true, top_logprobs: 20`` on a chat completion):

  * muse-glimmer (vLLM): ``10`` is ONE token; ``: 10,`` → ``':' ' ' '10' ','``;
    the score's top list holds other numbers and junk (``' tenth'``,
    ``'<|start|>'``, ``' '``);
  * qwen3.8-solo (vLLM): digits split (``10`` → ``'1' '0'``) and the stream
    carries the reasoning tokens before the answer;
  * qwen3.5-4b (llama.cpp): digits split, real spread, and once ``"score": 0``.
"""

from __future__ import annotations

import logging
import math

import pytest

from common.logprobs import (
    Distribution,
    Unresolved,
    from_samples,
    int_field_distribution,
    resolve_path,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _e(token: str, top: list[tuple[str, float]] | None = None, p: float | None = None) -> dict:
    """One logprob entry. ``top`` = [(token, probability)]; the sampled
    token's probability is its row in ``top`` (or ``p``, or 1.0)."""
    top = top or [(token, p if p is not None else 1.0)]
    sampled_p = p if p is not None else dict(top).get(token, 1.0)
    return {
        "token": token,
        "logprob": math.log(sampled_p),
        "bytes": list(token.encode("utf-8")),
        "top_logprobs": [{"token": t, "logprob": math.log(q)} for t, q in top],
    }


def _plain(text_tokens: list[str]) -> list[dict]:
    return [_e(t) for t in text_tokens]


def _answer(score_entries: list[dict], *, head=('{"', "score", '":', " "),
            tail=(' "verdict', '":', ' "PASS', '",', ' "confidence', '":', " 95",
                  ',', ' "reason', '":', ' "ok', '"}')) -> tuple[list[dict], str]:
    """``{"score": <score tokens>,\\n "verdict": ...}`` as entries + content."""
    entries = _plain(list(head)) + score_entries + _plain(list(tail))
    return entries, "".join(e["token"] for e in entries)


def _muse(score_top, next_token=",\n", next_top=None):
    """A muse-glimmer answer: ``{"score": <one token>,\\n ...``."""
    sampled = max(score_top, key=lambda r: r[1])[0]
    return _answer([_e(sampled, score_top), _e(next_token, next_top)])


QWEN_REASONING = [
    "<think>", "The", " user", " wants", " JSON", " like", ' {"', "score", '":',
    " ", "3", "}", " but", " the", " page", " shows", " it", ".", "</think>", "\n\n",
]


def _qwen(first_top, second_top, *, reasoning=True):
    """A qwen answer with split digits, reasoning (with a decoy ``"score": 3``)
    first. Returns (entries, content) — content is the ANSWER only, as the
    reasoning parser returns it."""
    first = max(first_top, key=lambda r: r[1])[0]
    second = max(second_top, key=lambda r: r[1])[0]
    answer, content = _answer([_e(first, first_top), _e(second, second_top)])
    prefix = _plain(QWEN_REASONING) if reasoning else []
    return prefix + answer, content


# ---------------------------------------------------------------------------
# muse-glimmer: whole-number tokens
# ---------------------------------------------------------------------------


def test_muse_single_token_ten():
    top = [("10", 0.97), ("9", 0.02), (" tenth", 0.002), (" Tenth", 0.001),
           ("<|start|>", 0.001), (" ", 0.001), ("<|message|>", 0.0005), ("8", 0.003),
           ("11", 0.002), (" decade", 0.0005)]
    entries, content = _muse(top)
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d is not None and d.sampled == 10
    numeric = 0.97 + 0.02 + 0.003 + 0.002
    assert d.coverage == pytest.approx(numeric)
    # "11" clamps onto 10, like clamp_answer.
    assert d.probs[10] == pytest.approx((0.97 + 0.002) / numeric)
    assert d.probs[9] == pytest.approx(0.02 / numeric)
    assert d.probs[8] == pytest.approx(0.003 / numeric)
    assert d.unresolved == ()  # whole-number tokens: a "1" here would be 1
    assert sum(d.probs.values()) == pytest.approx(1.0)


def test_muse_a_non_sampled_one_is_one_under_whole_number_tokens():
    top = [("9", 0.7), ("10", 0.2), ("1", 0.1)]
    entries, content = _muse(top, next_token=",")
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d.sampled == 9 and d.unresolved == ()
    assert d.probs == pytest.approx({1: 0.1, 9: 0.7, 10: 0.2})


def test_the_whitespace_token_before_the_digit_is_skipped():
    # Separate ' ' token (muse) and a merged ' 9' token (other tokenizers).
    entries, content = _answer([_e(" 9", [(" 9", 0.6), (" 8", 0.3), ("9", 0.1)]),
                                _e(",")], head=('{"', "score", '":'))
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d.sampled == 9
    assert d.probs == pytest.approx({8: 0.3, 9: 0.7})


# ---------------------------------------------------------------------------
# qwen: split digits, reasoning in the stream
# ---------------------------------------------------------------------------


def test_qwen_one_then_zero_is_ten():
    entries, content = _qwen(
        [("1", 0.999), ("9", 0.0005), ("8", 0.0003), (" ", 0.0002)],
        [("0", 0.98), (",", 0.015), ("\n", 0.004), (".", 0.001)],
    )
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d.sampled == 10
    numeric = 0.999 + 0.0005 + 0.0003
    assert d.coverage == pytest.approx(numeric)
    assert d.probs[10] == pytest.approx(0.999 * 0.98 / numeric)
    assert d.probs[1] == pytest.approx(0.999 * 0.02 / numeric)
    assert d.probs[9] == pytest.approx(0.0005 / numeric)
    assert d.unresolved == ()


def test_qwen_one_then_comma_is_one():
    entries, content = _qwen([("1", 0.9), ("2", 0.1)], [(",", 0.9), ("0", 0.1)])
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d.sampled == 1
    assert d.probs == pytest.approx({1: 0.81, 2: 0.1, 10: 0.09})


def test_sampled_nine_with_a_one_alternative_is_unresolved():
    entries, content = _qwen([("9", 0.8), ("1", 0.15), ("8", 0.05)], [(",", 1.0)])
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d.sampled == 9
    assert d.probs == pytest.approx({8: 0.05, 9: 0.8})
    assert d.unresolved == (Unresolved((1, 10), pytest.approx(0.15)),)
    # A lower bound: the {1, 10} mass is in no band it could straddle.
    assert d.mass(range(7, 11)) == pytest.approx(0.85)   # 8 + 9, not the {1, 10}
    assert d.mass([1, 10]) == pytest.approx(0.15)
    assert d.mass(range(1, 11)) == pytest.approx(1.0)
    assert d.unresolved[0].as_dict() == {"values": [1, 10], "mass": pytest.approx(0.15)}


def test_a_zero_score_clamps_to_one():
    # qwen3.5-4b once answered "score": 0 — sampled '0' 0.914, '1' 0.083.
    entries, content = _qwen([("0", 0.914), ("1", 0.083)], [(",", 1.0)], reasoning=False)
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d.sampled == 1
    numeric = 0.914 + 0.083
    assert d.probs == pytest.approx({1: 0.914 / numeric})
    assert d.unresolved == (Unresolved((1, 10), pytest.approx(0.083 / numeric)),)


def test_reasoning_with_a_decoy_score_is_ignored():
    entries, content = _qwen([("9", 1.0)], [(",", 1.0)])
    assert '"score": 3' in "".join(e["token"] for e in entries)
    with_content = int_field_distribution(entries, "score", 1, 10, content=content)
    without = int_field_distribution(entries, "score", 1, 10)  # last key wins anyway
    assert with_content.sampled == without.sampled == 9


def test_content_that_does_not_match_the_stream_falls_back_to_the_whole_stream():
    entries, _ = _qwen([("7", 1.0)], [(",", 1.0)])
    d = int_field_distribution(entries, "score", 1, 10, content="nothing like it")
    assert d.sampled == 7 and d.probs == {7: 1.0}


def test_a_quoted_number_is_read():
    entries, content = _answer([_e('"', [('"', 1.0)]), _e("7", [("7", 0.6), ("6", 0.4)]),
                                _e('",')])
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    assert d.sampled == 7 and d.probs == pytest.approx({6: 0.4, 7: 0.6})


# ---------------------------------------------------------------------------
# Missing data → None, never an exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entries", [None, [], "garbage", {"content": []}])
def test_no_logprobs_is_none(entries):
    assert int_field_distribution(entries, "score", 1, 10) is None


def test_key_absent_is_none():
    entries = _plain(['{"', "verdict", '":', ' "PASS', '"}'])
    assert int_field_distribution(entries, "score", 1, 10) is None


def test_a_non_number_value_is_none():
    entries = _plain(['{"', "score", '":', " null", "}"])
    assert int_field_distribution(entries, "score", 1, 10) is None


def test_garbled_entries_do_not_raise():
    entries, content = _muse([("8", 0.9), ("7", 0.1)])
    entries[-2] = {"nonsense": True}           # a tail token with no text
    entries[4]["top_logprobs"].append({"token": "6", "logprob": "high"})
    entries.insert(0, 42)
    d = int_field_distribution(entries, "score", 1, 10, content=content)
    # The stream lost tokens, but the score is still found and read.
    assert d.sampled == 8 and d.probs == pytest.approx({7: 0.1, 8: 0.9})
    # Losing the key's own token loses the field: None, not an exception.
    entries[2] = {"nonsense": True}
    assert int_field_distribution(entries, "score", 1, 10, content=content) is None


def test_bad_arguments_raise():
    with pytest.raises(ValueError):
        int_field_distribution([], "score", 1, 10, number_tokens="bytes")
    with pytest.raises(ValueError):
        int_field_distribution([], "score", 10, 1)


# ---------------------------------------------------------------------------
# number_tokens: pinned modes, and the stream winning
# ---------------------------------------------------------------------------


def test_digits_mode_marks_a_one_unresolved_single_mode_does_not():
    entries, content = _muse([("9", 0.8), ("1", 0.2)], next_token=",")
    digits = int_field_distribution(entries, "score", 1, 10, content=content,
                                    number_tokens="digits")
    single = int_field_distribution(entries, "score", 1, 10, content=content,
                                    number_tokens="single")
    assert [u.values for u in digits.unresolved] == [(1, 10)]
    assert single.unresolved == () and single.probs == pytest.approx({1: 0.2, 9: 0.8})


def test_a_pinned_mode_the_stream_contradicts_is_logged_and_the_stream_wins(caplog):
    split, content = _qwen([("1", 1.0)], [("0", 0.9), (",", 0.1)])
    with caplog.at_level(logging.WARNING, logger="common.logprobs"):
        d = int_field_distribution(split, "score", 1, 10, content=content,
                                   number_tokens="single")
    assert d.sampled == 10 and d.probs == pytest.approx({1: 0.1, 10: 0.9})
    assert any("'single'" in r.getMessage() for r in caplog.records)

    caplog.clear()
    whole, content = _muse([("10", 0.6), ("9", 0.3), ("1", 0.1)])
    with caplog.at_level(logging.WARNING, logger="common.logprobs"):
        d = int_field_distribution(whole, "score", 1, 10, content=content,
                                   number_tokens="digits")
    assert d.unresolved == () and d.probs == pytest.approx({1: 0.1, 9: 0.3, 10: 0.6})
    assert any("'digits'" in r.getMessage() for r in caplog.records)


def test_a_custom_top_key():
    entries, content = _muse([("6", 0.5), ("5", 0.5)])
    for e in entries:
        e["alts"] = e.pop("top_logprobs")
    d = int_field_distribution(entries, "score", 1, 10, content=content, top_key="alts")
    assert d.probs == pytest.approx({5: 0.5, 6: 0.5})


# ---------------------------------------------------------------------------
# from_samples, resolve_path, Distribution helpers
# ---------------------------------------------------------------------------


def test_from_samples():
    d = from_samples([8, 8, 9, 0, 12, "x"], 1, 10)
    assert d.sampled == 8 and d.coverage == 1.0 and d.unresolved == ()
    assert d.probs == pytest.approx({1: 0.2, 8: 0.4, 9: 0.2, 10: 0.2})
    with pytest.raises(ValueError):
        from_samples(["x"], 1, 10)


def test_resolve_path():
    data = {"choices": [{"logprobs": {"content": [1, 2]}}]}
    assert resolve_path(data, "choices.0.logprobs.content") == [1, 2]
    assert resolve_path(data, "choices.1.logprobs.content") is None
    assert resolve_path(data, "choices.0.logprobs.missing") is None
    assert resolve_path({"choices": [{"logprobs": None}]}, "choices.0.logprobs.content") is None
    assert resolve_path(data, "choices.x") is None
    assert resolve_path([[0, 7]], "0.-1") == 7


def test_distribution_statistics():
    d = Distribution(probs={9: 0.5, 10: 0.25}, unresolved=(Unresolved((1, 10), 0.25),),
                     coverage=1.0, sampled=9, lo=1, hi=10)
    split = d.even_split()
    assert split[1] == pytest.approx(0.125) and split[10] == pytest.approx(0.375)
    assert d.expected() == pytest.approx(0.125 * 1 + 0.5 * 9 + 0.375 * 10)
    assert d.entropy_bits() == pytest.approx(
        -sum(p * math.log2(p) for p in (0.125, 0.5, 0.375)))
    certain = from_samples([7, 7], 1, 10)
    assert certain.entropy_bits() == 0 and certain.certainty() == 1.0
    uniform = from_samples(list(range(1, 11)), 1, 10)
    assert uniform.certainty() == pytest.approx(0.0)
