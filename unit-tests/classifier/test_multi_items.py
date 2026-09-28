"""Many documents, many pages: items, units, aggregation, scope, and the cap.

A request carries a list of documents and every page of every document is
one ITEM; the unit of work is (criterion, item). What is pinned here:

  * the request — a JSON ``documents`` list, the one-``document`` shorthand,
    both at once refused, repeated multipart ``file`` parts and ``text``
    fields in form order;
  * the item cap — pages counted at submit, CLASSIFIER_MAX_ITEMS (20)
    inclusive, 21 refused with a per-document breakdown;
  * aggregation — every rule at both levels, the defaults table, ``all`` as
    ``worst``, ``sum`` text-only, skipped / errored units excluded, and
    incompleteness propagating to the criterion and the assessment;
  * per-item ``depends_on`` — a dependant skipped (no model call) on the item
    where its dependency failed and run where it passed;
  * ``options.scope: "document"`` — a phrase broken over a page break matches,
    and each hit's regions land on the page they belong to;
  * per-item weighted scores, the overall from the aggregates, the unit cap
    bound, and the artifact item map with per-item text-layer files.

No network and no live model: the model is scripted at the transport
(``llm.client._send``) or an evaluator is replaced outright.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_multi_items.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import analysis
from analysis import aggregate, scheduler
from analysis.aggregate import Member, aggregate_criterion, aggregate_level
from analysis.outcome import Outcome, skipped
from api.schemas import CriterionInput
from common.vision import Region
from llm import client as llm_client

HERE = pathlib.Path(__file__).resolve().parent
DOCS = HERE / "documents"
sys.path.insert(0, str(DOCS))
from make_fixtures import SPLIT_PHRASE  # noqa: E402

TWO_PAGE = (DOCS / "invoice_two_page.pdf").read_bytes()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    import main

    with TestClient(main.app) as c:
        yield c


def _png(value=180, width=120, height=90) -> bytes:
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), value, dtype=np.uint8))[1].tobytes()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _wait(client, job_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["phase"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def _done(client, response):
    assert response.status_code == 202, response.text
    job = _wait(client, response.json()["job_id"])
    assert job["phase"] == "completed", job
    return job


def _entries(result):
    return result["assessment"]["per_criterion_scores"]


class Model:
    """Transport fake: the score comes from the first substring that matches."""

    def __init__(self, score_for=None, default=9, fail_on=(), delay=0.0):
        self.score_for = dict(score_for or {})
        self.default = default
        self.fail_on = tuple(fail_on)
        self.delay = delay
        self.prompts: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, prompt):
        import httpx

        text = json.dumps(prompt)
        self.prompts.append(text)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if any(s in text for s in self.fail_on):
                raise httpx.ConnectError("scripted outage")
            score = next((v for k, v in self.score_for.items() if k in text), self.default)
            body = {"score": score, "verdict": "x", "confidence": 70, "reason": "scripted"}
            return {"choices": [{"message": {"content": json.dumps(body)}}]}
        finally:
            self.in_flight -= 1

    def asked(self, *fragments) -> bool:
        return any(all(f in p for f in fragments) for p in self.prompts)


@pytest.fixture
def model(monkeypatch):
    fake = Model()
    monkeypatch.setattr(llm_client, "_send", fake)
    return fake


def _txt_docs(*texts):
    return [analysis.load_document_bytes(t.encode(), f"d{i}.txt", None) for i, t in enumerate(texts)]


def _analyze(docs, criteria, **kw):
    return asyncio.run(analysis.analyze_document(docs, criteria, **kw))


# ---------------------------------------------------------------------------
# The request, and the item cap
# ---------------------------------------------------------------------------


def test_a_two_page_pdf_and_two_photos_are_four_items(client, model):
    job = _done(client, client.post("/assess", json={
        "documents": [
            {"type": "base64", "data": _b64(TWO_PAGE), "filename": "two.pdf"},
            {"type": "base64", "data": _b64(_png(60)), "filename": "a.png"},
            {"type": "base64", "data": _b64(_png(200)), "filename": "b.png"},
        ],
        "criteria": [{"name": "sharpness", "type": "cv"}],
    }))
    result = job["result"]
    assert [(d["index"], d["filename"], d["kind"], d["pages"], d["items"])
            for d in result["documents"]] == [
        (0, "two.pdf", "pdf", 2, [0, 1]), (1, "a.png", "image", 1, [2]), (2, "b.png", "image", 1, [3]),
    ]
    assert [(i["item"], i["document"], i["page"], i["filename"]) for i in result["items"]] == [
        (0, 0, 0, "two.pdf"), (1, 0, 1, "two.pdf"), (2, 1, 0, "a.png"), (3, 2, 0, "b.png"),
    ]
    assert [g["item"] for g in result["page_geometry"]] == [0, 1, 2, 3]
    assert [g["page"] for g in result["page_geometry"]] == [0, 1, 2, 3]  # global index
    entry = _entries(result)["sharpness"]
    assert [u["item"] for u in entry["items"]] == [0, 1, 2, 3]

    manifest = client.get(f"/jobs/{job['job_id']}/artifacts").json()
    assert manifest["items"] == {
        "0": {"document": 0, "page": 0, "filename": "two.pdf"},
        "1": {"document": 0, "page": 1, "filename": "two.pdf"},
        "2": {"document": 1, "page": 0, "filename": "a.png"},
        "3": {"document": 2, "page": 0, "filename": "b.png"},
    }
    names = {f["name"] for f in manifest["files"]}
    assert {"p0.base.jpg", "p1.base.jpg", "p2.base.jpg", "p3.base.jpg"} <= names
    assert manifest["options"]["byte_cap"]["items"] == 4
    assert manifest["options"]["byte_cap"]["total"] == 4 * manifest["options"]["byte_cap"]["per_item"]
    assert [i["item"] for i in result["artifacts"]["items"]] == [0, 1, 2, 3]
    assert client.get(f"/jobs/{job['job_id']}/artifacts/p3.svg").status_code == 200


def test_exactly_the_cap_is_accepted(client, model):
    from config import MAX_ITEMS

    assert MAX_ITEMS == 20  # the default this test is about
    docs = [{"type": "text", "data": f"page {i} mentions Net 30"} for i in range(20)]
    job = _done(client, client.post("/assess", json={
        "documents": docs, "criteria": [{"name": "Net 30", "type": "text"}],
    }))
    assert len(job["result"]["items"]) == 20
    entry = _entries(job["result"])["Net 30"]
    assert entry["verdict"] == "PASS" and entry["detail"]["count"] == 20  # summed


def test_one_over_the_cap_is_refused_with_a_breakdown(client):
    docs = [{"type": "base64", "data": _b64(TWO_PAGE), "filename": f"inv{i}.pdf"} for i in range(10)]
    docs.append({"type": "text", "data": "one more"})
    r = client.post("/assess", json={"documents": docs, "criteria": [{"name": "x", "type": "text"}]})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "too many items: 21 pages across 11 document(s) exceeds CLASSIFIER_MAX_ITEMS=20" in detail
    assert "#0 inv0.pdf: 2 pages" in detail and "#9 inv9.pdf: 2 pages" in detail
    assert "#10 inline.txt: 1 page" in detail


def _multipart(parts):
    """A multipart body in EXACTLY this part order (httpx's ``files=`` /
    ``data=`` would send every plain field before every file)."""
    boundary = "multi-items-test-boundary"
    body = b""
    for name, value in parts:
        body += f"--{boundary}\r\n".encode()
        if isinstance(value, tuple):
            filename, raw, ctype = value
            body += (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n").encode() + raw + b"\r\n"
        else:
            body += (f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n").encode()
    body += f"--{boundary}--\r\n".encode()
    return body, {"content-type": f"multipart/form-data; boundary={boundary}"}


def test_multipart_repeats_file_parts_and_text_fields_in_order(client, model):
    body, headers = _multipart([
        ("file", ("first.png", _png(40), "image/png")),
        ("text", "inline one"),
        ("file", ("two.pdf", TWO_PAGE, "application/pdf")),
        ("image", ("legacy.png", _png(90), "image/png")),
        ("text", "inline two"),
        ("criteria", json.dumps([{"name": "inline", "type": "text"}])),
    ])
    job = _done(client, client.post("/assess", content=body, headers=headers))
    docs = job["result"]["documents"]
    assert [d["filename"] for d in docs] == [
        "first.png", "inline.txt", "two.pdf", "legacy.png", "inline.txt"
    ]
    assert [d["pages"] for d in docs] == [1, 1, 2, 1, 1]
    entry = _entries(job["result"])["inline"]
    # items: first.png, inline one, two.pdf p1, two.pdf p2, legacy.png, inline two
    assert [u["verdict"] for u in entry["items"]] == [
        "FAIL", "PASS", "FAIL", "FAIL", "FAIL", "PASS"
    ]
    assert entry["detail"]["count"] == 2 and entry["verdict"] == "PASS"


def test_multipart_via_httpx_files_and_data(client, model):
    """The everyday client form: repeated files= tuples and a data= list."""
    job = _done(client, client.post(
        "/assess",
        files=[("file", ("a.png", _png(40), "image/png")),
               ("file", ("b.png", _png(90), "image/png"))],
        data={"text": ["one", "two"], "criteria": json.dumps([{"name": "sharpness", "type": "cv"}])},
    ))
    assert sorted(d["filename"] for d in job["result"]["documents"]) == [
        "a.png", "b.png", "inline.txt", "inline.txt"
    ]


def test_json_single_document_and_both_given(client, model):
    job = _done(client, client.post("/assess", json={
        "document": {"type": "text", "data": "hello"}, "criteria": [{"name": "hello", "type": "text"}],
    }))
    assert len(job["result"]["documents"]) == 1
    r = client.post("/assess", json={
        "document": {"type": "text", "data": "a"}, "documents": [{"type": "text", "data": "b"}],
    })
    assert r.status_code == 400 and "not both" in r.json()["detail"]
    r = client.post("/assess", json={"criteria": [{"name": "x"}]})
    assert r.status_code == 400 and "no document" in r.json()["detail"]


def test_score_false_needs_an_image_somewhere_in_the_request(client, model):
    locate = [{"name": "Tampa", "type": "text", "score": False}]
    r = client.post("/assess", json={"documents": [{"type": "text", "data": "Tampa"}] * 2,
                                     "criteria": locate})
    assert r.status_code == 400 and "no document in this request has one" in r.json()["detail"]
    job = _done(client, client.post("/assess", json={
        "documents": [{"type": "text", "data": "Tampa"},
                      {"type": "base64", "data": _b64(TWO_PAGE)}],
        "criteria": locate,
    }))
    entry = _entries(job["result"])["Tampa"]
    assert entry["score"] is None and {r["page"] for r in entry["regions"]} == {1}


# ---------------------------------------------------------------------------
# Aggregation — directly on outcomes
# ---------------------------------------------------------------------------


def _ok(score, *, regions=0, item=0, count=None, confidence=70):
    from utils import verdict_from_score

    detail = {"count": count, "best_ratio": 1.0 if count else 0.0, "searched_chars": 10,
              "text_source": "native", "snippets": []} if count is not None else None
    return Outcome(
        method="llm", score=score, verdict=verdict_from_score(score),
        confidence=confidence, reason=f"s{score}", detail=detail,
        regions=[Region(page=item, kind="box", points=[(0, 0), (5, 5)], label="x", source="cv")
                 for _ in range(regions)],
    )


def _err(message="boom"):
    return Outcome(status="error", method="llm", reason=f"Evaluation failed: {message}", error=message)


def _members(*outcomes):
    return [Member(f"item {i}", i, o) for i, o in enumerate(outcomes)]


C_LLM = CriterionInput(name="x", type="llm")


@pytest.mark.parametrize(
    "rule, scores, expected",
    [
        ("any", [3, 9, 5], 9),
        ("worst", [3, 9, 5], 3),
        ("all", [3, 9, 5], 3),     # alias of worst
        ("mean", [3, 9, 5], 6),    # 17/3 = 5.67 → 6
        ("mean", [7, 8], 8),       # 7.5 → round-half-even 8, the weighted-score rounding
        ("mean", [9, 10, 10], 10),
    ],
)
def test_each_rule(rule, scores, expected):
    out = aggregate_level(C_LLM, rule, _members(*[_ok(s) for s in scores]), "item")
    assert out.status == "ok" and out.score == expected
    assert out.verdict == ("PASS" if expected >= 7 else "MARGINAL" if expected >= 4 else "FAIL")
    assert out.complete is True


def test_mean_reports_its_arithmetic():
    out = aggregate_level(C_LLM, "mean", _members(_ok(3), _ok(9)), "item")
    assert out.detail["mean"] == 6.0 and out.detail["scores"] == {"item 0": 3, "item 1": 9}
    assert out.reason.startswith("mean of 2 item score(s) = 6.00")


def test_any_and_worst_say_which_member_they_took():
    out = aggregate_level(C_LLM, "any", _members(_ok(2), _ok(8)), "item")
    assert out.reason.startswith("any of 2 item(s) → item 1: s8")
    out = aggregate_level(C_LLM, "all", _members(_ok(2), _ok(8)), "item")
    assert out.reason.startswith("all of 2 item(s) → item 0: s2")


def test_one_member_passes_straight_through():
    only = _ok(7, regions=2)
    assert aggregate_level(C_LLM, "mean", _members(only), "item") is only


def test_geometry_is_the_union_whatever_the_rule():
    out = aggregate_level(C_LLM, "worst", _members(_ok(2, regions=1, item=0), _ok(9, regions=2, item=1)), "item")
    assert out.score == 2 and [r.page for r in out.regions] == [0, 1, 1]


def test_skipped_and_errored_members_are_excluded():
    out = aggregate_level(C_LLM, "worst", _members(_ok(8), skipped("dep"), _err()), "item")
    assert out.status == "ok" and out.score == 8
    assert out.complete is False  # an errored member → incomplete
    assert "1 item(s) errored and are excluded" in out.reason
    out = aggregate_level(C_LLM, "mean", _members(_ok(8), skipped("dep")), "item")
    assert out.score == 8 and out.complete is True


def test_every_member_skipped_is_skipped_and_every_member_failed_is_an_error():
    out = aggregate_level(C_LLM, "any", _members(skipped("a"), skipped("b")), "item")
    assert out.status == "skipped" and out.reason == "a"
    out = aggregate_level(C_LLM, "any", _members(skipped("a"), _err("down")), "item")
    assert out.status == "error" and out.error == "down" and out.complete is False


def test_an_unscored_criterion_aggregates_geometry_never_a_judgement():
    c = CriterionInput(name="x", type="llm", score=False, options={"boxes": True, "hint": "presence"})
    members = _members(
        Outcome(method="llm", reason="r0", regions=[Region(page=0, kind="box", points=[(0, 0), (1, 1)], label="x")]),
        Outcome(method="llm", reason="r1"),
    )
    out = aggregate_level(c, "any", members, "item")
    assert out.score is None and out.verdict is None and out.confidence is None
    assert len(out.regions) == 1 and out.reason.startswith("Located on 1 of 2 item(s)")


def test_sum_adds_hit_counts_and_rescores():
    c = CriterionInput(name="Net 30", type="text", options={"min_count": 3})
    members = _members(_ok(1, count=0), _ok(10, count=2), _ok(10, count=1))
    out = aggregate_level(c, "sum", members, "item")
    assert out.detail["count"] == 3 and out.score == 10 and out.verdict == "PASS"
    assert out.detail["counts"] == {"item 0": 0, "item 1": 2, "item 2": 1}
    out = aggregate_level(c, "sum", members[:2], "item")
    assert out.detail["count"] == 2 and out.score == 1 and out.verdict == "FAIL"


def _groups(*sizes):
    """SimpleNamespace documents of the given page counts, items numbered globally."""
    groups, n = [], 0
    for index, size in enumerate(sizes):
        groups.append(SimpleNamespace(index=index, items=[SimpleNamespace(item=n + k) for k in range(size)]))
        n += size
    return groups


def _units(c, outcomes):
    return scheduler.CriterionUnits(criterion=c, scope=c.scope(), outcomes=dict(enumerate(outcomes)))


def test_two_levels_pages_then_documents():
    """doc 0 = items 0,1 · doc 1 = item 2. pages any, documents worst."""
    c = CriterionInput(name="x", type="llm", options={"aggregate": {"pages": "any", "documents": "worst"}})
    agg = aggregate_criterion(c, _units(c, [_ok(2), _ok(9), _ok(6)]), _groups(2, 1))
    assert [o.score for o in agg.per_document] == [9, 6]
    assert agg.final.score == 6 and agg.final.verdict == "MARGINAL"
    assert agg.used == {"pages": "any", "documents": "worst"}
    # the other way round
    c = CriterionInput(name="x", type="llm", options={"aggregate": {"pages": "worst", "documents": "any"}})
    agg = aggregate_criterion(c, _units(c, [_ok(2), _ok(9), _ok(6)]), _groups(2, 1))
    assert [o.score for o in agg.per_document] == [2, 6] and agg.final.score == 6
    c = CriterionInput(name="x", type="llm", options={"aggregate": "mean"})
    agg = aggregate_criterion(c, _units(c, [_ok(2), _ok(9), _ok(6)]), _groups(2, 1))
    assert [o.score for o in agg.per_document] == [6, 6] and agg.final.score == 6  # 5.5 → 6


def test_presence_defaults_mean_some_page_of_every_document():
    c = CriterionInput(name="x", type="llm", options={"hint": "presence"})
    groups = _groups(2, 2)
    agg = aggregate_criterion(c, _units(c, [_ok(1), _ok(9), _ok(9), _ok(1)]), groups)
    assert agg.final.verdict == "PASS"  # each document has it on one page
    agg = aggregate_criterion(c, _units(c, [_ok(1), _ok(1), _ok(9), _ok(1)]), groups)
    assert agg.final.verdict == "FAIL"  # document 0 has it nowhere


def test_quality_defaults_mean_every_page():
    c = CriterionInput(name="x", type="llm", options={"hint": "quality"})
    agg = aggregate_criterion(c, _units(c, [_ok(9), _ok(9), _ok(3)]), _groups(3))
    assert agg.final.score == 3 and agg.used == {"pages": "worst", "documents": "worst"}


def test_text_defaults_sum_at_both_levels():
    c = CriterionInput(name="Net 30", type="text", options={"min_count": 2})
    # Each page unit scored its own count against min_count 2: one hit is a FAIL.
    agg = aggregate_criterion(c, _units(c, [_ok(1, count=1), _ok(1, count=0), _ok(1, count=1)]), _groups(2, 1))
    assert [o.detail["count"] for o in agg.per_document] == [1, 1]
    assert [o.verdict for o in agg.per_document] == ["FAIL", "FAIL"]  # 1 < min_count on each
    assert agg.final.detail["count"] == 2 and agg.final.verdict == "PASS"  # 2 across the request


def test_an_error_on_one_page_makes_the_criterion_incomplete_at_every_level():
    c = CriterionInput(name="x", type="llm", options={"aggregate": "any"})
    agg = aggregate_criterion(c, _units(c, [_ok(9), _err(), _ok(4)]), _groups(2, 1))
    assert agg.per_document[0].complete is False and agg.per_document[1].complete is True
    assert agg.final.status == "ok" and agg.final.score == 9 and agg.final.complete is False


def test_document_scope_has_no_pages_level():
    c = CriterionInput(name="x", type="text", options={"scope": "document", "aggregate": "any"})
    units = scheduler.CriterionUnits(criterion=c, scope="document",
                                     outcomes={0: _ok(1, count=0), 1: _ok(10, count=1)})
    agg = aggregate_criterion(c, units, _groups(3, 1))
    assert agg.used["pages"] is None and agg.used["documents"] == "any"
    assert "no pages level" in agg.used["note"]
    assert agg.final.score == 10


# ---------------------------------------------------------------------------
# Through the pipeline: gating, scores, the cap, errors
# ---------------------------------------------------------------------------


def test_depends_on_is_gated_per_item(monkeypatch):
    fake = Model(score_for={"alpha": 9, "beta": 2})
    monkeypatch.setattr(llm_client, "_send", fake)
    result = _analyze(_txt_docs("page alpha", "page beta"), [
        CriterionInput(name="gate", type="llm"),
        CriterionInput(name="after", type="llm", depends_on="gate"),
    ])
    after = _entries(result)["after"]
    assert [u["status"] for u in after["items"]] == ["ok", "skipped"]
    assert "dependency 'gate' did not pass (verdict: FAIL)" in after["items"][1]["reason"]
    assert fake.asked("CRITERION: after", "alpha")
    assert not fake.asked("CRITERION: after", "beta")  # no model call on the skipped item
    assert len(fake.prompts) == 3
    # The criterion's answer comes from the item that ran.
    assert after["status"] == "ok" and after["score"] == 9


def test_a_criterion_skipped_on_every_item_is_skipped(monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Model(default=2))
    result = _analyze(_txt_docs("a", "b"), [
        CriterionInput(name="gate", type="llm"),
        CriterionInput(name="after", type="llm", depends_on="gate"),
    ])
    after = _entries(result)["after"]
    assert after["status"] == "skipped" and after["score"] is None
    assert result["assessment"]["weighted_score_breakdown"]["excluded"] == {"after": "skipped"}


def test_per_item_scores_and_the_overall_from_the_aggregates(monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Model(score_for={"alpha": 9, "beta": 2}))
    worst = _analyze(_txt_docs("alpha", "beta"), [CriterionInput(name="q", type="llm")])
    assert [(i["overall_score"], i["overall_verdict"]) for i in worst["items"]] == [
        (9, "PASS"), (2, "FAIL")
    ]
    assert worst["assessment"]["overall_score"] == 2 and worst["verdict"] == "FAIL"

    best = _analyze(_txt_docs("alpha", "beta"),
                    [CriterionInput(name="q", type="llm", options={"aggregate": "any"})])
    assert [i["overall_score"] for i in best["items"]] == [9, 2]  # items are unaffected
    assert best["assessment"]["overall_score"] == 9 and best["verdict"] == "PASS"
    assert _entries(best)["q"]["aggregate_used"] == {"pages": "any", "documents": "any"}


def test_one_failed_unit_fails_alone_and_the_job_is_incomplete(monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Model(fail_on=("beta",)))
    result = _analyze(_txt_docs("alpha", "beta"), [CriterionInput(name="q", type="llm")])
    entry = _entries(result)["q"]
    assert [u["status"] for u in entry["items"]] == ["ok", "error"]
    assert "scripted outage" in entry["items"][1]["error"]
    assert entry["status"] == "ok" and entry["complete"] is False and entry["score"] == 9
    assessment = result["assessment"]
    assert assessment["complete"] is False and assessment["overall_score"] is None
    assert assessment["weighted_score_breakdown"]["partial"] is True
    assert [i["complete"] for i in result["items"]] == [True, False]


def test_the_unit_cap_bounds_a_job(monkeypatch):
    fake = Model(delay=0.05)
    monkeypatch.setattr(llm_client, "_send", fake)
    monkeypatch.setattr(llm_client.LLM_CALLS, "limit", 16)
    criteria = [CriterionInput(name=f"c{i}", type="llm") for i in range(2)]
    monkeypatch.setattr(scheduler, "MAX_UNITS_PER_JOB", 2)
    _analyze(_txt_docs("a", "b", "c"), criteria)
    assert len(fake.prompts) == 6 and fake.peak == 2  # 2 criteria × 3 items, 2 at once

    fake = Model(delay=0.05)
    monkeypatch.setattr(llm_client, "_send", fake)
    monkeypatch.setattr(scheduler, "MAX_UNITS_PER_JOB", 6)
    _analyze(_txt_docs("a", "b", "c"), criteria)
    assert fake.peak == 6  # units of ONE criterion run in parallel too


# ---------------------------------------------------------------------------
# options.scope: "document"
# ---------------------------------------------------------------------------


def test_a_phrase_broken_over_a_page_break_matches_with_document_scope(client):
    criteria = [
        {"name": "split", "type": "text", "options": {"pattern": SPLIT_PHRASE, "scope": "document"}},
        {"name": "split per page", "type": "text", "options": {"pattern": SPLIT_PHRASE}},
    ]
    job = _done(client, client.post(
        "/assess", files={"file": ("two.pdf", TWO_PAGE, "application/pdf")},
        data={"criteria": json.dumps(criteria)},
    ))
    result, job_id = job["result"], job["job_id"]
    entries = _entries(result)

    doc = entries["split"]
    assert doc["verdict"] == "PASS" and doc["detail"]["count"] == 1
    assert doc["detail"]["items_with_hits"] == [0, 1]
    assert doc["aggregate_used"]["pages"] is None
    # Regions on BOTH pages: the half of the phrase each one holds.
    assert sorted({r["page"] for r in doc["regions"]}) == [0, 1]
    assert all(r["source"] == "pdf-text" for r in doc["regions"])
    (unit,) = doc["items"]
    assert unit["item"] is None and unit["items"] == [0, 1] and unit["document"] == 0
    assert unit["text_layer"]["url"] == f"/jobs/{job_id}/artifacts/text.d0.auto.json"
    assert [e["item"] for e in doc["artifacts"]["items"]] == [0, 1]

    per_page = entries["split per page"]
    assert [u["verdict"] for u in per_page["items"]] == ["FAIL", "FAIL"]
    assert per_page["verdict"] == "FAIL"
    # Per-item scores cover the page-scope criterion only.
    assert [i["overall_verdict"] for i in result["items"]] == ["FAIL", "FAIL"]

    joined = client.get(f"/jobs/{job_id}/artifacts/text.d0.auto.json").json()
    assert joined["scope"] == "document" and joined["separator"] == " "
    assert SPLIT_PHRASE in joined["text"]
    assert [(s["item"], s["file"]) for s in joined["segments"]] == [
        (0, "text.p0.auto.json"), (1, "text.p1.auto.json")
    ]
    names = {f["name"] for f in client.get(f"/jobs/{job_id}/artifacts").json()["files"]}
    assert {"text.p0.auto.json", "text.p1.auto.json", "text.d0.auto.json"} <= names
    # Each unit links its own page's layer.
    assert [u["text_layer"]["url"] for u in per_page["items"]] == [
        f"/jobs/{job_id}/artifacts/text.p0.auto.json", f"/jobs/{job_id}/artifacts/text.p1.auto.json"
    ]
    assert client.get(f"/jobs/{job_id}/artifacts/text.d0.auto.txt").text == joined["text"]
    layers = result["documents"][0]["document_info"]["ocr"]
    assert [lay["item"] for lay in layers["layers"]] == [0, 1]
    assert layers["document_layers"][0]["file"] == "text.d0.auto.json"


def test_document_scope_on_ocr_text_maps_hits_to_line_polygons(monkeypatch):
    """Two photographed pages; the phrase is split over their OCR lines."""
    from analysis import ocr
    from common.documents import OCRResult

    class TwoPageOCR:
        def __init__(self):
            self.n = 0

        def recognize(self, image_bgr):
            self.n += 1
            line = "Please remit the balance to" if image_bgr[0, 0, 0] == 40 else "the Acme Roofing billing office."
            return OCRResult(text=line, confidence=0.9, lines=[
                {"text": line, "confidence": 0.9, "box": [[5, 5], [80, 5], [80, 20], [5, 20]]}
            ])

    from common.documents import Document, Page

    monkeypatch.setattr(ocr, "_ocr_engine", TwoPageOCR())
    criterion = CriterionInput(name="s", type="text",
                               options={"pattern": SPLIT_PHRASE, "scope": "document"})

    # One scanned two-page document: the phrase spans its OCR'd pages.
    def page(index, value):
        return Page(index=index, image_bgr=np.full((90, 120, 3), value, dtype=np.uint8),
                    width=120, height=90)

    scan = Document(kind="pdf", filename="scan.pdf", pages=[page(0, 40), page(1, 90)])
    entry = _entries(_analyze(scan, [criterion]))["s"]
    assert entry["verdict"] == "PASS"
    assert [(r["page"], r["source"]) for r in entry["regions"]] == [(0, "ocr"), (1, "ocr")]
    assert [r["points"] for r in entry["regions"]] == [
        [[5.0, 5.0], [80.0, 5.0], [80.0, 20.0], [5.0, 20.0]]
    ] * 2

    # Two photos are two DOCUMENTS; a document-scope search is per document,
    # so the same halves must NOT match across them.
    docs = [analysis.load_document_bytes(_png(v), f"{v}.png", None) for v in (40, 90)]
    result = _analyze(docs, [criterion])
    assert _entries(result)["s"]["verdict"] == "FAIL"
    assert [u["verdict"] for u in _entries(result)["s"]["items"]] == ["FAIL", "FAIL"]


def test_mixed_scope_dependencies(client):
    """page-scope A → document-scope B → page-scope C, on one two-page PDF.

    "Net 30" is only on page 2, so A fails on item 0 and passes on item 1; its
    pages aggregate (sum) passes, so B runs; C is gated on B's document
    result on EVERY page.
    """
    criteria = [
        {"name": "Net 30", "type": "text"},
        {"name": "B", "type": "text", "depends_on": "Net 30",
         "options": {"pattern": SPLIT_PHRASE, "scope": "document"}},
        {"name": "C", "type": "text", "depends_on": "B", "options": {"pattern": "Tampa"}},
        {"name": "D", "type": "text", "depends_on": "Net 30", "options": {"pattern": "Tampa"}},
    ]
    job = _done(client, client.post(
        "/assess", files={"file": ("two.pdf", TWO_PAGE, "application/pdf")},
        data={"criteria": json.dumps(criteria)},
    ))
    e = _entries(job["result"])
    assert [u["verdict"] for u in e["Net 30"]["items"]] == ["FAIL", "PASS"]
    assert e["B"]["items"][0]["status"] == "ok" and e["B"]["verdict"] == "PASS"
    assert [u["status"] for u in e["C"]["items"]] == ["ok", "ok"]
    # D is page-scope on a page-scope dependency: gated item by item.
    assert [u["status"] for u in e["D"]["items"]] == ["skipped", "ok"]


def test_document_kinds_reports_items_and_units(client):
    body = client.get("/document-kinds").json()
    limits = body["limits"]
    assert limits["max_items"] == 20 and limits["max_units_per_job"] == 2
    assert "max_pages" not in limits and "max_criteria_per_job" not in limits
    pdf = next(k for k in body["kinds"] if k["kind"] == "pdf")
    assert pdf["pages"].startswith("any — every page is one item")
    assert body["regions"]["artifact_max_bytes_per_item"] > 0
