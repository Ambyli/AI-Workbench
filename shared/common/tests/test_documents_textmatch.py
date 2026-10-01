"""Tests for common.documents.textmatch — the four match modes."""

from __future__ import annotations

import pytest

from common.documents import InvalidPatternError, MAX_PATTERN_CHARS, match_text

from .documents_fixtures import make_document

PAGES = [
    "Invoice #INV-2026-0042\nNotice to Owner\nTotal Due: $4,850.00",
    "Payment Terms: Net 30\nNotice to Owner appears again here.",
]


# ---------------------------------------------------------------------------
# contains
# ---------------------------------------------------------------------------


def test_contains_finds_substring_on_both_pages() -> None:
    doc = make_document(PAGES)
    res = match_text(doc, "Notice to Owner", "contains")
    assert res.found is True
    assert res.count == 2
    assert res.pages == [0, 1]
    assert res.best_ratio == 1.0
    assert all("Notice to Owner" in s["text"] for s in res.snippets)


def test_contains_is_case_insensitive_by_default() -> None:
    doc = make_document(["NOTICE TO OWNER"])
    assert match_text(doc, "notice to owner", "contains").found is True
    assert match_text(doc, "notice to owner", "contains", case_sensitive=True).found is False


def test_contains_treats_metacharacters_literally() -> None:
    doc = make_document(["Total Due: $4,850.00"])
    assert match_text(doc, "$4,850.00", "contains").found is True
    assert match_text(doc, "$4.850.00", "contains").found is False


def test_min_count_requires_repeats() -> None:
    doc = make_document(PAGES)
    assert match_text(doc, "Notice to Owner", "contains", min_count=2).found is True
    assert match_text(doc, "Notice to Owner", "contains", min_count=3).found is False


def test_missing_phrase_reports_not_found() -> None:
    res = match_text(make_document(PAGES), "Lien Waiver", "contains")
    assert res.found is False and res.count == 0 and res.pages == [] and res.best_ratio == 0.0


def test_document_without_text_returns_empty_result_not_error() -> None:
    res = match_text(make_document(["", ""]), "anything", "contains")
    assert res.found is False and res.searched_chars == 0


# ---------------------------------------------------------------------------
# exact
# ---------------------------------------------------------------------------


def test_exact_requires_word_boundaries() -> None:
    doc = make_document(["Subtotal line", "Total Due: $4,850.00"])
    assert match_text(doc, "Total", "exact").count == 1  # not the "Subtotal" substring
    assert match_text(doc, "Total", "contains").count == 2


def test_exact_matches_a_whole_line() -> None:
    doc = make_document(["Notice to Owner\nsecond line"])
    assert match_text(doc, "Notice to Owner", "exact").found is True


def test_exact_handles_leading_punctuation() -> None:
    doc = make_document(["Total Due: $4,850.00 today"])
    assert match_text(doc, "$4,850.00", "exact").found is True


# ---------------------------------------------------------------------------
# regex
# ---------------------------------------------------------------------------


def test_regex_matches_a_pattern() -> None:
    doc = make_document(["Case number CASE-77813 filed"])
    res = match_text(doc, r"CASE-\d{5}", "regex")
    assert res.found is True and res.count == 1
    assert "CASE-77813" in res.snippets[0]["text"]


def test_regex_respects_case_sensitivity() -> None:
    doc = make_document(["case-77813"])
    assert match_text(doc, r"CASE-\d{5}", "regex").found is True
    assert match_text(doc, r"CASE-\d{5}", "regex", case_sensitive=True).found is False


def test_invalid_regex_is_rejected() -> None:
    with pytest.raises(InvalidPatternError):
        match_text(make_document(PAGES), "CASE-(\\d{5}", "regex")


def test_overlong_pattern_is_rejected() -> None:
    with pytest.raises(InvalidPatternError) as exc:
        match_text(make_document(PAGES), "a" * (MAX_PATTERN_CHARS + 1), "regex")
    assert str(MAX_PATTERN_CHARS) in str(exc.value)


def test_empty_pattern_is_rejected() -> None:
    with pytest.raises(InvalidPatternError):
        match_text(make_document(PAGES), "", "contains")


# ---------------------------------------------------------------------------
# fuzzy
# ---------------------------------------------------------------------------


def test_fuzzy_matches_ocr_style_noise() -> None:
    # What RapidOCR typically returns for a photographed letter.
    doc = make_document(["NotIce to 0wner\nCASE-77813"])
    res = match_text(doc, "Notice to Owner", "fuzzy", fuzzy_threshold=0.8)
    assert res.found is True
    assert res.best_ratio >= 0.8
    assert res.pages == [0]
    assert res.snippets and res.snippets[0]["ratio"] >= 0.8


def test_fuzzy_exact_text_scores_one() -> None:
    res = match_text(make_document(PAGES), "Notice to Owner", "fuzzy")
    assert res.found is True and res.best_ratio == pytest.approx(1.0)


def test_fuzzy_threshold_gates_the_verdict() -> None:
    doc = make_document(["Notlce to 0wnor"])
    loose = match_text(doc, "Notice to Owner", "fuzzy", fuzzy_threshold=0.7)
    strict = match_text(doc, "Notice to Owner", "fuzzy", fuzzy_threshold=0.99)
    assert loose.found is True
    assert strict.found is False
    # Even on a miss the best ratio is reported, so callers can scale a score.
    assert 0.7 <= strict.best_ratio < 0.99


def test_fuzzy_unrelated_text_scores_low() -> None:
    res = match_text(make_document(["Payment Terms: Net 30"]), "Notice to Owner", "fuzzy")
    assert res.found is False
    assert res.best_ratio < 0.7


def test_fuzzy_counts_each_phrase_once() -> None:
    doc = make_document(["Notice to Owner", "Notice to Owner"])
    res = match_text(doc, "Notice to Owner", "fuzzy")
    assert res.count == 2 and res.pages == [0, 1]


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_as_dict_is_json_safe() -> None:
    import json

    res = match_text(make_document(PAGES), "Notice to Owner", "contains")
    payload = res.as_dict()
    json.dumps(payload)  # must not raise
    assert payload["mode"] == "contains"
    assert payload["pattern"] == "Notice to Owner"
    assert payload["found"] is True
