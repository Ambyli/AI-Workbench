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
    ``verify_reason``, ``marks_lines`` kept); the summary (``marks_lines``
    per tier, ``sent_as``, ``size_mismatch`` counted and left out of every
    rate); ``--pdf-dir`` (each page's PDF by its stem, one page only,
    ``application/pdf``, a PDF read at another size than the JPG a
    ``size_mismatch``); waiting on the jobs (each job long-polled —
    ``GET /jobs/{id}?wait=`` — on its own waiter thread, the wait cut to the
    time left, a ``--poll`` sleep only after an answer that came back at
    once, as from a classifier that ignores ``wait``);
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
    afterwards, an error mid-run included. And ``--pdf-dir`` end to end: a
    digital invoice sent as its PDF is numbered from its own lines
    (``marks_lines: native``), a PDF of another size is a ``size_mismatch``,
    a page without one goes as the image.

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
import threading
import time

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
        {"refine_mode": "marks", "marks_lines": "native"},
        {"refine_mode": "grid", "marks_fallback":
         "93 native text lines lie inside the refine window, over the cap of 40"},
        {"refine_mode": "grid", "marks_lines": "ocr", "marks_fallback":
         "the model answered marks: null — none of the marked lines is the feature"},
        {"refine_mode": "marks", "marks_lines": "native"}]
    row = rba.summarise(records)["per_criterion"][USAGE]
    assert row["n"] == 5 and row["errors"] == 1 and row["error_kinds"] == {"job_failed": 1}
    assert row["marks_refined_rate"] == 0.5 and row["marks_over_cap"] == 1
    # Which lines each marks CALL numbered — a call whose answer fell back
    # still counts; an over-cap fallback made no call.
    assert row["marks_lines"] == {"native": 2, "ocr": 1}
    assert row["size_mismatch"] == 0 and row["sent_as"] == {"image": 5}
    assert row["present"]["hit_rate"] == 0.5 and row["present"]["false_accept_rate"] == 0.5
    assert row["absent"] == {"n": 2, "correct_rate": 0.5, "wrong_accept_rate": 0.5,
                             "bbox_null_rate": 0.5, "landed_on_rates": {LOOKALIKE: 0.5}}
    assert row["fail_sent"] == 2 and row["fail_reached_of_sent"] == 0.5
    rba.print_summary(rba.summarise(records), "guided")            # prints, never raises


# ---------------------------------------------------------------------------
# --pdf-dir: the page sent as its PDF, scored in the JPG's frame
# ---------------------------------------------------------------------------


def test_the_attempt_log_keeps_marks_lines():
    attempt = {"attempt": 1, "bbox_px": [120.0, 120.0, 280.0, 240.0], "accepted": True,
               "refine_mode": "marks", "marks_shown": 5, "marks_lines": "native", "marks": [1],
               "verify_reason": "never copied"}
    scored = rba.score_entry(_entry([attempt]), _result(None), USAGE, TRUTH_C)
    assert scored["attempt_log"] == [{k: v for k, v in attempt.items() if k != "verify_reason"}]
    assert "marks_lines" in rba._ATTEMPT_KEYS


def test_a_pdf_read_at_another_size_is_a_size_mismatch():
    hit = {"attempt": 1, "bbox_px": [120.0, 120.0, 280.0, 240.0], "accepted": True}
    per = {"status": "ok", "localization": {"attempts": [hit], "accepted_attempt": 1,
                                            "calls": 2}}

    def result(width, height):
        return {"page_geometry": [{"item": 0, "width": width, "height": height}],
                "assessment": {"per_criterion_scores": {USAGE: per}}}

    truth = {"c.jpg": TRUTH_C}
    pdf = rba.JobSpec("c.jpg", 1, [USAGE], job_id="j1", sent_as="pdf")
    # The PDF read at the JPG's size: scored as any job is.
    (same,) = rba.job_records(pdf, result(400, 300), truth, {}, "pdf", truth_size=(400, 300))
    assert (same["sent_as"], same["status"], same["hit"]) == ("pdf", "ok", True)
    assert "page_size" not in same
    # At another size: neither a hit nor a miss, both sizes beside it, the
    # loop's record kept.
    (other,) = rba.job_records(pdf, result(1240, 1755), truth, {}, "pdf",
                               truth_size=(400, 300))
    assert other["status"] == "size_mismatch"
    assert (other["expected"], other["hit"], other["correct"], other["iou"]) == (
        None, None, None, None)
    assert (other["page_size"], other["truth_size"]) == ([1240, 1755], [400, 300])
    assert other["attempt_log"][0]["bbox_px"] == hit["bbox_px"]
    # An image job is never checked: its truth IS its frame.
    image = rba.JobSpec("c.jpg", 1, [USAGE], job_id="j2")
    (plain,) = rba.job_records(image, result(1240, 1755), truth, {}, "x", truth_size=(400, 300))
    assert (plain["sent_as"], plain["status"]) == ("image", "ok")
    (failed,) = rba.failed_job_records(pdf, "failed", {}, "pdf")
    assert failed["sent_as"] == "pdf"


