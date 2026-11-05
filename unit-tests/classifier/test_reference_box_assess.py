"""``bin/reference_box_assess.py`` — the box loop measured through the API.

Two halves:

  * the pure parts, no app: the references planned per (page, criterion,
    polarity) — never from an ``absent`` pair — and their generic forms (no
    page content: a crc page id, fixed reasons); the picks — PASS and FAIL
    outside the candidate's group, never the candidate, rotated across
    repeats, FAIL independent of PASS, a FAIL only beside a PASS and only at
    three images; the locate criterion and its ``--options`` overlay (and
    ``options.reference`` dropped when a job has no references); the
    ``--key`` / ``--options`` matching; scoring one criterion of a job result
    (hit, absent, ``landed_on``, what the loop was shown, no
    ``verify_reason``); the summary;
  * END TO END, in process: the real app (its queue, stores and routes —
    Postgres, like ``test_references_assess.py``) driven by the script's own
    ``main`` through a ``TestClient``, the model scripted at
    ``llm.client._send``. Three repo fixture pages (two bills, one letter)
    with a SYNTHETIC truth: an amount box on every page, a usage box on two,
    a look-alike decoy on two, and the usage criterion ``absent`` on one.
    Pinned: the references are created (no model call) and become ready;
    every job carries one PASS and one FAIL reference per criterion, the
    keys and the options overlay; ``localization.references`` shows the
    PASS and FAIL examples reaching the loop; the scripted model, shown the
    FAIL, says "not found" on the absent page — CORRECT — and, in the
    ``--no-references`` baseline, boxes the look-alike — WRONG, landed on
    the decoy; and everything (jobs, creation jobs, references) is deleted
    afterwards, an error mid-run included.

Run with::

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:55439/postgres \\
    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_reference_box_assess.py -q -p no:cacheprovider
"""

from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import shutil
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[2] / "ai" / "classifier" / "bin"
_spec = importlib.util.spec_from_file_location("reference_box_assess", _BIN /
                                               "reference_box_assess.py")
rba = importlib.util.module_from_spec(_spec)
# Registered before it runs: its dataclasses resolve their (postponed)
# annotations through sys.modules.
sys.modules["reference_box_assess"] = rba
_spec.loader.exec_module(rba)
ge = rba.ge

DOCS = pathlib.Path(__file__).resolve().parent / "documents"
AMOUNT = "the total the customer owes on this bill"
USAGE = "the monthly usage history chart"
LOOKALIKE = "lookalike false usage history"


def _truth(**pages) -> dict:
    out = {}
    for name, entry in pages.items():
        out[name] = {"criteria": entry.get("criteria", {}), "decoys": entry.get("decoys", {}),
                     "absent": entry.get("absent", [])}
    return out


BOX = [10.0, 10.0, 50.0, 30.0]
TRUTH = _truth(**{
    "a.jpg": {"criteria": {AMOUNT: [BOX], USAGE: [BOX]}},
    "b.jpg": {"criteria": {AMOUNT: [BOX]}, "decoys": {LOOKALIKE: [BOX]}, "absent": [USAGE]},
    "c.jpg": {"criteria": {AMOUNT: [BOX], USAGE: [BOX]}, "decoys": {LOOKALIKE: [BOX]}},
    "d.jpg": {"criteria": {AMOUNT: [BOX], USAGE: [BOX]}, "decoys": {LOOKALIKE: [BOX]}},
    "e.jpg": {"criteria": {AMOUNT: [BOX]}, "decoys": {LOOKALIKE: [BOX]}},
})


# ---------------------------------------------------------------------------
# The references: what is created, and how it reads
# ---------------------------------------------------------------------------


def test_references_are_planned_per_page_criterion_and_polarity():
    specs = rba.plan_references(TRUTH, set(TRUTH), [AMOUNT, USAGE], {USAGE: LOOKALIKE})
    got = {(s.page, s.criterion, s.polarity) for s in specs}
    # a: two PASS; b: amount PASS, usage FAIL (its usage is absent — never a
    # PASS); c / d: amount PASS, usage PASS AND usage FAIL; e: amount PASS,
    # usage FAIL.
    assert got == {
        ("a.jpg", AMOUNT, "pass"), ("a.jpg", USAGE, "pass"),
        ("b.jpg", AMOUNT, "pass"), ("b.jpg", USAGE, "fail"),
        ("c.jpg", AMOUNT, "pass"), ("c.jpg", USAGE, "pass"), ("c.jpg", USAGE, "fail"),
        ("d.jpg", AMOUNT, "pass"), ("d.jpg", USAGE, "pass"), ("d.jpg", USAGE, "fail"),
        ("e.jpg", AMOUNT, "pass"), ("e.jpg", USAGE, "fail"),
    }
    assert all(s.decoy == LOOKALIKE for s in specs if s.polarity == "fail")
    # Without a FAIL map, no FAIL; a page not under --images, nothing.
    assert {s.polarity for s in rba.plan_references(TRUTH, {"a.jpg"}, [USAGE], {})} == {"pass"}


