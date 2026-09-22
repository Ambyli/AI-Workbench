"""Unit tests for the detector service's model-free parts.

Everything here runs without torch, without transformers, and without a GPU:
request parsing, the coordinate contract, and response shaping. Those are the
parts where a bug is silent — a mis-scaled box looks plausible until someone
crops by it — and the parts that do not need 600 MB of weights to exercise.

Run from the repo root, in any environment that has pytest, pydantic and
Pillow (the classifier's workspace venv does):

    UV_LINK_MODE=copy uv run --package classifier pytest unit-tests/detector -q

The model path itself (`Owlv2Detector._detect_prepared`) is deliberately NOT
covered here — mocking a transformer's output shape tests the mock. It is
verified by running the real thing; see DETECTOR.md § Verifying a change.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ai", "detector"),
)

from detectors.base import Detection, OpenVocabularyDetector  # noqa: E402
from models import (  # noqa: E402
    apply_max_per_label,
    build_response,
    parse_labels,
    parse_max_per_label,
    parse_threshold,
)


# ── parse_labels ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        (["tree", "house"], ["tree", "house"]),
        ('["tree", "house"]', ["tree", "house"]),
        ("tree, house", ["tree", "house"]),
        ("tree", ["tree"]),
        ("  tree ,, house  ", ["tree", "house"]),
        (["tree", "tree", "house"], ["tree", "house"]),
    ],
)
def test_parse_labels_accepts_every_spelling(raw, expected):
    assert parse_labels(raw) == expected


def test_parse_labels_preserves_caller_order_and_case():
    """The label is how a caller matches a box back to its own criterion, so
    it must come back exactly as sent — not sorted, not lower-cased."""
    assert parse_labels(["Swimming Pool", "ROOF vent"]) == ["Swimming Pool", "ROOF vent"]


@pytest.mark.parametrize("raw", [None, "", "   ", [], [" ", ""], '[]'])
def test_parse_labels_refuses_nothing_to_look_for(raw):
    with pytest.raises(ValueError):
        parse_labels(raw)


def test_parse_labels_refuses_bad_json_and_non_strings():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_labels('["tree",')
    with pytest.raises(ValueError, match="must be strings"):
        parse_labels(["tree", 7])


def test_parse_labels_caps_the_list():
    import config

    with pytest.raises(ValueError, match="DETECTOR_MAX_LABELS"):
        parse_labels([f"thing {i}" for i in range(config.MAX_LABELS + 1)])


# ── parse_threshold / parse_max_per_label ──────────────────────────────────
def test_parse_threshold_defaults_and_coerces():
    import config

    assert parse_threshold(None) == config.DEFAULT_THRESHOLD
    assert parse_threshold("") == config.DEFAULT_THRESHOLD
    assert parse_threshold("0.4") == pytest.approx(0.4)
    assert parse_threshold(0) == 0.0
    assert parse_threshold(1) == 1.0


@pytest.mark.parametrize("raw", ["-0.1", "1.5", "banana", [0.3]])
def test_parse_threshold_refuses_out_of_range(raw):
    with pytest.raises(ValueError):
        parse_threshold(raw)


def test_parse_max_per_label():
    assert parse_max_per_label(None) is None
    assert parse_max_per_label("") is None
    assert parse_max_per_label("3") == 3
    for bad in ("0", "-1", "two", 0):
        with pytest.raises(ValueError):
            parse_max_per_label(bad)


# ── the coordinate contract ────────────────────────────────────────────────
class _Fake(OpenVocabularyDetector):
    """A detector whose 'model' returns whatever boxes the test hands it."""

    family = "fake"

    def __init__(self, boxes, *, max_image_side=1024):
        super().__init__("fake/model", "cpu", max_image_side=max_image_side)
        self._boxes = boxes
        self._loaded = True

    def _detect_prepared(self, image, labels, threshold):
        return [
            Detection(label=labels[0], score=score, box=box)
            for box, score in self._boxes
        ]


def _image(width: int, height: int):
    from PIL import Image

    return Image.new("RGB", (width, height), "white")


def test_prepare_downscales_the_long_side_only():
    det = _Fake([], max_image_side=100)
    prepared, scale = det.prepare(_image(400, 200))
    assert prepared.size == (100, 50)
    assert scale == pytest.approx(0.25)


def test_prepare_never_upscales():
    det = _Fake([], max_image_side=1024)
    original = _image(40, 30)
    prepared, scale = det.prepare(original)
    assert prepared is original and scale == 1.0


def test_prepare_scale_is_measured_from_the_rounded_size():
    """A 1-px rounding error in the resize compounds into a visibly offset box
    on a big photo, so `scale` is prepared_width / original_width — not the
    ratio we asked the resizer for."""
    det = _Fake([], max_image_side=100)
    prepared, scale = det.prepare(_image(333, 100))
    assert scale == pytest.approx(prepared.size[0] / 333)


def test_boxes_come_back_in_original_pixels():
    # Prepared to 100x50 from 400x200, so a box at (10,10)-(50,25) in
    # prepared pixels is (40,40)-(200,100) in the original.
    det = _Fake([((10, 10, 50, 25), 0.9)], max_image_side=100)
    out = det.detect(_image(400, 200), ["tree"], 0.1)
    assert out[0].box == (40.0, 40.0, 200.0, 100.0)


def test_boxes_are_clipped_to_the_image():
    """OWLv2's square padding lets a box run past the right or bottom edge of
    a non-square image. A consumer that crops by those numbers gets an error,
    so they are clipped rather than returned as the model produced them."""
    det = _Fake([((-20, -5, 500, 400), 0.8)], max_image_side=1000)
    out = det.detect(_image(200, 100), ["tree"], 0.1)
    assert out[0].box == (0.0, 0.0, 200.0, 100.0)


def test_inverted_corners_are_normalised():
    det = _Fake([((80, 60, 20, 10), 0.7)], max_image_side=1000)
    (x1, y1, x2, y2) = det.detect(_image(200, 100), ["tree"], 0.1)[0].box
    assert (x1, y1, x2, y2) == (20.0, 10.0, 80.0, 60.0)


def test_results_are_sorted_by_descending_score():
    det = _Fake([((0, 0, 10, 10), 0.2), ((0, 0, 20, 20), 0.9)], max_image_side=1000)
    scores = [d.score for d in det.detect(_image(200, 100), ["tree"], 0.1)]
    assert scores == [0.9, 0.2]


# ── grouping and response shaping ──────────────────────────────────────────
def _det(label, score):
    return Detection(label=label, score=score, box=(0, 0, 10, 10))


def test_every_requested_label_gets_a_key_even_with_no_hits():
    """"Looked and found none" and "did not look" must not be the same
    answer — a missing key makes them indistinguishable."""
    grouped = apply_max_per_label([_det("tree", 0.9)], ["tree", "house"], None)
    assert list(grouped) == ["tree", "house"]
    assert len(grouped["tree"]) == 1
    assert grouped["house"] == []


def test_max_per_label_keeps_the_best_per_label():
    detections = [_det("tree", 0.9), _det("tree", 0.5), _det("house", 0.8)]
    grouped = apply_max_per_label(detections, ["tree", "house"], 1)
    assert [d.score for d in grouped["tree"]] == [0.9]
    assert [d.score for d in grouped["house"]] == [0.8]


def test_unrequested_labels_are_dropped_not_raised():
    grouped = apply_max_per_label([_det("bicycle", 0.9)], ["tree"], None)
    assert grouped == {"tree": []}


def test_build_response_shape():
    grouped = apply_max_per_label(
        [_det("tree", 0.9), _det("tree", 0.4)], ["tree", "house"], None
    )
    body = build_response(
        model="google/owlv2-base-patch16-ensemble",
        device="cpu",
        family="owl",
        elapsed_ms=123.456,
        threshold=0.25,
        labels=["tree", "house"],
        width=800,
        height=600,
        grouped=grouped,
    )
    assert body["model"] == "google/owlv2-base-patch16-ensemble"
    assert body["device"] == "cpu"
    assert body["family"] == "owl"
    assert body["elapsed_ms"] == 123.5
    assert body["threshold"] == 0.25
    assert body["image"] == {"width": 800, "height": 600}
    assert body["counts"] == {"tree": 2, "house": 0}
    assert body["labels"] == ["tree", "house"]
    # by_label and detections are the same boxes, two views.
    assert len(body["detections"]) == 2
    assert [d["score"] for d in body["detections"]] == [0.9, 0.4]
    assert body["by_label"]["house"] == []
    assert body["detections"][0]["box"] == [0.0, 0.0, 10.0, 10.0]


def test_detection_rounding_is_stable():
    det = Detection(label="tree", score=0.123456789, box=(1.23456, 2.0, 3.0, 4.0))
    assert det.as_dict() == {
        "label": "tree",
        "score": 0.1235,
        "box": [1.2, 2.0, 3.0, 4.0],
    }


# ── the family factory ─────────────────────────────────────────────────────
def test_factory_maps_owl_ids_without_importing_torch():
    from detectors import build_detector

    det = build_detector(
        "google/owlv2-base-patch16-ensemble", "cpu", max_image_side=1024
    )
    assert det.family == "owl"
    assert det.loaded is False  # construction touches no weights


def test_factory_refuses_grounding_dino_with_an_actionable_message():
    from detectors import build_detector

    with pytest.raises(NotImplementedError, match="detectors/__init__.py"):
        build_detector("IDEA-Research/grounding-dino-base", "cpu", max_image_side=1024)


def test_factory_refuses_an_unknown_family():
    from detectors import build_detector

    with pytest.raises(ValueError, match="matches no detector family"):
        build_detector("meta-llama/Llama-3", "cpu", max_image_side=1024)