def test_size_mismatches_are_counted_and_left_out_of_every_rate():
    def record(**kw):
        base = {"criterion": AMOUNT, "status": "ok", "loop_ran": True, "accepted": True,
                "calls": 3, "fail_reached": False, "bbox_null": False, "landed_on": None,
                "hit": True, "correct": None, "iou": 0.9, "expected": "present",
                "loop_references": None, "fail_reference": {"id": None}, "sent_as": "pdf",
                "attempt_log": [{"refine_mode": "marks", "marks_lines": "native"}]}
        return {**base, **kw}

    records = [
        record(),
        record(sent_as="image", hit=False, attempt_log=[
            {"refine_mode": "marks", "marks_lines": "ocr"}]),
        record(status="size_mismatch", expected=None, hit=None, iou=None),
        record(status="size_mismatch", expected=None, hit=None, iou=None),
    ]
    summary = rba.summarise(records)
    for row in (summary["per_criterion"][AMOUNT], summary["overall"]):
        assert row["n"] == 2 and row["size_mismatch"] == 2 and row["errors"] == 0
        assert row["sent_as"] == {"image": 1, "pdf": 1}
        assert row["present"]["n"] == 2 and row["present"]["hit_rate"] == 0.5
        assert row["marks_lines"] == {"native": 1, "ocr": 1}       # mismatches not counted
    rba.print_summary(summary, "pdf")                                 # prints, never raises


def test_pdf_pages_finds_each_pages_pdf_by_its_stem(tmp_path):
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    shutil.copyfile(DOCS / "invoice_native.pdf", pdfs / "bill-1.pdf")
    shutil.copyfile(DOCS / "invoice_native.pdf", pdfs / "bill-2.PDF.pdf")
    found = rba.pdf_pages(pdfs, ["bill-1.jpg", "bill-2.PDF.jpg", "bill-3.jpg"])
    assert found == {"bill-1.jpg": pdfs / "bill-1.pdf", "bill-2.PDF.jpg": pdfs / "bill-2.PDF.pdf"}
    assert rba.pdf_pages(pdfs, []) == {}
    # One page each, or nothing is created; a directory that is not there.
    shutil.copyfile(DOCS / "invoice_two_page.pdf", pdfs / "bill-3.pdf")
    with pytest.raises(SystemExit, match="has 2 pages"):
        rba.pdf_pages(pdfs, ["bill-3.jpg"])
    (pdfs / "bill-4.pdf").write_bytes(b"not a pdf")
    with pytest.raises(SystemExit, match="does not open"):
        rba.pdf_pages(pdfs, ["bill-4.jpg"])
    with pytest.raises(SystemExit, match="not a directory"):
        rba.pdf_pages(tmp_path / "missing", ["bill-1.jpg"])


def test_a_pdf_goes_as_application_pdf():
    assert rba._content_type("bill-1.pdf") == "application/pdf"
    assert rba._content_type("bill-1.JPG") == "image/jpeg"
    assert rba.build_parser().parse_args(["--pdf-dir", "x"]).pdf_dir == "x"


# ---------------------------------------------------------------------------
# Waiting on the jobs: GET /jobs/{id}?wait=, one waiter thread per job
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, body: dict):
        self.status_code, self._body = status_code, body
        self.content = json.dumps(body).encode()

    def json(self) -> dict:
        return self._body


class JobsClient:
    """The three calls ``run_jobs`` makes, no app: ``POST /assess`` → a new
    job; ``GET /jobs/{id}`` → that job's next phase in ``phases`` (the last
    repeats), after ``hold_s`` — a long-polling service holding the request —
    or at once, as a classifier that ignores ``wait`` answers; ``DELETE`` →
    204. Records every GET's time and params; the waiter threads call it at
    once, hence the lock."""

    def __init__(self, phases: list[str], hold_s: float = 0.0):
        self.phases, self.hold_s = phases, hold_s
        self.lock = threading.Lock()
        self.gets: dict[str, list[tuple[float, dict]]] = {}
        self.posted: list[str] = []
        self.deleted: list[str] = []
        self.in_flight = self.peak = 0

    def post(self, path, data=None, files=None):
        with self.lock:
            job_id = f"j{len(self.posted) + 1}"
            self.posted.append(job_id)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        return _Response(202, {"job_id": job_id, "phase": "pending"})

    def get(self, path, params=None):
        job_id = path.rsplit("/", 1)[1]
        with self.lock:
            calls = self.gets.setdefault(job_id, [])
            calls.append((time.monotonic(), dict(params or {})))
            phase = self.phases[min(len(calls), len(self.phases)) - 1]
        if self.hold_s and phase not in rba.JOB_TERMINAL:
            time.sleep(self.hold_s)
        return _Response(200, {"job_id": job_id, "phase": phase, "result": {}})

    def delete(self, path, params=None):
        with self.lock:
            self.deleted.append(path.rsplit("/", 1)[1])
            self.in_flight -= 1
        return _Response(204, {})