def test_a_reference_form_carries_no_page_content():
    spec = rba.RefSpec("customer-bill-0042.jpg", USAGE, "fail", [-5.0, 10.0, 900.0, 30.0],
                       decoy=LOOKALIKE)
    form = rba.reference_form(spec, "usage_history", (800, 600))
    for field in ("title", "description"):
        assert "customer" not in form[field] and "0042" not in form[field]
    assert form["title"] == f"zt527 e2e reference {rba.page_id(spec.page)} fail"
    assert json.loads(form["tags"]) == ["zt527-e2e"]
    assert json.loads(form["breakdown"]) == {USAGE: {"score": 1, "reason": rba.REASONS["fail"]}}
    # The scored copy of the locate criterion — never options.reference.
    assert json.loads(form["criteria"]) == [{
        "name": USAGE, "type": "llm", "score": True, "key": "usage_history",
        "options": {"hint": "presence", "boxes": True}}]
    # The region held inside the page.
    assert json.loads(form["regions"]) == {USAGE: [{"box": [0.0, 10.0, 800.0, 30.0]}]}
    assert form["region_units"] == "px"
    passed = rba.reference_form(rba.RefSpec("x.jpg", AMOUNT, "pass", BOX), None, (100, 100))
    assert json.loads(passed["breakdown"])[AMOUNT]["score"] == 10
    assert "key" not in json.loads(passed["criteria"])[0]


def test_the_form_validates_as_a_reference_request():
    from api.reference_schemas import ReferenceRequest

    form = rba.reference_form(rba.RefSpec("x.jpg", AMOUNT, "pass", BOX), "amount_due_figure",
                              (100, 100))
    body = {k: json.loads(v) if k in ("criteria", "breakdown", "regions", "tags") else v
            for k, v in form.items()}
    request = ReferenceRequest.model_validate({**body, "document": {
        "type": "base64", "data": "aGVsbG8="}})
    (criterion,) = request.criteria
    assert criterion.key == "amount_due_figure" and criterion.score is True
    assert request.breakdown[AMOUNT].verdict == "PASS"


def test_image_size_follows_exif_rotation():
    raw = (DOCS / "utility_bill.jpeg").read_bytes()       # stored 5712×4284, orientation 6
    assert rba.image_size(raw) == (4284, 5712)
    assert rba.image_size((DOCS / "photo_of_letter.png").read_bytes()) == (1000, 1300)


def test_error_kind_keeps_only_the_kind():
    assert rba.error_kind("no criterion of this reference has an expected answer: 'x': you "
                          "said PASS, the model scored 2") == (
        "no criterion of this reference has an expected answer")
    assert rba.error_kind(None) is None and rba.error_kind(":") == "error"


# ---------------------------------------------------------------------------
# The picks
# ---------------------------------------------------------------------------


def _ready(specs):
    for i, spec in enumerate(specs):
        spec.reference_id, spec.status = f"r{i:012x}", "ready"
    return specs


def _jobs(groups=None, *, max_images=3, repeats=5, candidates=None, ready=None, **kw):
    ready = ready if ready is not None else _ready(
        rba.plan_references(TRUTH, set(TRUTH), [AMOUNT, USAGE], {USAGE: LOOKALIKE}))
    candidates = candidates or {p: [*e["criteria"], *e["absent"]] for p, e in TRUTH.items()}
    return rba.plan_jobs(candidates, repeats, TRUTH, groups or {}, ready, {USAGE: LOOKALIKE},
                         references=True, max_images=max_images,
                         max_per_request=kw.get("max_per_request", 10)), ready


