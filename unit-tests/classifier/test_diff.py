"""Change detection, on the generated before/after fixture pair.

``regions/scene_before.png`` and ``scene_after.png`` are one synthetic yard
photographed twice: the camera moved ~6 px and rotated ~1°, a car appeared,
and a shed went. That shift is the point — two pixel-aligned images would let
a diff that never aligns anything pass this file.

What is asserted:

  * the pair aligns, and produces an ``added`` region where the car is and a
    ``removed`` one where the shed was;
  * two unrelated images do NOT align, and return no regions rather than a
    guess;
  * identical images align and find nothing;
  * a featureless image says so instead of failing;
  * and at the ``run_compare`` level, with the vision model scripted: the
    ``diff`` block and the ``diff-e0-p0.svg`` layer appear, and the aggregate
    score is BYTE-IDENTICAL to the same job with ``diff`` off — diff regions
    are not criteria and must never reach the weighted score.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_diff.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import pathlib

import numpy as np
import pytest

from compare.diff import detect_changes
from config import DIFF_CHANGE_ADDED, DIFF_CHANGE_CHANGED, DIFF_CHANGE_REMOVED

FIXTURES = pathlib.Path(__file__).resolve().parent / "regions"
BEFORE = FIXTURES / "scene_before.png"
AFTER = FIXTURES / "scene_after.png"

# Where make_fixtures.py puts the two objects that change. The diff's blobs
# follow the drawn shapes but are not pixel-identical to them (a car's wheels
# overhang its body, a shed's roof overhangs its walls), so the assertions are
# about OVERLAP, not equality.
CAR = (120, 500, 330, 590)
SHED = (700, 360, 830, 470)


pytestmark = pytest.mark.skipif(
    not BEFORE.exists() or not AFTER.exists(),
    reason="run unit-tests/classifier/regions/make_fixtures.py first",
)


def _read(path: pathlib.Path):
    import cv2

    image = cv2.imread(str(path))
    assert image is not None, f"could not read {path}"
    return image


def _overlaps(bbox, target) -> bool:
    ax1, ay1, ax2, ay2 = bbox
    bx1, by1, bx2, by2 = target
    return not (ax2 < bx1 or ax1 > bx2 or ay2 < by1 or ay1 > by2)


def _by_change(result, kind: str) -> list[dict]:
    return [r for r in result.regions if r["attrs"]["change"] == kind]


# ---------------------------------------------------------------------------
# detect_changes
# ---------------------------------------------------------------------------


def test_before_after_aligns_and_finds_the_two_changes():
    result = detect_changes(_read(BEFORE), _read(AFTER))

    assert result.aligned is True
    assert result.inliers >= 30
    assert result.homography is not None and len(result.homography) == 3
    assert result.note is None

    added = _by_change(result, "added")
    removed = _by_change(result, "removed")
    assert added, "the car that appeared should be an `added` region"
    assert removed, "the shed that went should be a `removed` region"

    assert any(_overlaps(r["attrs"]["bbox"], CAR) for r in added)
    assert any(_overlaps(r["attrs"]["bbox"], SHED) for r in removed)

    # Every region is a polygon in the SUBJECT's frame, scored 0-1 by how
    # different it is.
    height, width = _read(AFTER).shape[:2]
    for region in result.regions:
        assert region["kind"] == "polygon" and len(region["points"]) >= 3
        assert 0.0 <= region["score"] <= 1.0
        for x, y in region["points"]:
            assert 0 <= x <= width and 0 <= y <= height


def test_the_house_that_did_not_change_is_not_a_region():
    """The alignment has to be real: an unaligned diff lights up every edge."""
    result = detect_changes(_read(BEFORE), _read(AFTER))
    house = (250, 250, 650, 470)
    # The car overlaps nothing of the house and the shed is well clear of it,
    # so nothing in the house's own footprint should be reported.
    inside_house = [
        r for r in result.regions
        if _overlaps(r["attrs"]["bbox"], house)
        and not _overlaps(r["attrs"]["bbox"], CAR)
        and not _overlaps(r["attrs"]["bbox"], SHED)
    ]
    assert inside_house == []


def test_identical_images_find_nothing():
    after = _read(AFTER)
    result = detect_changes(after, after.copy())
    assert result.aligned is True
    assert result.regions == []
    assert "no change" in result.note


def test_unrelated_images_do_not_align_and_guess_nothing():
    result = detect_changes(_read(FIXTURES / "text_blocks.png"), _read(AFTER))
    assert result.aligned is False
    assert result.regions == []
    assert result.homography is None
    assert "not two views of the same scene" in result.note


def test_featureless_image_says_so():
    flat = np.full((400, 400, 3), 128, dtype=np.uint8)
    result = detect_changes(flat, flat.copy())
    assert result.aligned is False
    assert "featureless" in result.note


def test_missing_pixels_is_a_note_not_an_exception():
    assert detect_changes(None, _read(AFTER)).note is not None
    assert detect_changes(np.zeros((0, 0, 3), np.uint8), _read(AFTER)).aligned is False


def test_min_inliers_is_honoured():
    """Raise the bar past what the pair can meet and the answer becomes "no"."""
    result = detect_changes(_read(BEFORE), _read(AFTER), min_inliers=100_000)
    assert result.aligned is False
    assert result.regions == []
    assert "CLASSIFIER_DIFF_MIN_INLIERS" in result.note


def test_as_dict_caps_the_inline_regions():
    result = detect_changes(_read(BEFORE), _read(AFTER))
    block = result.as_dict(max_regions=1)
    assert len(block["regions"]) == 1
    assert block["regions_truncated"] is (len(result.regions) > 1)
    assert set(block) == {
        "aligned", "inliers", "homography", "regions", "regions_truncated", "note"
    }


# ---------------------------------------------------------------------------
# run_compare
# ---------------------------------------------------------------------------


def _scoring_call(_prompt=None):
    """A stand-in for ``analysis.pipeline.call_vllm`` — one criterion, a fixed
    score."""

    async def _call(prompt):
        return {
            "assessment": {
                "overall_verdict": "PASS",
                "overall_score": 8,
                "per_criterion_scores": {
                    "yard is tidy": {
                        "score": 8, "verdict": "PASS", "confidence": 70,
                        "reason": "I observe a mown lawn. Therefore it is tidy.",
                    }
                },
            }
        }

    return _call


def _compare_payload(*, diff_on: bool, job_id: str) -> dict:
    return {
        "job_id": job_id,
        "request": {
            "image": {
                "data": base64.b64encode(AFTER.read_bytes()).decode(), "type": "base64"
            },
            # One cv criterion so the SUBJECT has regions of its own (and so a
            # `p0.svg` the diff append must not clobber), plus one llm
            # criterion so the scripted scoring call is exercised.
            "criteria": [
                {"name": "has sky", "type": "cv"},
                {"name": "yard is tidy", "type": "llm", "hint": "quality"},
            ],
            "ocr": "never",
            "examples": [
                {
                    "data": base64.b64encode(BEFORE.read_bytes()).decode(),
                    "type": "base64",
                    "weight": 0.5,
                }
            ],
            "regions": {
                "enabled": True, "layers": ["svg"], "diff": diff_on,
            },
        },
    }


def _run_compare(monkeypatch, *, diff_on: bool, job_id: str) -> dict:
    from analysis import pipeline
    from jobs import runners

    monkeypatch.setattr(pipeline, "call_vllm", _scoring_call())
    return asyncio.run(runners.run_compare(_compare_payload(diff_on=diff_on, job_id=job_id)))


def test_run_compare_attaches_the_diff_and_its_layer(monkeypatch):
    from common.vision import ArtifactStore
    from config import ARTIFACT_DIR

    result = _run_compare(monkeypatch, diff_on=True, job_id="difftest1")
    block = result["example_results"][0]["diff"]

    assert block["aligned"] is True and block["inliers"] >= 30
    assert block["changes"]["added"] >= 1 and block["changes"]["removed"] >= 1
    assert block["regions"], "the inline copy should carry the change regions"
    assert all(r["source"] == "diff" for r in block["regions"])
    assert block["artifacts"]["slug"].startswith("diff-e0")

    store = ArtifactStore(ARTIFACT_DIR)
    names = {f["name"] for f in store.list("difftest1")}
    assert "diff-e0-p0.svg" in names
    assert "p0.svg" in names  # the subject's own layer survived the append

    # The diff is filed under a synthetic criterion in the subject's
    # regions.json, where the artifact endpoints can find it.
    stored = store.read_json("difftest1", "regions.json")
    assert "_diff:e0" in stored["criteria"]
    entry = stored["criteria"]["_diff:e0"]
    assert entry["sources"] == ["diff"] and entry["count"] >= 2
    assert entry["type"] == "diff"

    # …and the manifest lists it, so nothing promises a file that is not there.
    manifest = store.manifest("difftest1")
    assert "_diff:e0" in manifest["criteria"]
    assert "diff-e0-p0.svg" in {f["name"] for f in manifest["files"]}


def test_diff_does_not_move_a_single_score(monkeypatch):
    """The property the whole design rests on: diff regions are not criteria."""
    without = _run_compare(monkeypatch, diff_on=False, job_id="difftest2")
    with_diff = _run_compare(monkeypatch, diff_on=True, job_id="difftest3")

    assert with_diff["aggregate"] == without["aggregate"]
    assert (
        with_diff["example_results"][0]["combined_score"]
        == without["example_results"][0]["combined_score"]
    )
    assert (
        with_diff["example_results"][0]["similarity"]
        == without["example_results"][0]["similarity"]
    )
    assert (
        with_diff["input_analysis"]["assessment"]["per_criterion_scores"]
        ["yard is tidy"]["score"]
        == without["input_analysis"]["assessment"]["per_criterion_scores"]
        ["yard is tidy"]["score"]
    )
    # `diff` is present only when it was asked for.
    assert "diff" not in without["example_results"][0]


def test_pre_generated_example_is_skipped_with_a_reason(monkeypatch):
    from analysis import pipeline
    from jobs import runners

    monkeypatch.setattr(pipeline, "call_vllm", _scoring_call())
    payload = _compare_payload(diff_on=True, job_id="difftest4")
    payload["request"]["examples"][0]["pre_generated_analysis"] = {
        "assessment": {
            "overall_score": 8,
            "per_criterion_scores": {
                "yard is tidy": {"score": 8, "verdict": "PASS", "confidence": 70}
            },
        },
        "verdict": "PASS",
    }
    result = asyncio.run(runners.run_compare(payload))

    block = result["example_results"][0]["diff"]
    assert block["aligned"] is False and block["regions"] == []
    assert "pre_generated_analysis" in block["note"]
    assert any("pre_generated_analysis" in n for n in result["artifacts"]["notes"])


def test_regions_examples_renders_the_example_layers(monkeypatch):
    from common.vision import ArtifactStore
    from config import ARTIFACT_DIR
    from analysis import pipeline
    from jobs import runners

    monkeypatch.setattr(pipeline, "call_vllm", _scoring_call())
    payload = _compare_payload(diff_on=False, job_id="difftest5")
    payload["request"]["regions"]["examples"] = True
    payload["request"]["criteria"] = [{"name": "has sky", "type": "cv"}]
    result = asyncio.run(runners.run_compare(payload))

    store = ArtifactStore(ARTIFACT_DIR)
    names = {f["name"] for f in store.list("difftest5")}
    assert "e0.p0.svg" in names
    assert "e0.regions.json" in names
    assert "p0.svg" in names

    example = result["example_results"][0]["example_analysis"]
    assert example["artifacts"] is not None
    assert {f["name"] for f in example["artifacts"]["files"]} == {
        "e0.p0.svg", "e0.regions.json"
    }
    entry = example["assessment"]["per_criterion_scores"]["has sky"]
    assert entry["artifacts"]["regions_url"].startswith(
        "/jobs/difftest5/artifacts/e0.regions.json?criterion="
    )


def test_a_compare_run_increments_the_aligned_counter(monkeypatch):
    """`classifier_diff_jobs_total{aligned="true"}` moves once per diffed example."""
    before = _metric("true")
    _run_compare(monkeypatch, diff_on=True, job_id="difftest6")
    assert _metric("true") == before + 1


def _metric(aligned: str) -> float:
    from metrics import diff_jobs

    return diff_jobs.labels(aligned=aligned)._value.get()


def test_change_classes_are_the_three_documented_ones():
    assert {DIFF_CHANGE_ADDED, DIFF_CHANGE_REMOVED,
            DIFF_CHANGE_CHANGED} == {"added", "removed", "changed"}