def _run_jobs(tmp_path, monkeypatch, client, *, jobs=3, **flags):
    """``run_jobs`` over ``client`` with ``jobs`` one-criterion jobs; the
    records are stubbed to ``{job_id, job_seconds}`` per job."""
    page = tmp_path / "p.png"
    page.write_bytes(b"not decoded here")
    monkeypatch.setattr(rba, "job_records",
                        lambda job, result, *a, **k: [{"job_id": job.job_id, "phase": "ok"}])
    argv = ["--concurrency", "2", "--poll", "0.2", "--timeout", "60"]
    for name, value in flags.items():
        argv += [f"--{name}", str(value)]                 # the later flag wins
    args = rba.build_parser().parse_args(argv)
    state = rba.Run(jobs=[rba.JobSpec("p.png", n, [AMOUNT]) for n in range(1, jobs + 1)])
    rba.run_jobs(client, state, args, {"p.png": page}, {}, {}, {}, "test")
    return state


def test_run_jobs_long_polls_each_job_in_its_own_waiter(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rba, "FAST_ANSWER_S", 0.05)
    client = JobsClient(["processing", "completed"], hold_s=0.1)
    state = _run_jobs(tmp_path, monkeypatch, client, poll=5)
    # Two requests a job: the one held until the deadline, the one that saw
    # it end — and no --poll sleep after a held answer (5 s here).
    assert {job_id: len(calls) for job_id, calls in client.gets.items()} == {
        "j1": 2, "j2": 2, "j3": 2}
    for calls in client.gets.values():
        assert all(0 < params["wait"] <= rba.JOB_WAIT_S for _, params in calls)
        assert calls[1][0] - calls[0][0] < 2
    # --concurrency 2: never three in flight; every job scored, then deleted.
    assert client.peak == 2 and sorted(client.deleted) == ["j1", "j2", "j3"]
    assert sorted(r["job_id"] for r in state.records) == ["j1", "j2", "j3"]
    assert all(isinstance(r["job_seconds"], float) for r in state.records)
    assert capsys.readouterr().out.count(" completed") == 3


def test_run_jobs_sleeps_between_polls_of_a_classifier_that_ignores_wait(
    tmp_path, monkeypatch
):
    client = JobsClient(["pending", "processing", "completed"])
    _run_jobs(tmp_path, monkeypatch, client, jobs=2)
    for calls in client.gets.values():
        assert len(calls) == 3
        # Answered at once and not done: --poll (0.2 s) before the next ask.
        gaps = [later[0] - earlier[0] for earlier, later in zip(calls, calls[1:])]
        assert all(gap >= 0.15 for gap in gaps)


def test_run_jobs_cuts_the_wait_to_the_time_left_and_times_out(tmp_path, monkeypatch):
    client = JobsClient(["processing"])
    state = _run_jobs(tmp_path, monkeypatch, client, jobs=1, timeout=0.5, poll=0.1)
    (record,) = state.records
    assert record["status"] == "job_timed_out" and record["job_seconds"] > 0.5
    waits = [params["wait"] for _, params in client.gets["j1"]]
    assert waits[0] <= 0.5 and waits[-1] == 0.0 and waits == sorted(waits, reverse=True)
    assert client.deleted == ["j1"]


def test_a_waiters_error_reaches_the_run(tmp_path, monkeypatch):
    class Gone(JobsClient):
        def get(self, path, params=None):
            return _Response(404, {"detail": "no job 'j1'"})

    with pytest.raises(SystemExit, match="GET /jobs/j1: HTTP 404"):
        _run_jobs(tmp_path, monkeypatch, Gone(["processing"]), jobs=1)


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


class MarksModel(PageModel):
    """``PageModel`` that also answers the marks refine: mark 1."""

    async def __call__(self, prompt):
        if "numbered marks" in prompt["messages"][0]["content"]:
            self.calls.append(("marks", prompt))
            body = {"reason": "scripted", "marks": [1]}
            return {"choices": [{"message": {"content": json.dumps(body)}}]}
        return await super().__call__(prompt)