def test_picks_are_outside_the_group_never_the_candidate_and_rotate():
    groups = {"a.jpg": frozenset({"g1"}), "c.jpg": frozenset({"g1"})}
    jobs, ready = _jobs(groups)
    by_id = {s.reference_id: s for s in ready}
    for job in jobs:
        mine = groups.get(job.page, frozenset())
        for label, guide in job.guides.items():
            for rid in (guide.pass_id, guide.fail_id):
                if rid is None:
                    continue
                source = by_id[rid]
                assert source.page != job.page
                assert not mine & groups.get(source.page, frozenset())
                assert source.criterion == label
            if guide.pass_id:
                assert by_id[guide.pass_id].polarity == "pass"
            if guide.fail_id:
                assert by_id[guide.fail_id].polarity == "fail"
    # The same picks the experiment makes, so the two measure the same thing.
    job = next(j for j in jobs if j.page == "b.jpg" and j.repeat == 2)
    passes = {s.page for s in ready if s.criterion == USAGE and s.polarity == "pass"}
    assert job.guides[USAGE].pass_page == ge._truth_reference("b.jpg", USAGE, 2, TRUTH, {},
                                                              passes)
    # Rotated across repeats: b's usage PASS pages over five repeats.
    rotation = [j.guides[USAGE].pass_page for j in jobs if j.page == "b.jpg"]
    assert len(set(rotation)) > 1


def test_pass_and_fail_are_picked_independently():
    jobs, ready = _jobs()
    pairs = [(j.guides[USAGE].pass_page, j.guides[USAGE].fail_page)
             for j in jobs if j.page == "a.jpg"]
    # Both rotate, on their own salts: the FAIL page is not a function of
    # the PASS page.
    assert len({p for p, _ in pairs}) > 1 and len({f for _, f in pairs}) > 1
    fails = {s.page for s in ready if s.criterion == USAGE and s.polarity == "fail"}
    assert [f for _, f in pairs] == [
        ge._truth_fail_reference("a.jpg", LOOKALIKE, r, TRUTH, {}, fails) for r in range(1, 6)]


def test_a_fail_needs_a_pass_and_three_images():
    jobs, _ = _jobs(max_images=2)
    guide = next(j for j in jobs if j.page == "b.jpg").guides[USAGE]
    assert guide.pass_id and guide.fail_id is None
    assert "3" in guide.fail_note and "PASS only" in guide.fail_note
    # No PASS source at all (every usage box in the candidate's group): no
    # FAIL either, and both say why.
    groups = {p: frozenset({"all"}) for p in TRUTH}
    jobs, _ = _jobs(groups)
    guide = next(j for j in jobs if j.page == "b.jpg").guides[USAGE]
    assert (guide.pass_id, guide.fail_id) == (None, None)
    assert guide.note == rba.NO_PASS_REFERENCE and guide.fail_note == rba.FAIL_NEEDS_PASS


def test_the_references_list_is_deduplicated_and_capped():
    job = rba.JobSpec("a.jpg", 1, [AMOUNT, USAGE], guides={
        AMOUNT: rba.Guide(pass_id="r1"), USAGE: rba.Guide(pass_id="r1", fail_id="r2")})
    assert job.reference_ids() == ["r1", "r2"]
    with pytest.raises(SystemExit) as exc:
        _jobs(max_per_request=2)
    assert "CLASSIFIER_REFERENCE_MAX_PER_REQUEST" in str(exc.value)


def test_a_failed_reference_is_never_picked():
    specs = rba.plan_references(TRUTH, set(TRUTH), [AMOUNT, USAGE], {USAGE: LOOKALIKE})
    ready = [s for s in _ready(specs) if not (s.page == "c.jpg" and s.polarity == "pass")]
    jobs, _ = _jobs(ready=ready)
    assert all(j.guides[label].pass_page != "c.jpg" for j in jobs for label in j.guides)


# ---------------------------------------------------------------------------
# The requests
# ---------------------------------------------------------------------------


def test_the_locate_criterion_and_its_overlay():
    overlay = {"refine": "marks", "reference": {"boxes": {"ask": True}}}
    assert rba.locate_criterion(AMOUNT, "amount_due_figure", overlay) == {
        "name": AMOUNT, "type": "llm", "score": False, "key": "amount_due_figure",
        "options": {"hint": "presence", "boxes": True, "refine": "marks",
                    "reference": {"boxes": {"ask": True}}}}
    # No references on the job: options.reference would be a 400 — dropped.
    plain = rba.locate_criterion(AMOUNT, None, overlay, references=False)
    assert "reference" not in plain["options"] and "key" not in plain
    assert overlay == {"refine": "marks", "reference": {"boxes": {"ask": True}}}  # untouched
    assert rba.merged({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}, "d": 4}) == {
        "a": {"b": 1, "c": 3}, "d": 4}


