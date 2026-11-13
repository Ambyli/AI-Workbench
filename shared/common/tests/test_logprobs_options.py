"""common.logprobs — a one-token answer's distribution over fixed labels.

Shaped like what the box's servers returned on 2026-11-12 for a readout
(``enable_thinking: false``, ``max_tokens: 1``, ``logprobs: true``):
qwen3.8-solo answered ``9`` with the other digits and ``[9``-style variants in
its top list; a 5-option choice put ``B`` first with ``[B`` carrying a few
percent; qwen3.5-4b's yes / no split ``A`` 0.86 + ``[A`` 0.12.

Pinned: the first non-whitespace position is read; bracket / punctuation /
whitespace variants count as their label (exact case) and are summed;
coverage is the matched mass before renormalising (clamped to 1, while probs
always sum to 1); the unseen-label estimate (``n_unseen × p_min_listed``,
capped by ``1 - coverage``) is tiny on a confident answer and large on a
spread one; nothing garbled raises; the temperature and Platt helpers.
"""

from __future__ import annotations

import math

import pytest

from common.logprobs import OptionReadout, option_distribution, platt, softmax_temperature

DIGITS = tuple("0123456789")


def _e(token: str, top: list[tuple[str, float]], p: float | None = None) -> dict:
    """One entry: the sampled ``token`` and its alternatives ``[(token, p)]``."""
    sampled = p if p is not None else dict(top).get(token, 1.0)
    return {
        "token": token,
        "logprob": math.log(sampled),
        "top_logprobs": [{"token": t, "logprob": math.log(q)} for t, q in top],
    }


def test_a_confident_digit_with_variants_summed():
    top = [("9", 0.90), ("8", 0.04), ("[9", 0.02), (" 9", 0.01), ("7", 0.01), ("<|im_end|>", 0.01)]
    r = option_distribution([_e("9", top)], DIGITS)
    assert isinstance(r, OptionReadout)
    assert r.first_token == "9" and r.best() == "9"
    assert r.coverage == pytest.approx(0.98)
    assert r.probs["9"] == pytest.approx(0.93 / 0.98)
    assert r.probs["8"] == pytest.approx(0.04 / 0.98)
    assert sum(r.probs.values()) == pytest.approx(1.0)
    assert list(r.probs) == list(DIGITS)          # every label, in order
    assert r.probs["0"] == 0.0 and "0" not in r.logprobs
    assert r.logprobs["9"] == pytest.approx(math.log(0.93))  # raw: the summed variants
    assert r.n_unseen == 7 and r.listed == 6
    assert r.p_min_listed == pytest.approx(0.01)
    assert r.unseen_bound == pytest.approx(min(0.02, 7 * 0.01))


@pytest.mark.parametrize("variant", ["[B", "(B", "B]", " B", "B.", "B:", "(B)", " [B", "B\n"])
def test_bracket_and_punctuation_variants_count_as_their_label(variant):
    top = [("A", 0.5), (variant, 0.5)]
    r = option_distribution([_e("A", top)], ("A", "B"))
    assert r.probs == {"A": pytest.approx(0.5), "B": pytest.approx(0.5)}
    assert r.coverage == pytest.approx(1.0)


def test_matching_is_exact_case_and_never_partial():
    top = [("b", 0.3), ("B", 0.2), ("Bx", 0.2), ("10", 0.3)]
    r = option_distribution([_e("b", top)], ("A", "B", "1"))
    assert r.probs["B"] == pytest.approx(1.0)       # only "B" itself matched
    assert r.coverage == pytest.approx(0.2)
    assert r.probs["A"] == 0.0 and r.probs["1"] == 0.0


def test_the_first_non_whitespace_position_is_read():
    entries = [_e(" ", [(" ", 0.6), ("7", 0.4)]), _e("\n", [("\n", 1.0)]),
               _e("3", [("3", 0.7), ("4", 0.3)])]
    r = option_distribution(entries, DIGITS)
    assert r.first_token == "3"
    assert r.probs["3"] == pytest.approx(0.7) and r.probs["4"] == pytest.approx(0.3)


def test_an_all_whitespace_stream_reads_its_first_position():
    r = option_distribution([_e(" ", [(" ", 0.5), (" 7", 0.3), ("8", 0.2)])], DIGITS)
    assert r.first_token == " "
    assert r.probs["7"] == pytest.approx(0.6) and r.coverage == pytest.approx(0.5)


def test_the_sampled_token_counts_when_it_is_not_in_the_list():
    entry = _e("5", [("4", 0.2)], p=0.7)
    r = option_distribution([entry], DIGITS)
    assert r.probs["5"] == pytest.approx(0.7 / 0.9)
    assert r.listed == 1 and r.p_min_listed == pytest.approx(0.2)


def test_a_duplicate_token_is_counted_once():
    entry = _e("5", [("5", 0.5), ("5", 0.5), ("6", 0.5)])
    r = option_distribution([entry], DIGITS)
    assert r.probs["5"] == pytest.approx(0.5) and r.coverage == pytest.approx(1.0)