def test_end_to_end_pdf_pages_are_sent_as_pdfs_and_numbered_from_their_own_lines(
    client, tmp_path, monkeypatch, capsys
):
    """``--pdf-dir`` through the real app, with the amount due on marks:

      * ``inv.jpg`` — a JPG render of the repo's digital invoice at the
        service's 150 dpi — goes as ``inv.pdf``: read at the JPG's size, so
        it is scored, and its marks are the PDF's OWN lines (``marks_lines:
        native``, no fallback) — the plan's end-to-end case;
      * ``letter.png`` goes as ``letter.pdf`` (the invoice again), read at
        another size than the letter the truth is in: ``size_mismatch``,
        counted, scored as nothing;
      * ``plain.png`` has no PDF: sent as the image; a photo with no OCR
        engine in the session falls back to the grid, every tier named.
    """
    import cv2

    from common.documents import load_document
    from llm import boxes as llm_boxes
    from llm import client as llm_client

    invoice = (DOCS / "invoice_native.pdf").read_bytes()
    images, pdfs = tmp_path / "images", tmp_path / "pdfs"
    images.mkdir()
    pdfs.mkdir()
    render = load_document(invoice, "invoice_native.pdf").pages[0].image_bgr
    (images / "inv.jpg").write_bytes(cv2.imencode(".jpg", render)[1].tobytes())
    shutil.copyfile(DOCS / "photo_of_letter.png", images / "letter.png")
    shutil.copyfile(DOCS / "photo_of_letter.png", images / "plain.png")
    (pdfs / "inv.pdf").write_bytes(invoice)
    (pdfs / "letter.pdf").write_bytes(invoice)
    # The truth: the ask's refine window (the scripted ask boxes [150, 120,
    # 350, 180] on the page grid → window [0, 50, 500, 250]), so mark 1 —
    # the window's first line — is a hit.
    sizes = {"inv.jpg": (1240, 1755), "letter.png": (1000, 1300), "plain.png": (1000, 1300)}
    assert rba.image_size((images / "inv.jpg").read_bytes()) == sizes["inv.jpg"]
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({
        name: {"criteria": {AMOUNT: [_px([0, 50, 500, 250], size)]}}
        for name, size in sizes.items()}), encoding="utf-8")
    model = MarksModel()
    monkeypatch.setattr(llm_client, "_send", model)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", True)

    out = tmp_path / "pdf.json"
    summary = rba.main([
        "--images", str(images), "--truth", str(truth), "--repeats", "1", "--poll", "0.05",
        "--timeout", "120", "--no-references", "--pdf-dir", str(pdfs),
        "--options", 'total the customer owes={"refine":"marks"}',
        "--label", "pdf", "--out", str(out)], client=client)
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["complete"] is True and report["config"]["pdf_dir"] == str(pdfs)
    by_image = {r["image"]: r for r in report["records"]}
    assert {i: r["sent_as"] for i, r in by_image.items()} == {
        "inv.jpg": "pdf", "letter.png": "pdf", "plain.png": "image"}

    inv = by_image["inv.jpg"]
    assert (inv["status"], inv["expected"], inv["hit"]) == ("ok", "present", True)
    (attempt,) = inv["attempt_log"]
    assert (attempt["refine_mode"], attempt["marks_lines"], attempt["marks"]) == (
        "marks", "native", [1])
    assert "marks_fallback" not in attempt

    letter = by_image["letter.png"]
    assert (letter["status"], letter["hit"], letter["expected"]) == ("size_mismatch", None, None)
    assert (letter["page_size"], letter["truth_size"]) == ([1240, 1755], [1000, 1300])

    plain = by_image["plain.png"]
    assert plain["status"] == "ok" and plain["expected"] == "present"
    (attempt,) = plain["attempt_log"]
    assert attempt["refine_mode"] == "grid" and "marks_lines" not in attempt
    assert attempt["marks_fallback"].endswith(
        "window OCR: unavailable — no OCR engine, or the pass failed)")

    row = summary["per_criterion"][AMOUNT]
    assert (row["n"], row["size_mismatch"], row["sent_as"]) == (2, 1, {"image": 1, "pdf": 1})
    assert row["marks_lines"] == {"native": 1} and row["marks_refined_rate"] == 0.5
    printed = capsys.readouterr().out
    assert "pdfs    : 2 of 3 page(s) sent as their PDF" in printed
    assert "1 more left out as size_mismatch" in printed
    assert "marks calls by lines: native ×1" in printed
    for job_id in {r["job_id"] for r in report["records"]}:
        assert client.get(f"/jobs/{job_id}").status_code == 404