def test_the_job_form():
    job = rba.JobSpec("b.jpg", 1, [AMOUNT, USAGE], guides={
        AMOUNT: rba.Guide(pass_id="r000000000001"),
        USAGE: rba.Guide(pass_id="r000000000002", fail_id="r000000000003")})
    overlays = {USAGE: {"reference": {"boxes": {"ask": True, "verify": True}}}}
    form = rba.job_form(job, {AMOUNT: "amount_due_figure", USAGE: "usage_history"}, overlays,
                        None)
    assert json.loads(form["references"]) == ["r000000000001", "r000000000002",
                                              "r000000000003"]
    criteria = json.loads(form["criteria"])
    assert [c["key"] for c in criteria] == ["amount_due_figure", "usage_history"]
    assert criteria[1]["options"]["reference"] == {"boxes": {"ask": True, "verify": True}}
    assert "model" not in form
    # The job validates as the service's request.
    from api.schemas import AssessRequest

    AssessRequest.model_validate({"document": {"type": "text", "data": "x"},
                                  "criteria": criteria,
                                  "references": json.loads(form["references"])})
    # Nothing to send: no `references` field, no options.reference.
    bare = rba.job_form(rba.JobSpec("b.jpg", 1, [USAGE]), {}, overlays, "muse-glimmer")
    assert "references" not in bare and bare["model"] == "muse-glimmer"
    assert "reference" not in json.loads(bare["criteria"])[0]["options"]


def test_keys_and_options_name_criteria_by_substring():
    names = [AMOUNT, USAGE]
    assert rba.parse_keys(["total the customer owes=amount_due_figure",
                           "usage history=usage_history"], names) == {
        AMOUNT: "amount_due_figure", USAGE: "usage_history"}
    assert rba.parse_options(['usage history={"refine":"grid"}'], names) == {
        USAGE: {"refine": "grid"}}
    for bad, flag in (("the=x", "ambiguous"), ("nope=x", "matches no criterion"),
                      ("usage history", "expected")):
        with pytest.raises(SystemExit) as exc:
            rba.parse_keys([bad], names)
        assert flag in str(exc.value)
    with pytest.raises(SystemExit, match="not JSON"):
        rba.parse_options(["usage history={refine"], names)
    with pytest.raises(SystemExit, match="JSON object"):
        rba.parse_options(['usage history=["grid"]'], names)


def test_bad_options_are_refused_before_anything_is_created():
    with pytest.raises(SystemExit) as exc:
        rba._validate_criteria([rba.locate_criterion(USAGE, None, {"refine": "zoom"})], True)
    assert "options.refine" in str(exc.value)


# ---------------------------------------------------------------------------
# Scoring one criterion of a job result
# ---------------------------------------------------------------------------


def _result(entry, width=400, height=300):
    return {"page_geometry": [{"item": 0, "width": width, "height": height}],
            "assessment": {"per_criterion_scores": {USAGE: entry}}}


def _entry(attempts, *, examples=(), steps=("ask", "verify"), status="ok"):
    entry = {
        "status": status, "reason": "the model's own words",
        "localization": {"attempts": attempts,
                         "accepted_attempt": next((a["attempt"] for a in attempts
                                                   if a.get("accepted")), None),
                         "calls": 2 * len(attempts)},
        "options_used": {"refine": "grid", "verify_partial": False, "verify_min_span": 0.0,
                         "reference": {"boxes": {"ask": True, "refine": False, "verify": True},
                                       "text": False}},
        "detail": {"reference": {"applied": True, "examples": [
            {"polarity": p} for p in examples], "calls": [{"reason": "never copied"}]}},
    }
    if examples:
        entry["localization"]["references"] = [{
            "item": 0, "applied": True, "ask_images": "composite",
            "examples": [{"reference_id": f"r{i}", "polarity": p}
                         for i, p in enumerate(examples)],
            **{s: s in steps for s in ("ask", "refine", "verify")}, "note": None}]
    return entry


TRUTH_B = {"criteria": {AMOUNT: [[10.0, 10.0, 60.0, 30.0]]},
           "decoys": {LOOKALIKE: [[100.0, 100.0, 300.0, 250.0]]}, "absent": [USAGE]}
TRUTH_C = {"criteria": {USAGE: [[100.0, 100.0, 300.0, 250.0]]}, "decoys": {}, "absent": []}


