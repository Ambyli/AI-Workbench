"""The benchmark report: every candidate against the labels and the rules.

    python -m bench.report --points DATA/points.jsonl --results DATA/results.jsonl \\
        [--labels DATA/labels.jsonl] [--json]

A CANDIDATE is one (model, criteria set) in ``results.jsonl`` -- two
fingerprints under one criteria-set id (a file edited without a version
bump) are two candidates. For each, the LAST result per point counts.

Two label tiers, each reported separately and named in the output:

* **gold** -- ``labels.jsonl`` from ``label.html``: a person's label, on the
  sampled points only. The promotion rule reads this tier.
* **weak** -- the points' ``weak_label`` (privacy rule / code tool called /
  the logged decision), on every point that has one. Most weak labels are
  the live rules' own decisions, so the rules' accuracy against them is
  optimistic: the weak tier shows where a candidate departs from today's
  routing, not who is right.

Per candidate and tier: accuracy and macro-F1, per-route precision / recall,
``local-private`` recall ON ITS OWN next to the privacy keywords' alone, the
confusion matrix, and calibration (accuracy per confidence bucket) -- with
the rules (``rules_route``) measured on exactly the same points. Then, once
per candidate: agreement with ``rules_route`` (overall, where a rule fired,
where the rules fell through to their default route), failure rate (status
not ``ok`` / ``incomplete``) and the no-verdict rate, p50 / p95 latency,
queue time and model seconds, tokens per model call, and the § 10 promotion
rule as PASS / FAIL with one line per condition (``metrics.promotion``). The
median gap between user messages over all points is printed once: the
latency check's bar.

A prediction of no route (an abstain, or a failure) counts against accuracy
and recall. ``--json`` prints the same numbers as one JSON document.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import metrics
from .jsonl import read_jsonl, warn
from .results import latest

POINT_FIELDS = ("point_id", "weak_label", "weak_source", "rules_route", "rules_decided_by",
                "privacy_kw_route", "gap_s")
GOLD, WEAK = "gold", "weak"
CandidateKey = Tuple[str, str, str]   # (model, criteria_set, fingerprint)


# -- loading -------------------------------------------------------------------------


def iter_points(path: str) -> Iterator[Dict[str, Any]]:
    """The fields the report needs, one point at a time (points carry whole
    conversations; only these few are kept in memory)."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                point = json.loads(line)
                yield {k: point.get(k) for k in POINT_FIELDS}


