"""bench.report: candidates against gold and weak labels and the rules.

Six fabricated points, gold labels on four, two candidates -- one that beats
the rules everywhere, one that fails -- and the report's numbers, verdicts
and text / JSON output pinned on them.
"""

from __future__ import annotations

import json

import pytest

from bench import report as report_mod
from bench.jsonl import write_jsonl

G, C, R, P = "local-general", "local-code", "local-reasoning", "local-private"


def point(pid, weak, rules, decided_by, kw, gap):
    return {"point_id": pid, "weak_label": weak, "weak_source": "decision" if weak else None,
            "rules_route": rules, "rules_decided_by": decided_by, "privacy_kw_route": kw, "gap_s": gap,
            "messages": [{"role": "user", "content": "fabricated"}], "k": 1}


POINTS = [
    point("p1", C, C, "code-text", None, 30.0),
    point("p2", G, G, None, None, 60.0),
    point("p3", P, P, "privacy", P, 90.0),
    point("p4", None, G, None, None, None),
    point("p5", R, R, "reasoning-text", None, 120.0),
    point("p6", G, G, None, None, 40.0),
]
LABELS = [
    {"point_id": "p1", "label": C}, {"point_id": "p2", "label": C},
    {"point_id": "p3", "label": P}, {"point_id": "p4", "label": P},
    {"point_id": "p4", "label": ""},            # an empty label is ignored
    {"point_id": "nope", "label": G},           # a label for an unknown point is ignored
]


def result(pid, model, route, *, status="ok", latency=2000.0, confidence=0.8):
    return {"point_id": pid, "model": model, "criteria_set": "default-llm@1/topic-llm",
            "criteria_fingerprint": "f" * 64, "route": route, "confidence": confidence,
            "scores": {}, "reason": None, "status": status, "error": None, "latency_ms": latency,
            "queue_ms": 100.0, "model_seconds": 1.5, "calls": 3, "prompt_tokens": 2700,
            "completion_tokens": 150, "input_chars": 800, "at": "2026-10-19T00:00:00+00:00"}


RESULTS = [
    result("p1", "a", G),                     # superseded by the next line
    result("p1", "a", C),
    result("p2", "a", C), result("p3", "a", P), result("p4", "a", P),
    result("p5", "a", R), result("p6", "a", G),
    result("zz", "a", G),                     # a point not in the points file
    result("p1", "b", G),
    result("p2", "b", None, status="timeout", latency=180000.0, confidence=None),
    result("p3", "b", P),
]


@pytest.fixture
def rep():
    return report_mod.build_report(POINTS, RESULTS, LABELS)


def candidate(rep, model):
    (c,) = [c for c in rep["candidates"] if c["model"] == model]
    return c


def test_overview(rep):
    assert (rep["points"], rep["gold_labels"], rep["weak_labels"]) == (6, 4, 5)
    assert rep["median_gap_s"] == 60.0
    assert rep["weak_label_sources"] == {"decision": 5, "none": 1}
    assert rep["rules"]["gold"]["accuracy"] == 0.5
    assert rep["rules"]["gold"]["privacy_kw_recall"] == 0.5
    assert rep["rules"]["weak"]["accuracy"] == 1.0
    assert [c["model"] for c in rep["candidates"]] == ["a", "b"]