def test_an_over_counted_stream_still_renormalises_to_one():
    # Two spellings of one label, each near its own ceiling: the matched rows
    # add up past 1. coverage is clamped; probs are divided by the real total.
    r = option_distribution([_e("9", [("9", 0.7), ("[9", 0.5), ("8", 0.1)])], DIGITS)
    assert r.coverage == 1.0
    assert sum(r.probs.values()) == pytest.approx(1.0)
    assert r.probs["9"] == pytest.approx(1.2 / 1.3) and r.probs["8"] == pytest.approx(0.1 / 1.3)
    assert all(0.0 <= p <= 1.0 for p in r.probs.values())


def test_a_confident_answer_has_a_tiny_unseen_bound():
    # 0a's 19-option choice: N 0.98, a few letters listed, the tail ~0.
    letters = tuple("ABCDEFGHJKLMNPQRSTU")
    top = [("N", 0.98), ("[N", 0.015), ("M", 0.004)] + [("x%d" % i, 6e-6) for i in range(17)]
    r = option_distribution([_e("N", top)], letters)
    assert r.n_unseen == 17
    assert r.unseen_bound == pytest.approx(17 * 6e-6)
    assert r.unseen_bound < 0.02


def test_a_spread_answer_over_more_labels_than_listed_has_a_large_bound():
    labels = tuple("ABCDEFGHIJ")
    top = [(label, 0.08) for label in "ABCDE"]       # half the labels, 0.4 of the mass
    r = option_distribution([_e("A", top)], labels)
    assert r.n_unseen == 5
    assert r.unseen_bound == pytest.approx(min(1 - 0.4, 5 * 0.08))
    assert r.unseen_bound >= 0.02


def test_nothing_matched_is_a_readout_with_zero_coverage():
    r = option_distribution([_e("hello", [("hello", 0.9), ("world", 0.1)])], DIGITS)
    assert r.coverage == 0.0 and r.best() is None
    assert set(r.probs.values()) == {0.0} and r.logprobs == {}
    assert r.n_unseen == 10 and r.unseen_bound == pytest.approx(min(1.0, 10 * 0.1))


def test_no_alternatives_list_bounds_unseen_labels_by_the_rest():
    entry = {"token": "7", "logprob": math.log(0.6)}
    r = option_distribution([entry], DIGITS)
    assert r.probs["7"] == pytest.approx(1.0) and r.coverage == pytest.approx(0.6)
    assert r.listed == 0 and r.unseen_bound == pytest.approx(0.4)


@pytest.mark.parametrize("entries", [
    None, [], "garbage", [None], [{"token": None}], [{"token": "7", "logprob": "x"}],
    [{"token": "7", "logprob": 0.5, "top_logprobs": "nope"}], [7, "x"],
])
def test_garbled_streams_are_none_never_an_exception(entries):
    assert option_distribution(entries, DIGITS) is None


def test_garbled_rows_are_skipped():
    entry = {"token": "7", "logprob": math.log(0.5), "top_logprobs": [
        None, {"token": 3, "logprob": -1}, {"token": "8", "logprob": float("nan")},
        {"token": "6", "logprob": 3.0}, {"token": "7", "logprob": math.log(0.5)},
        {"token": "9", "logprob": math.log(0.25)}]}
    r = option_distribution([entry], DIGITS)
    assert r.probs["7"] == pytest.approx(2 / 3) and r.probs["9"] == pytest.approx(1 / 3)
    assert r.listed == 2


def test_a_custom_top_key():
    entry = {"token": "1", "logprob": 0.0, "alts": [{"token": "1", "logprob": 0.0}]}
    assert option_distribution([entry], DIGITS, top_key="alts").listed == 1


@pytest.mark.parametrize("labels", [(), ("A", "A"), ("A", ""), ("A", 3)])
def test_bad_labels_are_a_value_error(labels):
    with pytest.raises(ValueError):
        option_distribution([_e("A", [("A", 1.0)])], labels)


def test_softmax_temperature():
    lps = {"a": math.log(0.6), "b": math.log(0.3)}
    plain = softmax_temperature(lps, 1.0)
    assert plain == {"a": pytest.approx(2 / 3), "b": pytest.approx(1 / 3)}
    flat = softmax_temperature(lps, 2.0)
    assert flat["a"] < plain["a"] and sum(flat.values()) == pytest.approx(1.0)
    sharp = softmax_temperature(lps, 0.5)
    assert sharp["a"] == pytest.approx(0.36 / 0.45)
    assert softmax_temperature({}, 1.5) == {}
    for bad in (0, -1, float("nan"), float("inf"), True, "1"):
        with pytest.raises(ValueError):
            softmax_temperature(lps, bad)


def test_platt():
    assert platt(0.5) == pytest.approx(0.5)
    assert platt(0.9, 1.0, 0.0) == pytest.approx(0.9)
    assert platt(0.9, 2.0) == pytest.approx(1 / (1 + 3 ** -1))   # logit 2.197 / 2
    assert platt(0.5, 1.0, math.log(3)) == pytest.approx(0.75)
    assert 0.0 < platt(0.0) < 1e-9 and 1 - 1e-9 < platt(1.0) < 1.0
    with pytest.raises(ValueError):
        platt(0.5, 0)
    with pytest.raises(ValueError):
        platt(float("nan"))
