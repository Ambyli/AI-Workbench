"""``bin/grounding_experiment.py``'s ``absent`` truth (ZT-527).

A truth file may list, per image, ``"absent": ["<criterion>", ...]`` beside
``criteria`` and ``decoys``: criteria that are NOT on the page. Pinned here,
on synthetic data (no model — the loop's model call is scripted, and no
real bill is read):

  * the loader — ``absent`` accepted, ``criteria`` optional with it,
    repeats collapsed, and refused: a name both absent and boxed (or a
    decoy), a list that is not names, an unknown key;
  * the scoring — an absent run is ``expected: "absent"``, CORRECT when no
    box was accepted and WRONG when one was, ``landed_on`` naming the decoy
    a wrong box landed on, ``hit`` null so it never reaches a hit rate; a
    present run is unchanged but for ``expected: "present"``;
  * the summary — absent pairs reported on their own (``correct_rate``,
    ``wrong_accept_rate``, ``bbox_null_rate``, ``landed_on_rates``), never
    in ``hit_rate`` / ``false_accept_rate``;
  * ``--write-failures`` takes an absent pair right in under 50% of its
    ``off`` runs; ``--references-from-truth`` never draws a PASS example
    from a page where the criterion is absent; ``--fail-from-decoys`` maps
    an absent-only criterion;
  * a whole ``run()`` on two synthetic pages: the absent criterion is asked
    like any other, and its record and summary say so.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_grounding_experiment.py -q -p no:cacheprovider
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib

import pytest

from common.vision import PageGeometry

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "ai" / "classifier" / "bin" / \
    "grounding_experiment.py"
_spec = importlib.util.spec_from_file_location("grounding_experiment", _SCRIPT)
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)

GEOMETRY = PageGeometry(page=0, width=400, height=300, working_scale=1.0)
CHART = "the monthly usage chart"
AMOUNT = "the amount due"
LOOKALIKE = "usage look-alike"


def _write(tmp_path, data) -> pathlib.Path:
    path = tmp_path / "truth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def test_absent_is_loaded_beside_criteria_and_decoys(tmp_path):
    truth = ge._load_truth(_write(tmp_path, {
        "a.png": {"criteria": {AMOUNT: [10, 10, 50, 20]},
                  "decoys": {LOOKALIKE: [[100, 100, 200, 200]]},
                  "absent": [CHART, CHART]},
        "b.png": {"absent": [CHART]},                       # criteria optional with absent
        "c.png": {"criteria": {CHART: [0, 0, 10, 10]}},      # absent optional
    }))
    assert truth["a.png"]["absent"] == [CHART]               # repeats collapsed
    assert truth["a.png"]["criteria"] == {AMOUNT: [[10.0, 10.0, 50.0, 20.0]]}
    assert truth["b.png"] == {"criteria": {}, "decoys": {}, "absent": [CHART]}
    assert truth["c.png"]["absent"] == [] and truth["c.png"]["decoys"] == {}


@pytest.mark.parametrize("entry, fragment", [
    ({"criteria": {CHART: [0, 0, 10, 10]}, "absent": [CHART]},
     f"['{CHART}'] is both absent and a criterion with boxes"),
    ({"criteria": {}, "decoys": {CHART: [0, 0, 10, 10]}, "absent": [CHART]},
     f"['{CHART}'] is both absent and a decoy"),
    ({"criteria": {}, "absent": CHART}, "absent must be a list of criterion names"),
    ({"criteria": {}, "absent": [""]}, "absent must be a list of criterion names"),
    ({"criteria": {}, "absent": [3]}, "absent must be a list of criterion names"),
    ({"criteria": {}, "missing": [CHART]}, "must be {\"criteria\""),
    ({"decoys": {}}, "criteria optional with absent"),          # neither criteria nor absent
])
def test_bad_absent_entries_are_refused(tmp_path, entry, fragment):
    with pytest.raises(SystemExit) as exc:
        ge._load_truth(_write(tmp_path, {"a.png": entry}))
    assert fragment in str(exc.value)


# ---------------------------------------------------------------------------
# Scoring one run
# ---------------------------------------------------------------------------


TRUTH = {
    "criteria": {AMOUNT: [[10.0, 10.0, 60.0, 30.0]]},
    "decoys": {LOOKALIKE: [[100.0, 100.0, 300.0, 250.0]]},
    "absent": [CHART],
}


def test_an_absent_run_is_right_when_nothing_was_accepted():
    assert ge._against_truth(None, CHART, TRUTH, GEOMETRY) == {
        "expected": "absent", "correct": True, "hit": None, "truth_copy": None,
        "iou": None, "center_error_grid": None, "landed_on": None,
    }


def test_an_absent_run_is_wrong_when_a_box_was_accepted_and_says_where():
    on_lookalike = ge._against_truth([150.0, 150.0, 250.0, 200.0], CHART, TRUTH, GEOMETRY)
    assert on_lookalike == {
        "expected": "absent", "correct": False, "hit": None, "truth_copy": None,
        "iou": None, "center_error_grid": None, "landed_on": LOOKALIKE,
    }
    on_amount = ge._against_truth([20.0, 15.0, 50.0, 25.0], CHART, TRUTH, GEOMETRY)
    assert (on_amount["correct"], on_amount["landed_on"]) == (False, AMOUNT)
    nowhere = ge._against_truth([350.0, 260.0, 390.0, 290.0], CHART, TRUTH, GEOMETRY)
    assert (nowhere["correct"], nowhere["landed_on"]) == (False, None)


def test_a_present_run_is_scored_as_before():
    hit = ge._against_truth([12.0, 12.0, 58.0, 28.0], AMOUNT, TRUTH, GEOMETRY)
    assert (hit["expected"], hit["correct"], hit["hit"], hit["landed_on"]) == (
        "present", None, True, None)
    assert hit["truth_copy"] == 0 and hit["iou"] > 0.5
    miss = ge._against_truth([150.0, 150.0, 250.0, 200.0], AMOUNT, TRUTH, GEOMETRY)
    assert (miss["hit"], miss["landed_on"]) == (False, LOOKALIKE)
    assert ge._against_truth(None, AMOUNT, TRUTH, GEOMETRY)["hit"] is False
    untruthed = ge._against_truth(None, "a logo", TRUTH, GEOMETRY)
    assert untruthed["expected"] is None and untruthed["hit"] is None


# ---------------------------------------------------------------------------
# The summary and --write-failures
# ---------------------------------------------------------------------------


def _record(image, criterion, *, accepted, bbox_null=False, config="off", **truth):
    return {
        "image": image, "criterion": criterion, "config": config, "repeat": 1,
        "attempt_1_valid": accepted, "verify_scores": [9] if accepted else [],
        "accepted": accepted, "accepted_attempt": 1 if accepted else None,
        "bbox_null": bbox_null, "calls": 2, "detector_iou": None, "attempt_log": [],
        "call_log": [],
        **{"expected": None, "correct": None, "hit": None, "truth_copy": None, "iou": None,
           "center_error_grid": None, "landed_on": None, **truth},
    }


def _absent(image, *, accepted, **kw):
    return _record(image, CHART, accepted=accepted, expected="absent", correct=not accepted,
                   landed_on=LOOKALIKE if accepted else None, **kw)


def _present(image, *, hit, **kw):
    return _record(image, AMOUNT, accepted=True, expected="present", hit=hit, iou=0.5,
                   center_error_grid=5.0, **kw)


def test_the_summary_reports_absent_pairs_apart_from_the_hit_rate():
    records = [
        _present("a.png", hit=True), _present("a.png", hit=False),
        _absent("b.png", accepted=False, bbox_null=True),
        _absent("b.png", accepted=False),
        _absent("b.png", accepted=False, bbox_null=True),
        _absent("b.png", accepted=True),
    ]
    summary = ge._summarise(records, 1.0, "m", ["off"], True, "crop")
    overall = summary["configs"]["off"]["overall"]
    # The hit rate and false accepts are the PRESENT pair's alone.
    assert overall["hit_rate"] == 0.5 and overall["false_accept_rate"] == 0.5
    assert overall["absent"] == {
        "n": 4, "correct_rate": 0.75, "wrong_accept_rate": 0.25, "bbox_null_rate": 0.5,
        "landed_on_rates": {LOOKALIKE: 0.25},
    }
    per = summary["configs"]["off"]["per_criterion"]
    assert "hit_rate" not in per[CHART] and per[CHART]["absent"]["n"] == 4
    assert "absent" not in per[AMOUNT] and per[AMOUNT]["hit_rate"] == 0.5
    ge._print_summary(summary)                               # prints, never raises


def test_a_summary_without_absent_pairs_has_no_absent_block():
    summary = ge._summarise([_present("a.png", hit=True)], 1.0, "m", ["off"], True, "crop")
    assert "absent" not in summary["configs"]["off"]["overall"]


def test_write_failures_takes_an_absent_pair_right_in_under_half_its_runs():
    records = [
        # b.png: absent, right once in three → a failure.
        _absent("b.png", accepted=True), _absent("b.png", accepted=True),
        _absent("b.png", accepted=False),
        # c.png: absent, right twice in three → not.
        _absent("c.png", accepted=False), _absent("c.png", accepted=False),
        _absent("c.png", accepted=True),
        # a.png: present, hit once in two → not (50% is not under 50%).
        _present("a.png", hit=True), _present("a.png", hit=False),
        # d.png: present, never hit → a failure.
        _present("d.png", hit=False),
        # Guided runs never count.
        _absent("c.png", accepted=True, config="all"), _absent("c.png", accepted=True,
                                                                config="all"),
    ]
    failing, measured = ge._failures(records)
    assert failing == {"b.png": [CHART], "d.png": [AMOUNT]}
    assert measured == 4


# ---------------------------------------------------------------------------
# Examples are never drawn from an absent page
# ---------------------------------------------------------------------------


def test_references_from_truth_never_picks_a_page_where_it_is_absent():
    truth = {
        "cand.png": {"criteria": {}, "decoys": {}, "absent": [CHART]},
        "absent.png": {"criteria": {AMOUNT: [[0.0, 0.0, 9.0, 9.0]]}, "decoys": {},
                       "absent": [CHART]},
        "real.png": {"criteria": {CHART: [[0.0, 0.0, 9.0, 9.0]]}, "decoys": {}, "absent": []},
    }
    available = set(truth)
    picks = {ge._truth_reference("cand.png", CHART, repeat, truth, {}, available)
             for repeat in range(1, 6)}
    assert picks == {"real.png"}
    # With no page that really has it, the pair runs unguided.
    del truth["real.png"]
    assert ge._truth_reference("cand.png", CHART, 1, truth, {}, set(truth)) is None


def test_fail_from_decoys_maps_an_absent_criterion():
    truth = {
        "a.png": {"criteria": {}, "decoys": {LOOKALIKE: [[0.0, 0.0, 9.0, 9.0]]},
                  "absent": [CHART]},
    }
    assert ge._fail_map([f"{LOOKALIKE}=usage chart"], truth) == {CHART: LOOKALIKE}


# ---------------------------------------------------------------------------
# A whole run on synthetic pages
# ---------------------------------------------------------------------------


def _png(path: pathlib.Path) -> None:
    import cv2
    import numpy as np

    cv2.imwrite(str(path), np.full((300, 400, 3), 200, dtype=np.uint8))


def _args(tmp_path, truth_path, **overrides) -> argparse.Namespace:
    values = dict(
        images=str(tmp_path / "images"), truth=str(truth_path), criteria=None, subset=None,
        model=None, configs="off", reference_image=None, references=None,
        references_from_truth=False, groups=None, write_failures=None, fail_from_decoys=None,
        verify_images="crop", refine_mode="grid", reference_text=False, verify_partial=False,
        verify_min_span=0.0, repeats=2, attempts=1, out=str(tmp_path / "out.json"),
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class _Model:
    """``call_model_json`` by call label: every ask answers ``bbox`` (the
    look-alike's box, or null), the refine nothing usable, the verify 9."""

    def __init__(self, bbox):
        self.bbox = bbox
        self.labels: list[str] = []

    async def __call__(self, prompt, *, label="", model=None):
        self.labels.append(label)
        if label.startswith("bbox/"):
            return {"bbox": self.bbox, "reason": "scripted"}
        if label.startswith("verify/"):
            return {"score": 9, "reason": "scripted"}
        return None


@pytest.mark.parametrize("bbox, correct", [(None, True), ([375, 333, 750, 833], False)],
                         ids=["not-found", "boxed-the-look-alike"])
def test_a_run_asks_the_absent_criterion_and_scores_it_apart(
    tmp_path, monkeypatch, bbox, correct
):
    from llm import client as llm_client
    from llm import usage as llm_usage

    (tmp_path / "images").mkdir()
    _png(tmp_path / "images" / "a.png")
    truth_path = _write(tmp_path, {
        # The look-alike fills the middle; the grid box [375, 333, 750, 833]
        # is x 150-300, y 100-250 px on this 400×300 page — inside it.
        "a.png": {"criteria": {AMOUNT: [10, 10, 60, 30]},
                  "decoys": {LOOKALIKE: [100, 90, 320, 260]},
                  "absent": [CHART]},
    })
    model = _Model(bbox)
    monkeypatch.setattr(llm_client, "call_model_json", model)
    # run() swaps the usage writer for its own log; put it back afterwards.
    monkeypatch.setattr(llm_usage, "record_call", llm_usage.record_call)
    failures = tmp_path / "failures.json"
    summary = __import__("asyncio").run(
        ge.run(_args(tmp_path, truth_path, write_failures=str(failures))))

    # The absent criterion was asked like the boxed one, twice each.
    asked = [label for label in model.labels if label.startswith("bbox/")]
    assert sorted(asked) == sorted([f"bbox/{AMOUNT}#1", f"bbox/{CHART}#1"] * 2)
    records = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["records"]
    chart = [r for r in records if r["criterion"] == CHART]
    assert [(r["expected"], r["correct"], r["hit"]) for r in chart] == [
        ("absent", correct, None)] * 2
    assert {r["landed_on"] for r in chart} == ({None} if correct else {LOOKALIKE})
    amount = [r for r in records if r["criterion"] == AMOUNT]
    assert {r["expected"] for r in amount} == {"present"}

    overall = summary["configs"]["off"]["overall"]
    assert overall["absent"]["n"] == 2
    assert overall["absent"]["correct_rate"] == (1.0 if correct else 0.0)
    assert overall["hit_rate"] == 0.0                        # the amount: never hit
    # Failures: the amount always; the chart only when it was boxed.
    expected = [AMOUNT] if correct else [AMOUNT, CHART]
    assert json.loads(failures.read_text(encoding="utf-8")) == {"a.png": expected}