def test_a_candidate_that_beats_the_rules_passes(rep):
    a = candidate(rep, "a")
    assert a["results"] == 6 and a["unknown_points"] == 1      # the last p1 line counts, once
    gold = a["gold"]
    assert (gold["n"], gold["accuracy"], gold["rules_accuracy"]) == (4, 1.0, 0.5)
    assert (gold["private_recall"], gold["privacy_kw_recall"], gold["private_support"]) == (1.0, 0.5, 2)
    assert a["weak"]["n"] == 5 and a["weak"]["accuracy"] == 0.8 and a["weak"]["rules_accuracy"] == 1.0
    assert a["agreement_with_rules"] == {"all": pytest.approx(4 / 6), "rule_fired": 1.0,
                                         "rules_default": pytest.approx(1 / 3), "n_rule_fired": 3,
                                         "n_rules_default": 3}
    assert a["failure_rate"] == 0.0 and a["no_verdict_rate"] == 0.0
    assert a["latency_ms"] == {"n": 6, "p50": 2000.0, "p95": 2000.0}
    assert a["prompt_tokens_per_call"] == 900.0 and a["completion_tokens_per_call"] == 50.0
    assert a["promotion"]["verdict"] == "PASS"
    assert a["gold"]["calibration"] == [{"bucket": "0.8-0.9", "n": 4, "accuracy": 1.0, "mean_confidence": 0.8}]


def test_a_failing_candidate_says_why(rep):
    b = candidate(rep, "b")
    assert b["status_counts"] == {"ok": 2, "timeout": 1}
    assert b["failure_rate"] == pytest.approx(1 / 3)
    gold = b["gold"]
    # Compared on ITS points (p1-p3): the rules get 2 of 3 there.
    assert (gold["n"], gold["accuracy"], gold["rules_accuracy"]) == (3, pytest.approx(1 / 3), pytest.approx(2 / 3))
    assert gold["confusion"][C] == {G: 1, "(none)": 1}
    checks = {c["name"]: c["passed"] for c in b["promotion"]["checks"]}
    assert checks == {"accuracy": False, "private_recall": True, "failure_rate": False, "latency": False}
    assert b["promotion"]["verdict"] == "FAIL"


def test_without_gold_labels_nothing_can_pass():
    rep = report_mod.build_report(POINTS, RESULTS, [])
    a = candidate(rep, "a")
    assert a["gold"] is None and a["weak"] is not None
    assert a["promotion"]["verdict"] == "FAIL"
    assert {c["name"]: c["passed"] for c in a["promotion"]["checks"]}["accuracy"] is None


def test_text_report(rep):
    text = report_mod.render_text(rep)
    assert "Median gap between user messages (the latency bar): 60.0 s" in text
    assert "== a · default-llm@1/topic-llm (fingerprint ffffffffffff) ==" in text
    assert "[PASS] accuracy: gold accuracy 1.000 vs rules 0.500 on the same 4 points" in text
    assert "[FAIL] failure_rate" in text
    assert "local-private recall 1.000 on 2 point(s) (privacy keywords alone 0.500)" in text
    assert "confusion (rows = label, columns = predicted)" in text
    # An abstain is one "(none)" column, after the routes.
    assert "confusion (rows = label, columns = predicted): general | code | private | (none)\n" in text
    assert "fabricated" not in text        # never a word of conversation text


def test_the_report_never_imports_the_http_client():
    """report.py reads files only: it takes the result schema from
    bench.results, not from run.py (which imports the classifier client)."""
    import ast
    tree = ast.parse(open(report_mod.__file__, encoding="utf-8").read())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "run" not in imported and not any("classifier_client" in (m or "") for m in imported)


def test_command_text_and_json(tmp_path, capsys):
    write_jsonl(tmp_path / "points.jsonl", POINTS)
    write_jsonl(tmp_path / "labels.jsonl", LABELS)
    with open(tmp_path / "results.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for r in RESULTS:
            fh.write(json.dumps(r) + "\n")
        fh.write('{"half a line')
    args = ["--points", str(tmp_path / "points.jsonl"), "--results", str(tmp_path / "results.jsonl"),
            "--labels", str(tmp_path / "labels.jsonl")]
    assert report_mod.main(args) == 0
    out, err = capsys.readouterr()
    assert "Summary" in out and "PASS" in out and "1 unreadable line(s)" in err
    assert report_mod.main(args + ["--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert [c["promotion"]["verdict"] for c in doc["candidates"]] == ["PASS", "FAIL"]