def test_an_absent_pair_is_right_when_nothing_was_accepted():
    null = {"attempt": 1, "bbox_grid": None, "valid": False, "accepted": False,
            "reject": "you answered bbox: null — you said the feature is not visible"}
    scored = rba.score_entry(_entry([null], examples=("pass", "fail")),
                             _result(None), USAGE, TRUTH_B)
    assert (scored["expected"], scored["correct"], scored["hit"]) == ("absent", True, None)
    assert scored["bbox_null"] is True and scored["accepted"] is False
    assert scored["fail_reached"] is True
    assert scored["loop_references"]["polarities"] == ["pass", "fail"]
    assert scored["scoring_reference"] == {"applied": True, "polarities": ["pass", "fail"],
                                           "calls": 1}


def test_an_absent_pair_is_wrong_when_a_box_was_accepted_and_says_where():
    boxed = {"attempt": 1, "bbox_px": [150.0, 150.0, 250.0, 200.0], "valid": True,
             "accepted": True, "verify_score": 9, "verify_reason": "a bar chart"}
    scored = rba.score_entry(_entry([boxed]), _result(None), USAGE, TRUTH_B)
    assert (scored["correct"], scored["landed_on"]) == (False, LOOKALIKE)
    assert scored["loop_references"] is None and scored["fail_reached"] is False
    # Never a reason: not the model's verify reason, not the scoring reason.
    text = json.dumps(scored)
    assert "a bar chart" not in text and "own words" not in text and "never copied" not in text
    assert scored["attempt_log"][0]["verify_score"] == 9
    assert scored["options_used"]["reference_boxes"] == {"ask": True, "refine": False,
                                                         "verify": True}


def test_a_present_pair_hits_on_its_centre():
    hit = {"attempt": 2, "bbox_px": [120.0, 120.0, 280.0, 240.0], "accepted": True}
    rejected = {"attempt": 1, "bbox_px": [0.0, 0.0, 400.0, 300.0], "accepted": False}
    scored = rba.score_entry(_entry([rejected, hit], examples=("pass",)), _result(None),
                             USAGE, TRUTH_C)
    assert (scored["expected"], scored["hit"], scored["accepted_attempt"]) == (
        "present", True, 2)
    assert scored["fail_reached"] is False and scored["iou"] > 0.5
    missing = rba.score_entry(None, _result(None), USAGE, TRUTH_C)
    assert missing["status"] == "missing" and missing["hit"] is None


def test_a_unit_error_keeps_only_its_kind():
    failed = {**_entry([]), "status": "error",
              "error": "no JSON in response: the bill says $412.07 is due"}
    scored = rba.score_entry(failed, _result(None), USAGE, TRUTH_C)
    assert scored["error_kind"] == "no JSON in response"
    assert "412" not in json.dumps(scored)
    assert rba.score_entry(_entry([]), _result(None), USAGE, TRUTH_C)["error_kind"] is None


def test_the_summary_keeps_present_and_absent_apart():
    def record(**kw):
        base = {"criterion": USAGE, "status": "ok", "loop_ran": True, "accepted": False,
                "calls": 3, "fail_reached": False, "bbox_null": False, "landed_on": None,
                "hit": None, "correct": None, "iou": None, "expected": None,
                "loop_references": {"applied": True}, "fail_reference": {"id": None}}
        return {**base, **kw}

    records = [
        record(expected="present", hit=True, accepted=True, iou=0.8),
        record(expected="present", hit=False, accepted=True, landed_on=LOOKALIKE, iou=0.0),
        record(expected="absent", correct=True, bbox_null=True, fail_reached=True,
               fail_reference={"id": "r1"}),
        record(expected="absent", correct=False, accepted=True, landed_on=LOOKALIKE,
               fail_reference={"id": "r2"}),
        record(status="job_failed", loop_ran=False, calls=0, loop_references=None),
    ]
    records[0]["attempt_log"] = [
        {"refine_mode": "marks"}, {"refine_mode": "grid", "marks_fallback":
                                   "93 OCR text lines lie inside the refine window, over the "
                                   "cap of 40"}, {"refine_mode": "grid", "marks_fallback":
                                                  "no OCR text line lies inside the window"}]
    row = rba.summarise(records)["per_criterion"][USAGE]
    assert row["n"] == 5 and row["errors"] == 1 and row["error_kinds"] == {"job_failed": 1}
    assert row["marks_refined_rate"] == 0.333 and row["marks_over_cap"] == 1
    assert row["present"]["hit_rate"] == 0.5 and row["present"]["false_accept_rate"] == 0.5
    assert row["absent"] == {"n": 2, "correct_rate": 0.5, "wrong_accept_rate": 0.5,
                             "bbox_null_rate": 0.5, "landed_on_rates": {LOOKALIKE: 0.5}}
    assert row["fail_sent"] == 2 and row["fail_reached_of_sent"] == 0.5
    rba.print_summary(rba.summarise(records), "guided")            # prints, never raises