def last_results(results: Sequence[Dict[str, Any]]) -> Dict[CandidateKey, Dict[str, Dict[str, Any]]]:
    """{candidate: {point_id: its last result}}."""
    out: Dict[CandidateKey, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for (point_id, model, fingerprint), result in latest(results).items():
        out[(model, str(result.get("criteria_set")), fingerprint)][point_id] = result
    return out


def gold_labels(records: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """{point_id: label}, the last label per point; empty labels ignored."""
    out: Dict[str, str] = {}
    for record in records:
        pid, label = record.get("point_id"), record.get("label")
        if isinstance(pid, str) and isinstance(label, str) and label:
            out[pid] = label
    return out


# -- one tier ---------------------------------------------------------------------------


def tier_block(rows: Sequence[Tuple[Dict[str, Any], Dict[str, Any], str]]) -> Optional[Dict[str, Any]]:
    """Metrics over (result, point, label) rows, with the rules' and the
    privacy keywords' numbers on the same rows; None when there are none."""
    if not rows:
        return None
    pairs = [(r.get("route"), label) for r, _, label in rows]
    rules_pairs = [(p.get("rules_route"), label) for _, p, label in rows]
    kw_pairs = [(p.get("privacy_kw_route"), label) for _, p, label in rows]
    block = metrics.summary(pairs)
    block["rules_accuracy"] = metrics.accuracy(rules_pairs)
    block["rules_macro_f1"] = metrics.macro_f1(rules_pairs)
    block["privacy_kw_recall"] = metrics.recall_for(kw_pairs, metrics.PRIVATE_ROUTE)
    block["calibration"] = [
        {"bucket": b.name, "n": b.n, "accuracy": b.accuracy, "mean_confidence": b.mean_confidence}
        for b in metrics.calibration([(r.get("route"), label, r.get("confidence")) for r, _, label in rows])]
    return block


def rules_block(points: Sequence[Dict[str, Any]], labels: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The rules alone over every point with a label in ``labels``."""
    pairs = [(p.get("rules_route"), labels[p["point_id"]]) for p in points if p["point_id"] in labels]
    if not pairs:
        return None
    block = metrics.summary(pairs)
    kw = [(p.get("privacy_kw_route"), labels[p["point_id"]]) for p in points if p["point_id"] in labels]
    block["privacy_kw_recall"] = metrics.recall_for(kw, metrics.PRIVATE_ROUTE)
    return block


# -- one candidate --------------------------------------------------------------------


def percentiles(values: Sequence[Optional[float]]) -> Dict[str, Any]:
    present = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return {"n": len(present), "p50": metrics.percentile(present, 50), "p95": metrics.percentile(present, 95)}


def per_call(results: Sequence[Dict[str, Any]], field: str) -> Optional[float]:
    rows = [r for r in results if isinstance(r.get(field), int) and isinstance(r.get("calls"), int) and r["calls"] > 0]
    calls = sum(r["calls"] for r in rows)
    return sum(r[field] for r in rows) / calls if calls else None


def candidate_report(key: CandidateKey, by_point: Dict[str, Dict[str, Any]], points: Dict[str, Dict[str, Any]],
                     gold: Dict[str, str], median_gap_s: Optional[float]) -> Dict[str, Any]:
    model, criteria_set, fingerprint = key
    known = [(r, points[pid]) for pid, r in by_point.items() if pid in points]
    results = [r for r, _ in known]
    statuses = [r.get("status") for r in results]
    gold_rows = [(r, p, gold[p["point_id"]]) for r, p in known if p["point_id"] in gold]
    weak_rows = [(r, p, p["weak_label"]) for r, p in known if p.get("weak_label")]
    agree = [(r.get("route") == p.get("rules_route"), p.get("rules_decided_by") is not None) for r, p in known]
    fired = [hit for hit, rule in agree if rule]
    default = [hit for hit, rule in agree if not rule]
    latency = percentiles([r.get("latency_ms") for r in results])
    gold_block = tier_block(gold_rows)
    failure = metrics.failure_rate(statuses)
    promotion = metrics.promotion(
        gold_accuracy=gold_block["accuracy"] if gold_block else None,
        rules_gold_accuracy=gold_block["rules_accuracy"] if gold_block else None,
        gold_n=gold_block["n"] if gold_block else 0,
        private_recall=gold_block["private_recall"] if gold_block else None,
        privacy_kw_recall=gold_block["privacy_kw_recall"] if gold_block else None,
        private_support=gold_block["private_support"] if gold_block else 0,
        failure=failure, p95_latency_ms=latency["p95"], median_gap_s=median_gap_s)
    return {
        "model": model,
        "criteria_set": criteria_set,
        "criteria_fingerprint": fingerprint,
        "results": len(results),
        "unknown_points": len(by_point) - len(known),
        "status_counts": dict(sorted(Counter(str(s) for s in statuses).items())),
        "failure_rate": failure,
        "no_verdict_rate": metrics.ratio(sum(1 for r in results if r.get("route") is None), len(results)),
        "latency_ms": latency,
        "queue_ms": percentiles([r.get("queue_ms") for r in results]),
        "model_seconds": percentiles([r.get("model_seconds") for r in results]),
        "prompt_tokens_per_call": per_call(results, "prompt_tokens"),
        "completion_tokens_per_call": per_call(results, "completion_tokens"),
        "agreement_with_rules": {
            "all": metrics.ratio(sum(1 for hit, _ in agree if hit), len(agree)),
            "rule_fired": metrics.ratio(sum(fired), len(fired)),
            "rules_default": metrics.ratio(sum(default), len(default)),
            "n_rule_fired": len(fired),
            "n_rules_default": len(default),
        },
        GOLD: gold_block,
        WEAK: tier_block(weak_rows),
        "promotion": {"verdict": promotion.verdict, "passed": promotion.passed,
                      "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in promotion.checks]},
    }


def build_report(points_list: Sequence[Dict[str, Any]], results: Sequence[Dict[str, Any]],
                 labels: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    points = {p["point_id"]: p for p in points_list if p.get("point_id")}
    gold = {pid: label for pid, label in gold_labels(labels).items() if pid in points}
    weak = {pid: p["weak_label"] for pid, p in points.items() if p.get("weak_label")}
    median_gap = metrics.median([p.get("gap_s") for p in points.values()])
    grouped = last_results(results)
    candidates = [candidate_report(key, grouped[key], points, gold, median_gap) for key in sorted(grouped)]
    return {
        "points": len(points),
        "gold_labels": len(gold),
        "weak_labels": len(weak),
        "weak_label_sources": dict(sorted(Counter(p.get("weak_source") or "none" for p in points.values()).items())),
        "median_gap_s": median_gap,
        "rules": {GOLD: rules_block(list(points.values()), gold), WEAK: rules_block(list(points.values()), weak)},
        "candidates": candidates,
    }


# -- text output --------------------------------------------------------------------------


def pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def num(value: Optional[float], spec: str = ".3f") -> str:
    return "n/a" if value is None else format(value, spec)


def seconds(ms: Optional[float]) -> str:
    return "n/a" if ms is None else f"{ms / 1000.0:.1f} s"


def tier_lines(name: str, block: Optional[Dict[str, Any]], *, with_rules: bool = True) -> List[str]:
    if block is None:
        return [f"  {name}: no labelled points"]
    lines = [f"  {name} (n={block['n']}): accuracy {num(block['accuracy'])}"
             + (f" (rules {num(block['rules_accuracy'])})" if with_rules else "")
             + f" · macro-F1 {num(block['macro_f1'])}"
             + (f" (rules {num(block['rules_macro_f1'])})" if with_rules else "")]
    lines.append(f"    local-private recall {num(block['private_recall'])} on {block['private_support']} point(s)"
                 f" (privacy keywords alone {num(block['privacy_kw_recall'])})")
    for route, s in block["per_route"].items():
        lines.append(f"    {route:<16} precision {num(s['precision'])}  recall {num(s['recall'])}  "
                     f"F1 {num(s['f1'])}  labels {s['support']:>4}  predicted {s['predicted']:>4}")
    seen = list(block["confusion"]) + [p for row in block["confusion"].values() for p in row]
    columns = metrics.ordered_routes(r for r in seen if r != metrics.NONE_LABEL)
    if metrics.NONE_LABEL in seen:
        columns.append(metrics.NONE_LABEL)
    lines.append("    confusion (rows = label, columns = predicted): " + " | ".join(c.replace("local-", "")
                                                                                     for c in columns))
    for label in metrics.ordered_routes(block["confusion"]):
        row = block["confusion"][label]
        lines.append(f"      {label:<16} " + " | ".join(f"{row.get(c, 0):>4}" for c in columns))
    if block.get("calibration"):
        lines.append("    calibration: " + "; ".join(
            f"{b['bucket']} n={b['n']} acc {num(b['accuracy'], '.2f')}" for b in block["calibration"]))
    return lines


def render_text(report: Dict[str, Any]) -> str:
    gap = report["median_gap_s"]
    out = [f"Route-classifier benchmark: {report['points']} points, {report['gold_labels']} gold labels, "
           f"{report['weak_labels']} weak labels (by source: {report['weak_label_sources']})",
           "Median gap between user messages (the latency bar): " + ("n/a" if gap is None else f"{gap:.1f} s"),
           "Weak labels are mostly the live rules' own decisions: the rules' weak accuracy is optimistic.",
           ""]
    out.append("Summary")
    out.append(f"  {'candidate':<52} {'results':>7} {'fail':>6} {'gold acc':>9} {'weak acc':>9} "
               f"{'p95':>8}  verdict")
    rules = report["rules"]
    out.append(f"  {'rules (default ruleset, offline)':<52} {'':>7} {'':>6} "
               f"{num(rules[GOLD]['accuracy'] if rules[GOLD] else None):>9} "
               f"{num(rules[WEAK]['accuracy'] if rules[WEAK] else None):>9} {'':>8}")
    for c in report["candidates"]:
        name = f"{c['model']} · {c['criteria_set']} ({c['criteria_fingerprint'][:8]})"
        out.append(f"  {name:<52} {c['results']:>7} {pct(c['failure_rate']):>6} "
                   f"{num(c[GOLD]['accuracy'] if c[GOLD] else None):>9} "
                   f"{num(c[WEAK]['accuracy'] if c[WEAK] else None):>9} "
                   f"{seconds(c['latency_ms']['p95']):>8}  {c['promotion']['verdict']}")
    out.append("")
    out.append("== rules (default ruleset, offline) ==")
    out += tier_lines(GOLD, rules[GOLD], with_rules=False)
    out += tier_lines(WEAK, rules[WEAK], with_rules=False)
    for c in report["candidates"]:
        out.append("")
        out.append(f"== {c['model']} · {c['criteria_set']} (fingerprint {c['criteria_fingerprint'][:12]}) ==")
        out.append(f"  results {c['results']} {c['status_counts']}"
                   + (f" + {c['unknown_points']} for points not in the points file" if c["unknown_points"] else "")
                   + f" · failure rate {pct(c['failure_rate'])} · no verdict {pct(c['no_verdict_rate'])}")
        out.append(f"  latency p50 {seconds(c['latency_ms']['p50'])} p95 {seconds(c['latency_ms']['p95'])} · "
                   f"queue p50 {seconds(c['queue_ms']['p50'])} p95 {seconds(c['queue_ms']['p95'])} · "
                   f"model seconds p50 {num(c['model_seconds']['p50'], '.1f')} "
                   f"p95 {num(c['model_seconds']['p95'], '.1f')} · tokens per call in "
                   f"{num(c['prompt_tokens_per_call'], '.0f')} out {num(c['completion_tokens_per_call'], '.0f')}")
        a = c["agreement_with_rules"]
        out.append(f"  agreement with the rules {pct(a['all'])} · where a rule fired {pct(a['rule_fired'])} "
                   f"(n={a['n_rule_fired']}) · where they fell through to the default {pct(a['rules_default'])} "
                   f"(n={a['n_rules_default']})")
        out += tier_lines(GOLD, c[GOLD])
        out += tier_lines(WEAK, c[WEAK])
        out.append(f"  promotion rule (ROUTE_POLICY_PLAN § 10): {c['promotion']['verdict']}")
        for check in c["promotion"]["checks"]:
            mark = "PASS" if check["passed"] is True else ("FAIL" if check["passed"] is False else "N/A ")
            out.append(f"    [{mark}] {check['name']}: {check['detail']}")
    return "\n".join(out) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bench.report", description=__doc__.split("\n\n")[0])
    p.add_argument("--points", required=True, help="points.jsonl")
    p.add_argument("--results", required=True, help="results.jsonl from bench.run")
    p.add_argument("--labels", help="labels.jsonl from label.html (gold)")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    args = p.parse_args(argv)
    for path in (args.points, args.results, args.labels):
        if path and not os.path.isfile(path):
            p.error(f"{path}: no such file")
    points = list(iter_points(args.points))
    results, bad = read_jsonl(args.results, skip_bad=True)
    if bad:
        warn(f"report: {bad} unreadable line(s) in {args.results} skipped")
    labels: List[Dict[str, Any]] = []
    if args.labels:
        labels, bad_labels = read_jsonl(args.labels, skip_bad=True)
        if bad_labels:
            warn(f"report: {bad_labels} unreadable line(s) in {args.labels} skipped")
    report = build_report(points, results, labels)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
