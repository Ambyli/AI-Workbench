"""The model lanes' own code (signals/tasks/*) without a model: the PII label
mapping and span rules, the real ``PiiModel.scan`` on a fake tokenizer and
session, the kNN shares, the injection head + tail windows, fastText's
input cleaning, and the wire features the scheduler builds from them."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from common.pii import ENTITY_TYPES, SENSITIVE_TYPES
from common.signals import WireError

from signals.scheduler import injection_features, language_features, nearest_features, pii_features
from signals.tasks.embed import nearest
from signals.tasks.injection import InjectionModel, positive_index
from signals.tasks.language import LanguageModel
from signals.tasks.pii import LABEL_TYPES, PiiModel, Span, decode_bio, entity_type, merge_spans, summarise

# gravitee-io/bert-small-pii-detection's labels (its model card / config.json).
MODEL_LABELS = ("AGE", "COORDINATE", "CREDIT_CARD", "DATE_TIME", "EMAIL_ADDRESS", "FINANCIAL", "HONORIFIC",
                "IBAN_CODE", "IMEI", "IP_ADDRESS", "LOCATION", "MAC_ADDRESS", "NRP", "ORGANIZATION", "PASSWORD",
                "PERSON", "PHONE_NUMBER", "TITLE", "URL", "US_BANK_NUMBER", "US_DRIVER_LICENSE", "US_ITIN",
                "US_LICENSE_PLATE", "US_PASSPORT", "US_SSN")


# ── label mapping ──────────────────────────────────────────────────────────────

def test_every_model_label_maps_into_the_shared_vocabulary():
    produced = {entity_type(label, "77 Main St") for label in MODEL_LABELS} | \
               {entity_type(label, "Provo") for label in MODEL_LABELS}
    assert produced <= set(ENTITY_TYPES)
    assert set(LABEL_TYPES) <= set(MODEL_LABELS)


@pytest.mark.parametrize("label,kind", [
    ("PERSON", "person"), ("EMAIL_ADDRESS", "email"), ("PHONE_NUMBER", "phone"),
    ("US_SSN", "gov_id"), ("US_PASSPORT", "gov_id"), ("US_DRIVER_LICENSE", "gov_id"), ("US_ITIN", "gov_id"),
    ("CREDIT_CARD", "card_number"), ("IBAN_CODE", "bank_account"), ("US_BANK_NUMBER", "bank_account"),
    ("FINANCIAL", "account_number"),
    # Any date is not a birth date, and the rest never force.
    ("DATE_TIME", "other"), ("AGE", "other"), ("ORGANIZATION", "other"), ("URL", "other"),
    ("IP_ADDRESS", "other"), ("PASSWORD", "other"), ("COORDINATE", "other"),
])
def test_label_mapping(label, kind):
    assert entity_type(label, "whatever 12") == kind


@pytest.mark.parametrize("piece,kind", [
    ("77 N 400 E", "street_address"), ("11 Willow Bend", "street_address"), ("Calle Reforma 222", "street_address"),
    ("unit 4B", "street_address"),
    ("Provo", "location"), ("Utah", "location"), ("Highland", "location"),
    ("Calle ٣", "location"),                      # only ASCII digits count, as measured
])
def test_a_location_with_a_digit_is_a_street_address(piece, kind):
    assert entity_type("LOCATION", piece) == kind


def test_person_and_location_are_not_sensitive_on_their_own():
    assert "person" not in SENSITIVE_TYPES and "location" not in SENSITIVE_TYPES
    assert {"street_address", "email", "phone", "gov_id", "card_number", "bank_account",
            "account_number"} == set(SENSITIVE_TYPES)


# ── span decoding ──────────────────────────────────────────────────────────────

def test_decode_bio_follows_the_measured_rules():
    labels = ["O", "B-PERSON", "I-PERSON", "O", "B-LOCATION", "I-LOCATION", "I-PERSON", "B-LOCATION", "I-LOCATION",
              "I-LOCATION"]
    scores = [0.99, 0.9, 0.8, 0.99, 0.7, 0.6, 0.9, 0.95, 0.4, 0.9]
    offsets = [(0, 0), (0, 4), (5, 8), (9, 11), (12, 14), (15, 16), (17, 20), (21, 24), (25, 26), (27, 30)]
    special = [1, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    spans = decode_bio(labels, scores, offsets, special, 0.5)
    assert spans == [
        Span("PERSON", 0, 8, 0.9),             # B + I of one entity
        Span("LOCATION", 12, 16, 0.7),         # closed by an I- of ANOTHER entity...
        Span("PERSON", 17, 20, 0.9),           # ...which starts its own span
        Span("LOCATION", 21, 24, 0.95),        # closed by a token below min_prob
        Span("LOCATION", 27, 30, 0.9),         # an I- with nothing open starts a span
    ]


def test_decode_bio_skips_special_and_empty_tokens():
    assert decode_bio(["B-PERSON", "I-PERSON"], [0.9, 0.9], [(0, 0), (3, 3)], [1, 0], 0.5) == []


def test_merge_spans_counts_an_entity_two_windows_saw_once():
    merged = merge_spans([Span("LOCATION", 10, 20, 0.6), Span("LOCATION", 15, 30, 0.8), Span("PERSON", 0, 4, 0.9),
                          Span("PERSON", 4, 8, 0.7)])
    assert merged == [Span("PERSON", 0, 4, 0.9), Span("PERSON", 4, 8, 0.7), Span("LOCATION", 10, 30, 0.8)]


def test_summarise_reports_types_counts_and_best_scores_only():
    text = "Jane at 77 N 400 E in Provo"
    spans = [Span("PERSON", 0, 4, 0.9712), Span("LOCATION", 8, 18, 0.65), Span("LOCATION", 22, 27, 0.8)]
    assert summarise(text, spans) == {"location": {"count": 1, "max_score": 0.8},
                                      "person": {"count": 1, "max_score": 0.971},
                                      "street_address": {"count": 1, "max_score": 0.65}}
    assert summarise("my name is Sam", [Span("PERSON", 11, 14, 0.97)]) == {"person": {"count": 1, "max_score": 0.97}}


# ── PiiModel.scan on a fake tokenizer + session ────────────────────────────────

class _Enc:
    def __init__(self, ids, offsets, special, overflowing=()):
        self.ids, self.offsets, self.special_tokens_mask = list(ids), list(offsets), list(special)
        self.overflowing = list(overflowing)
        self.attention_mask = [1] * len(self.ids)


class _Tok:
    def __init__(self, encode):
        self._encode = encode
        self.calls = []

    def no_padding(self):
        self.calls.append("no_padding")

    def enable_truncation(self, max_length, stride=0, direction="right"):
        self.calls.append(("truncation", max_length, stride, direction))
        self.direction = direction

    def encode(self, text):
        return self._encode(self, text)


class _Session:
    def __init__(self, logits_for, names=("input_ids", "attention_mask")):
        self._logits_for = logits_for
        self._names = names
        self.feeds = []

    def get_inputs(self):
        return [SimpleNamespace(name=n) for n in self._names]

    def run(self, _outputs, feed):
        self.feeds.append(feed)
        return [self._logits_for(feed["input_ids"][0].tolist())]


PII_LABELS = {0: "O", 1: "B-PERSON", 2: "I-PERSON", 3: "B-LOCATION", 4: "I-LOCATION"}
TOKEN_LABEL = {101: 0, 102: 0, 7: 1, 8: 0, 9: 3, 10: 4, 11: 4, 12: 4}   # token id -> label index


def _pii_model():
    text = "Jane at 77 N 400 E"
    #        Jane  at     77      N        400      E
    win1 = _Enc([101, 7, 8, 9, 10, 102], [(0, 0), (0, 4), (5, 7), (8, 10), (11, 12), (0, 0)], [1, 0, 0, 0, 0, 1])
    win2 = _Enc([101, 9, 10, 11, 12, 102], [(0, 0), (8, 10), (11, 12), (13, 16), (17, 18), (0, 0)],
                [1, 0, 0, 0, 0, 1])
    first = _Enc(win1.ids, win1.offsets, win1.special_tokens_mask, overflowing=[win2])

    def logits(ids):
        out = np.zeros((1, len(ids), len(PII_LABELS)), dtype=np.float32)
        for i, tok in enumerate(ids):
            out[0, i, TOKEN_LABEL[tok]] = 10.0
        return out

    tokenizer = _Tok(lambda tok, t: first)
    session = _Session(logits)
    return text, PiiModel(session, tokenizer, PII_LABELS, window=256, stride=16), tokenizer, session


def test_pii_scan_windows_merges_overlap_and_maps_the_span():
    text, model, tokenizer, session = _pii_model()
    assert tokenizer.calls == ["no_padding", ("truncation", 256, 16, "right")]
    scan = model.scan(text)
    first = next(scan)                                    # after window 1: "77 N" so far
    assert first == {"person": {"count": 1, "max_score": 1.0}, "street_address": {"count": 1, "max_score": 1.0}}
    second = next(scan)
    with pytest.raises(StopIteration) as stop:
        next(scan)
    # The two windows' LOCATION spans (8-12, 8-18) are one street address, not two.
    assert stop.value.value == second == {"person": {"count": 1, "max_score": 1.0},
                                          "street_address": {"count": 1, "max_score": 1.0}}
    assert len(session.feeds) == 2
    assert set(session.feeds[0]) == {"input_ids", "attention_mask"}   # no token_type_ids: the graph has none


def test_pii_scan_of_an_empty_text_runs_no_model():
    _text, model, _tok, session = _pii_model()
    scan = model.scan("   ")
    with pytest.raises(StopIteration) as stop:
        next(scan)
    assert stop.value.value == {} and session.feeds == []


# ── nearest ────────────────────────────────────────────────────────────────────

def _unit(*rows):
    m = np.asarray(rows, dtype=np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def test_nearest_shares_sum_to_one_and_order_by_share():
    vectors = _unit([1, 0, 0], [0.9, 0.1, 0], [0.8, 0.2, 0], [0, 1, 0], [0, 0, 1])
    ids, routes = ["c1", "c2", "c3", "r1", "g1"], ["code", "code", "code", "reason", "general"]
    found = nearest(vectors, ids, routes, _unit([1, 0.05, 0])[0], k=4)
    assert [nb["id"] for nb in found["neighbours"]] == ["c1", "c2", "c3", "r1"]
    assert list(found["routes"]) == ["code", "reason"]
    assert found["routes"]["code"]["count"] == 3 and found["routes"]["reason"]["count"] == 1
    assert sum(r["share"] for r in found["routes"].values()) == pytest.approx(1.0, abs=1e-3)
    assert found["routes"]["code"]["best"] == found["neighbours"][0]["similarity"]


def test_nearest_clips_negative_similarity_out_of_the_share_and_clamps_k():
    vectors = _unit([1, 0], [-1, 0.01])
    found = nearest(vectors, ["a", "b"], ["x", "y"], _unit([1, 0])[0], k=50)
    assert len(found["neighbours"]) == 2
    assert found["routes"]["x"]["share"] == 1.0 and found["routes"]["y"]["share"] == 0.0
    assert nearest(np.zeros((0, 2), dtype=np.float32), [], [], _unit([1, 0])[0], 5) == {"neighbours": [], "routes": {}}


# ── injection ──────────────────────────────────────────────────────────────────

def test_positive_index_finds_the_jailbreak_label():
    assert positive_index({0: "benign", 1: "jailbreak"}) == 1
    assert positive_index({0: "INJECTION", 1: "SAFE"}) == 0
    with pytest.raises(RuntimeError):
        positive_index({0: "a", 1: "b"})


def test_injection_scores_the_head_and_the_tail_window():
    def encode(tok, text):
        if len(text) <= 10:
            return _Enc([2, 5, 1], [(0, 0)] * 3, [1, 0, 1])
        if tok.direction == "right":
            rest = _Enc([2, 7, 1], [(0, 0)] * 3, [1, 0, 1])
            return _Enc([2, 5, 6, 1], [(0, 0)] * 4, [1, 0, 0, 1], overflowing=[rest])
        return _Enc([2, 8, 9, 1], [(0, 0)] * 4, [1, 0, 0, 1])

    def logits(ids):
        return np.asarray([[0.0, 5.0 if 9 in ids else -5.0]], dtype=np.float32)

    tokenizer = _Tok(encode)
    model = InjectionModel(_Session(logits), tokenizer, 1, max_tokens=512)
    assert model.windows("short", 256) == [[2, 5, 1]]
    assert model.windows("a long text indeed", 256) == [[2, 5, 6, 1], [2, 8, 9, 1]]   # head, then the LAST tokens
    assert ("truncation", 256, 0, "left") in tokenizer.calls
    scan = model.scan("a long text indeed", 4096)                          # clamped to the model's 512
    assert next(scan) is None and next(scan) is None
    with pytest.raises(StopIteration) as stop:
        next(scan)
    assert stop.value.value == {"score": round(float(np.exp(5) / (np.exp(5) + 1)), 4), "windows": 2}
    assert ("truncation", 512, 0, "right") in tokenizer.calls


# ── language ───────────────────────────────────────────────────────────────────

class _FastText:
    def __init__(self):
        self.lines = []

    def predict(self, line, k=1):
        self.lines.append(line)
        return (("__label__es",), (1.00001,))


def test_language_collapses_whitespace_and_clamps_confidence():
    ft = _FastText()
    model = LanguageModel(ft)
    assert model.detect("hola\n\namigo\tmio ") == {"language": "es", "confidence": 1.0}
    assert ft.lines == ["hola amigo mio"]


def test_a_text_without_letters_has_no_language():
    ft = _FastText()
    assert LanguageModel(ft).detect("12 + 30 = 42 ??") == {"language": "und", "confidence": 0.0}
    assert ft.lines == []


# ── wire features ──────────────────────────────────────────────────────────────

def test_the_features_are_the_wire_types_and_validate():
    pii = pii_features({"person": {"count": 1, "max_score": 0.97}}, False, True)
    assert pii.to_json() == {"entities": {"person": {"count": 1, "max_score": 0.97}}, "complete": False,
                             "cached": True}
    with pytest.raises(WireError):
        pii_features({"not_a_type": {"count": 1}}, True, False)
    found = nearest(_unit([1, 0], [0, 1]), ["a", "b"], ["x", "y"], _unit([1, 0])[0], 2)
    assert nearest_features(found, False).to_json()["neighbours"][0] == {"id": "a", "route": "x", "similarity": 1.0}
    assert injection_features({"score": 1.0000001, "windows": 2}, False).score == 1.0
    assert language_features({"language": "und", "confidence": 0.0}, True).to_json() == \
           {"language": "und", "confidence": 0.0, "cached": True}