# ---------------------------------------------------------------------------
# End to end, in process: the real app, the script's own main
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(need_postgres):
    import main
    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


# Three repo fixture pages; the truth is synthetic: boxes as FRACTIONS of
# each page, so the scripted model can answer on the 0-1000 grid. Upright
# sizes: bill 1 is stored 5712×4284 with EXIF orientation 6.
PAGES = {
    "a.jpg": ("utility_bill.jpeg", (4284, 5712)),
    "b.jpg": ("utility_bill_2.jpeg", (4032, 3024)),
    "c.png": ("photo_of_letter.png", (1000, 1300)),
}
AMOUNT_GRID = [100, 100, 400, 200]
USAGE_GRID = [500, 500, 900, 900]


def _px(grid, size):
    w, h = size
    return [grid[0] * w / 1000, grid[1] * h / 1000, grid[2] * w / 1000, grid[3] * h / 1000]


def _e2e_truth() -> dict:
    a, b, c = (PAGES[p][1] for p in ("a.jpg", "b.jpg", "c.png"))
    return {
        "a.jpg": {"criteria": {AMOUNT: [_px(AMOUNT_GRID, a)], USAGE: [_px(USAGE_GRID, a)]}},
        # b: no usage chart — a look-alike where the chart would be.
        "b.jpg": {"criteria": {AMOUNT: [_px(AMOUNT_GRID, b)]},
                  "decoys": {LOOKALIKE: [_px(USAGE_GRID, b)]}, "absent": [USAGE]},
        # c: the chart AND a look-alike (both a PASS and a FAIL source).
        "c.png": {"criteria": {AMOUNT: [_px(AMOUNT_GRID, c)], USAGE: [_px(USAGE_GRID, c)]},
                  "decoys": {LOOKALIKE: [_px([100, 600, 400, 900], c)]}},
    }


class PageModel:
    """The model, scripted per call kind and per CANDIDATE page (told apart
    by the candidate image's aspect ratio): every criterion scores 9 (so the
    loop runs), every verify 9; the ask boxes the truth spot — except the
    usage chart on b, where there is none: shown a FAIL example it answers
    ``bbox: null``, without one it boxes the look-alike."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def kind_of(prompt: dict) -> str:
        system = prompt["messages"][0]["content"]
        if "You locate features" in system:
            return "bbox"
        if "image crop" in system:
            return "verify"
        if "catalogue descriptions" in system:
            return "describe"
        return "score"

    @staticmethod
    def page_of(prompt: dict) -> str:
        import cv2
        import numpy as np

        urls = [p["image_url"]["url"] for p in prompt["messages"][1]["content"]
                if p["type"] == "image_url"]
        raw = base64.b64decode(urls[-1].split(",", 1)[1])
        h, w = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR).shape[:2]
        ratios = {page: size[0] / size[1] for page, (_, size) in PAGES.items()}
        return min(ratios, key=lambda page: abs(ratios[page] - w / h))

    async def __call__(self, prompt):
        kind = self.kind_of(prompt)
        self.calls.append((kind, prompt))
        if kind == "bbox":
            text = json.dumps(prompt)
            page = self.page_of(prompt)
            if USAGE in text and AMOUNT not in text:
                if page == "b.jpg":
                    bbox = None if "was FAIL here" in text else [550, 550, 850, 850]
                else:
                    bbox = [550, 550, 850, 850]
            else:
                bbox = [150, 120, 350, 180]
            body = {"bbox": bbox, "confidence": 90, "reason": "scripted"}
        elif kind == "verify":
            body = {"score": 9, "reason": "scripted"}
        elif kind == "describe":
            body = {"description": "should never be asked"}
        else:
            body = {"score": 9, "verdict": "PASS", "confidence": 80, "reason": "scripted"}
        return {"choices": [{"message": {"content": json.dumps(body)}}]}

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]


@pytest.fixture
def e2e(tmp_path, monkeypatch, client):
    from llm import boxes as llm_boxes
    from llm import client as llm_client

    images = tmp_path / "images"
    images.mkdir()
    for name, (source, _) in PAGES.items():
        shutil.copyfile(DOCS / source, images / name)
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps(_e2e_truth()), encoding="utf-8")
    groups = tmp_path / "groups.json"
    groups.write_text(json.dumps({"a.jpg": "p1", "b.jpg": "p2", "c.png": "p3"}),
                      encoding="utf-8")
    model = PageModel()
    monkeypatch.setattr(llm_client, "_send", model)
    # The scripted asks answer on the PAGE grid; a refine crop has its own,
    # so the refine pass is off (the verify still sees every crop).
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    base = ["--images", str(images), "--truth", str(truth), "--groups", str(groups),
            "--repeats", "2", "--poll", "0.05", "--timeout", "120",
            "--key", "total the customer owes=amount_due_figure",
            "--key", "usage history=usage_history"]
    return {"base": base, "model": model, "tmp": tmp_path}


GUIDED_OPTIONS = [
    "--options", 'total the customer owes={"refine":"marks","reference":{"boxes":'
                 '{"ask":true,"refine":true,"verify":true}}}',
    "--options", 'usage history={"reference":{"boxes":{"ask":true,"refine":true,'
                 '"verify":true}}}',
    "--fail-from-decoys", f"{LOOKALIKE}=usage history",
]


def _leftovers(client) -> int:
    return client.get("/references", params={"tag": rba.TAG}).json()["total"]


def test_end_to_end_with_references_and_options(client, e2e, capsys):
    out = e2e["tmp"] / "guided.json"
    summary = rba.main([*e2e["base"], *GUIDED_OPTIONS, "--label", "guided", "--out", str(out)],
                       client=client)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["complete"] is True

    # 1. References: 3 amount PASS, 2 usage PASS (a, c), 2 usage FAIL (b, c)
    #    — all ready, made without a single model call.
    refs = report["references"]
    assert sorted((r["page"], r["polarity"]) for r in refs if r["criterion"] == USAGE) == [
        ("a.jpg", "pass"), ("b.jpg", "fail"), ("c.png", "fail"), ("c.png", "pass")]
    assert len(refs) == 7 and {r["status"] for r in refs} == {"ready"}
    assert "describe" not in e2e["model"].kinds()

    # 2. Jobs: 3 pages × 2 repeats, both criteria each (usage absent on b).
    records = report["records"]
    assert len(records) == 12 and {r["status"] for r in records} == {"ok"}
    by_id = {r["reference_id"]: r for r in refs}
    for r in records:
        guide_pass = by_id[r["pass_reference"]["id"]]
        assert guide_pass["criterion"] == r["criterion"] and guide_pass["polarity"] == "pass"
        assert guide_pass["page"] != r["image"]                # never the candidate
        if r["fail_reference"]["id"]:
            fail = by_id[r["fail_reference"]["id"]]
            assert (fail["criterion"], fail["polarity"]) == (USAGE, "fail")
            assert fail["page"] != r["image"]
        assert set(r["references"]) >= {r["pass_reference"]["id"]}
        assert r["key"] == {AMOUNT: "amount_due_figure", USAGE: "usage_history"}[r["criterion"]]
    usage = [r for r in records if r["criterion"] == USAGE]
    assert all(r["fail_reference"]["id"] for r in usage)      # every usage job had one
    # The per-criterion options ran: marks on the amount only, every step on.
    amount = [r for r in records if r["criterion"] == AMOUNT]
    assert {r["options_used"]["refine"] for r in amount} == {"marks"}
    assert {r["options_used"]["refine"] for r in usage} == {"grid"}
    assert all(r["options_used"]["reference_boxes"] == {"ask": True, "refine": True,
                                                        "verify": True} for r in records)

    # 3. The examples reached the loop, the FAIL one included — through the
    #    stored references and the key matching, polarity fail, hint presence.
    for r in usage:
        loop = r["loop_references"]
        assert loop["applied"] and loop["ask"] and loop["verify"]
        assert loop["polarities"] == ["pass", "fail"] and r["fail_reached"] is True
        assert loop["reference_ids"] == [r["pass_reference"]["id"], r["fail_reference"]["id"]]
    for r in amount:
        assert r["loop_references"]["polarities"] == ["pass"] and not r["fail_reached"]

    # 4. Scoring: the amount hits everywhere; the usage chart hits where it is
    #    and, shown the FAIL, is "not found" on b — correct.
    assert all(r["hit"] is True for r in amount)
    assert all(r["hit"] is True for r in usage if r["image"] != "b.jpg")
    absent = [r for r in usage if r["image"] == "b.jpg"]
    assert [(r["expected"], r["correct"], r["bbox_null"]) for r in absent] == [
        ("absent", True, True)] * 2
    row = summary["per_criterion"][USAGE]
    assert row["absent"]["correct_rate"] == 1.0 and row["fail_reached_of_sent"] == 1.0
    assert summary["per_criterion"][AMOUNT]["present"]["hit_rate"] == 1.0

    # 5. Nothing left: references, their creation jobs, the assess jobs.
    assert _leftovers(client) == 0
    for rid in by_id:
        assert client.get(f"/references/{rid}").status_code == 404
    for job_id in {r["job_id"] for r in records}:
        assert client.get(f"/jobs/{job_id}").status_code == 404
    printed = capsys.readouterr().out
    assert "deleted 6 job(s), 7 reference(s), 7 creation job(s); 0 failed" in printed
    # Ids, counts and rates only: no model reason reaches the output.
    assert "scripted" not in printed and "scripted" not in out.read_text(encoding="utf-8")


def test_end_to_end_baseline_without_references(client, e2e):
    out = e2e["tmp"] / "baseline.json"
    summary = rba.main([*e2e["base"], "--options", 'usage history={"reference":{"boxes":'
                                                   '{"ask":true}},"verify_partial":true}',
                        "--no-references", "--out", str(out)], client=client)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["references"] == [] and report["config"]["label"] == "baseline"
    records = report["records"]
    assert all(r["references"] == [] and r["loop_references"] is None for r in records)
    # The overlay's reference was left out; the rest of it ran.
    usage = [r for r in records if r["criterion"] == USAGE]
    assert {r["options_used"]["verify_partial"] for r in usage} == {True}
    # Without the FAIL example the look-alike is boxed: wrong, landed on it.
    absent = [r for r in usage if r["image"] == "b.jpg"]
    assert [(r["correct"], r["landed_on"]) for r in absent] == [(False, LOOKALIKE)] * 2
    assert summary["per_criterion"][USAGE]["absent"]["wrong_accept_rate"] == 1.0
    for job_id in {r["job_id"] for r in records}:
        assert client.get(f"/jobs/{job_id}").status_code == 404


def test_an_error_mid_run_still_deletes_everything(client, e2e, monkeypatch):
    calls = {"n": 0}
    real = rba.job_records

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("scoring blew up")
        return real(*args, **kwargs)

    monkeypatch.setattr(rba, "job_records", flaky)
    out = e2e["tmp"] / "broken.json"
    with pytest.raises(RuntimeError, match="scoring blew up"):
        rba.main([*e2e["base"], *GUIDED_OPTIONS, "--out", str(out)], client=client)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["complete"] is False and len(report["records"]) == 2
    assert _leftovers(client) == 0
    for r in report["references"]:
        assert client.get(f"/references/{r['reference_id']}").status_code == 404


def test_purge_deletes_a_killed_runs_references(client, e2e):
    out = e2e["tmp"] / "kept.json"
    rba.main([*e2e["base"], *GUIDED_OPTIONS, "--subset", str(_subset(e2e["tmp"])),
              "--keep-references", "--out", str(out)], client=client)
    # Only the subset's criterion is measured, so only its references:
    # usage PASS (a, c) and usage FAIL (b, c).
    assert _leftovers(client) == 4
    assert rba.main(["--purge"], client=client) is None
    assert _leftovers(client) == 0


def test_the_reference_cap_is_checked_before_anything_is_created(client, e2e, monkeypatch):
    from api import references as references_api

    monkeypatch.setattr(references_api, "REFERENCE_MAX_COUNT", 3)
    import api.introspection as introspection

    monkeypatch.setattr(introspection, "REFERENCE_MAX_COUNT", 3)
    with pytest.raises(SystemExit) as exc:
        rba.main([*e2e["base"], *GUIDED_OPTIONS], client=client)
    assert "CLASSIFIER_REFERENCE_MAX_COUNT (3)" in str(exc.value)
    assert _leftovers(client) == 0


def _subset(tmp) -> pathlib.Path:
    path = tmp / "subset.json"
    path.write_text(json.dumps({"b.jpg": [USAGE]}), encoding="utf-8")
    return path
