"""Measure which confidence signal predicts a right answer — then calibrate it.

An ``llm`` result carries ``detail.confidence`` (``llm/confidence.py``): the
model's stated 0-100, and — for an entry whose ``models.yaml``
``confidence.method`` is ``logprobs`` or ``sampling`` — the probability of its
own verdict, the score distribution's certainty, the samples' agreement.
Which of them is worth trusting is an empirical, per-model question, so this
script answers it on real documents before anything is flipped:

  1. **Fast pass.** Every (criterion, page) unit runs through
     ``analysis.pipeline.analyze_document`` in-process (no job id, nothing
     written) on the model exactly as configured — what production runs.
  2. **Label.** The same unit re-runs on a DELIBERATE copy of the entry,
     ``<id>@deliberate``: ``dataclasses.replace`` of the spec with the
     reasoning value ``--deliberate-reasoning`` (xhigh; only for an entry
     whose reasoning is a ``chat_template_kwargs`` variable — any other entry
     differs only in budget and time), ``max_tokens.score``
     ``--deliberate-max-tokens``, ``timeout_s`` ``--deliberate-timeout``, its
     own limit of ``--concurrency`` and no confidence measurement. Its
     verdict (the majority of ``--samples K`` runs) is the label — a
     label-free check: "would the model, thinking hard, agree with its fast
     self?". Human labels (``--labels``) override it wherever present.
  3. **Score the signals.** For ``stated / 100``, ``verdict_probability``,
     ``certainty`` (``1 - entropy / log2(10)``) and, for sampling,
     ``agreement``: the rate the fast verdict matched the label, AUROC for
     "fast verdict == label", ECE (10 bins), Brier, and a 10-bin histogram.
     Plus, per model id, the seconds per call and the response bytes (the
     size / latency check for logprobs).
  4. **Fit.** Isotonic regression (``llm.confidence.fit_isotonic``) of
     correctness on the best signal by AUROC, printed as a ready-to-paste
     ``confidence.calibration`` block. Paste it only when that signal beats
     ``stated`` on AUROC; flipping ``headline: calibrated`` is a separate,
     later edit.

**This has to run where the model is** — in the classifier container
(``bin/`` ships in the image), off-hours: its calls come ON TOP of
production's slots (the harness is its own process with its own limits;
``--concurrency`` bounds both passes)::

    docker exec classifier uv run python bin/confidence_calibration.py \\
        --docs /data/calibration/docs --criteria /data/calibration/criteria.json \\
        --model muse-glimmer --concurrency 1 --out /data/calibration.json

``--criteria`` is grounding_experiment's shape: a JSON object mapping a file
name to its criteria, or a bare list applied to every file. A criterion is a
name (an ``llm`` criterion, hint ``auto``) or ``{"name", "options"}`` — llm
options (``hint``, ``ocr``, …); ``boxes`` is always off::

    {"bill-01.pdf": ["shows an account number", {"name": "is signed",
                                                  "options": {"hint": "presence"}}]}

``--labels labels.json`` = ``{file: {criterion: "PASS" | "MARGINAL" | "FAIL"}}``
(a verdict for every page of that file) or ``{file: {criterion: {"<item>":
verdict}}}`` per page. ``--export-labels out.csv`` writes file, item, page,
criterion, the fast verdict / score / reason, the deliberate label and an
empty ``expected`` column for a person to fill in (then turn it into
``--labels``).

Re-run after a model, prompt or reasoning change: a calibration describes one
model answering one prompt at one reasoning depth.

Process flow position: none — a standalone operator tool. It runs the same
``analyze_document`` the worker runs, so what it measures is what production
does, not a reimplementation of it.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import datetime as dt
import json
import pathlib
import statistics
import sys
import time
from collections import Counter
from typing import Any, Optional

# The classifier's modules import each other by bare name (the container's
# WORKDIR). Running this from `bin/` needs the parent on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import analysis  # noqa: E402
from api.schemas import CriterionInput  # noqa: E402
from llm import client as llm_client  # noqa: E402
from llm import confidence as llm_confidence  # noqa: E402
from llm import usage as llm_usage  # noqa: E402
from llm.models import ConfidenceConfig, ModelSpec, Registry, get_registry, use_registry  # noqa: E402

VERDICTS = ("PASS", "MARGINAL", "FAIL")
SIGNALS = ("stated", "verdict_probability", "certainty", "agreement")
BINS = 10


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _criteria(path: pathlib.Path, names: list[str]) -> dict[str, list[dict]]:
    """file → [{name, options}] from ``--criteria``."""
    data = json.loads(path.read_text(encoding="utf-8"))

    def norm(items) -> list[dict]:
        out = []
        for item in items or []:
            if isinstance(item, str):
                out.append({"name": item, "options": {}})
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                out.append({"name": item["name"], "options": dict(item.get("options") or {})})
            else:
                raise SystemExit(f"{path}: a criterion is a name or {{name, options}}, got {item!r}")
        return out

    if isinstance(data, list):
        return {name: norm(data) for name in names}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected {{file: [criterion, ...]}} or a bare list")
    return {name: norm(data.get(name)) for name in names}


def _labels(path: Optional[str]) -> dict:
    if not path:
        return {}
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected {{file: {{criterion: verdict}}}}")
    return data


def _human_label(labels: dict, file: str, criterion: str, item: int) -> Optional[str]:
    entry = (labels.get(file) or {}).get(criterion)
    if isinstance(entry, dict):
        entry = entry.get(str(item))
    if isinstance(entry, str) and entry.upper() in VERDICTS:
        return entry.upper()
    return None


def _criterion_inputs(items: list[dict]) -> list[CriterionInput]:
    return [
        CriterionInput.model_validate({
            "name": c["name"], "type": "llm", "options": {**c["options"], "boxes": False},
        })
        for c in items
    ]


# ---------------------------------------------------------------------------
# The two specs: production's, and the deliberate copy
# ---------------------------------------------------------------------------


def _specs(args) -> tuple[ModelSpec, ModelSpec]:
    """``(fast, deliberate)`` registered in a COPY of the registry."""
    registry = get_registry()
    try:
        spec = registry.check_selectable(args.model) if args.model else registry.default
    except ValueError as exc:
        raise SystemExit(f"--model: {exc}")
    # The fast pass: the entry as configured, but with this process's own
    # limit (production's slots are the container's, not ours).
    fast = dataclasses.replace(
        spec, max_concurrent=max(args.concurrency, spec.confidence.slots),
    )
    reasoning = dict(spec.reasoning)
    if reasoning.get("style") == "chat_template_kwargs":
        reasoning["value"] = args.deliberate_reasoning
    else:
        print(f"note: {spec.id} has no reasoning variable (style "
              f"{reasoning.get('style')!r}); the deliberate pass differs only in budget "
              "and time", file=sys.stderr)
    deliberate = dataclasses.replace(
        spec,
        id=f"{spec.id}@deliberate",
        reasoning=reasoning,
        max_tokens={**spec.max_tokens, "score": args.deliberate_max_tokens},
        timeout_s=float(args.deliberate_timeout),
        max_concurrent=args.concurrency,
        confidence=ConfidenceConfig(),
    )
    models = {**registry.models, fast.id: fast, deliberate.id: deliberate}
    use_registry(Registry(models, registry.default_id, path=registry.path))
    return fast, deliberate


class _NoStore:
    """llm_calls rows are production's accounting; the harness writes none."""

    async def record(self, row: dict) -> bool:
        return True


class _Meter:
    """Wraps ``llm.client._send``: seconds and response bytes per model id."""

    def __init__(self, send):
        self._send = send
        self.calls: dict[str, list[tuple[float, int, bool]]] = {}

    async def __call__(self, prompt: dict) -> dict:
        spec = llm_client._target.get()
        t0 = time.monotonic()
        data = await self._send(prompt)
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        has_lp = any(isinstance(c, dict) and c.get("logprobs")
                     for c in (data.get("choices") or []))
        self.calls.setdefault(spec.id if spec else "?", []).append(
            (time.monotonic() - t0, size, has_lp))
        return data

    def summary(self) -> dict[str, dict]:
        out = {}
        for model_id, rows in self.calls.items():
            secs = [r[0] for r in rows]
            sizes = [r[1] for r in rows]
            out[model_id] = {
                "calls": len(rows),
                "seconds_mean": round(statistics.fmean(secs), 2),
                "seconds_p50": round(statistics.median(secs), 2),
                "response_bytes_mean": int(statistics.fmean(sizes)),
                "with_logprobs": sum(1 for r in rows if r[2]),
            }
        return out


# ---------------------------------------------------------------------------
# Running one file
# ---------------------------------------------------------------------------


def _units(result: dict) -> dict[tuple[str, int], dict]:
    """(criterion, item) → the item entry, for every ok unit."""
    out = {}
    for name, entry in result["assessment"]["per_criterion_scores"].items():
        for unit in entry.get("items") or []:
            if unit.get("status") == "ok" and unit.get("item") is not None:
                out[(name, unit["item"])] = unit
    return out


def _majority(verdicts: list[str]) -> Optional[str]:
    if not verdicts:
        return None
    counts = Counter(verdicts)
    best = max(counts.values())
    return next(v for v in verdicts if counts[v] == best)  # ties: the first run's


async def _run_file(path: pathlib.Path, criteria: list[CriterionInput], fast: ModelSpec,
                    deliberate: ModelSpec, args, labels: dict) -> list[dict]:
    raw = path.read_bytes()
    doc = analysis.load_document_bytes(raw, path.name, None, keep_source=True)
    t0 = time.monotonic()
    fast_result = await analysis.analyze_document(doc, criteria, default_model=fast.id)
    fast_seconds = time.monotonic() - t0
    slow_units: list[dict] = []
    for _ in range(args.samples):
        slow = await analysis.analyze_document(doc, criteria, default_model=deliberate.id)
        slow_units.append(_units(slow))

    records = []
    for (name, item), unit in _units(fast_result).items():
        block = (unit.get("detail") or {}).get("confidence") or {}
        slow_verdicts = [u[(name, item)]["verdict"] for u in slow_units if (name, item) in u]
        deliberate_label = _majority(slow_verdicts)
        human = _human_label(labels, path.name, name, item)
        label = human or deliberate_label
        samples = block.get("samples") or {}
        records.append({
            "file": path.name,
            "item": item,
            "page": unit.get("page"),
            "criterion": name,
            "fast": {"score": unit["score"], "verdict": unit["verdict"],
                     "confidence": unit["confidence"], "reason": unit.get("reason")},
            "deliberate_verdicts": slow_verdicts,
            "deliberate_label": deliberate_label,
            "human_label": human,
            "label": label,
            "label_source": "human" if human else ("deliberate" if deliberate_label else None),
            "correct": (unit["verdict"] == label) if label else None,
            "source": block.get("source"),
            "signals": {
                "stated": (block["stated"] / 100.0) if block.get("stated") is not None else None,
                "verdict_probability": block.get("verdict_probability"),
                "certainty": block.get("certainty"),
                "agreement": samples.get("agreement"),
            },
            "coverage": block.get("coverage"),
        })
    mark = sum(1 for r in records if r["correct"])
    print(f"{path.name}: {len(records)} unit(s), fast {fast_seconds:.1f}s, "
          f"{mark}/{sum(1 for r in records if r['correct'] is not None)} agree with the label")
    return records


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _auroc(xs: list[float], ys: list[bool]) -> Optional[float]:
    """P(x of a right answer > x of a wrong one), ties counted half."""
    pos = sum(1 for y in ys if y)
    neg = len(ys) - pos
    if not pos or not neg:
        return None
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_sum = sum(r for r, y in zip(ranks, ys) if y)
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def _bin(x: float) -> int:
    return min(BINS - 1, max(0, int(x * BINS)))


def _metrics(xs: list[float], ys: list[bool]) -> dict[str, Any]:
    n = len(xs)
    if not n:
        return {"n": 0}
    correct = [1.0 if y else 0.0 for y in ys]
    bins: dict[int, list[int]] = {}
    for i, x in enumerate(xs):
        bins.setdefault(_bin(x), []).append(i)
    ece = sum(
        len(idx) / n * abs(statistics.fmean(correct[i] for i in idx)
                           - statistics.fmean(xs[i] for i in idx))
        for idx in bins.values()
    )
    auroc = _auroc(xs, ys)
    return {
        "n": n,
        "accuracy": round(statistics.fmean(correct), 4),
        "auroc": round(auroc, 4) if auroc is not None else None,
        "ece": round(ece, 4),
        "brier": round(statistics.fmean((x - c) ** 2 for x, c in zip(xs, correct)), 4),
        "mean_signal": round(statistics.fmean(xs), 4),
        "histogram": [len(bins.get(b, [])) for b in range(BINS)],
    }


def _summarise(records: list[dict], fast: ModelSpec, meter: _Meter, elapsed: float) -> dict:
    labelled = [r for r in records if r["correct"] is not None]
    per_signal = {}
    for signal in SIGNALS:
        rows = [r for r in labelled if r["signals"][signal] is not None]
        per_signal[signal] = _metrics([float(r["signals"][signal]) for r in rows],
                                      [bool(r["correct"]) for r in rows])
    candidates = [s for s in SIGNALS
                  if per_signal[s].get("auroc") is not None
                  and s in _produced(fast.confidence.method)]
    best = max(candidates, key=lambda s: per_signal[s]["auroc"]) if candidates else None
    fit = None
    if best is not None:
        rows = [r for r in labelled if r["signals"][best] is not None]
        points = llm_confidence.fit_isotonic(
            [float(r["signals"][best]) for r in rows],
            [100.0 if r["correct"] else 0.0 for r in rows],
        )
        sources = {r["label_source"] for r in rows}
        fit = {
            "signal": best,
            "points": points,
            "n": len(rows),
            "fitted_on": dt.date.today().isoformat(),
            "ground_truth": "mixed" if len(sources) > 1 else next(iter(sources)),
        }
    return {
        "model": fast.id,
        "method": fast.confidence.method,
        "units": len(records),
        "labelled": len(labelled),
        "label_sources": dict(Counter(r["label_source"] for r in labelled)),
        "sources": dict(Counter(r["source"] for r in records)),
        "signals": per_signal,
        "best_signal": best,
        "beats_stated": (
            best is not None and best != "stated"
            and per_signal["stated"].get("auroc") is not None
            and per_signal[best]["auroc"] > per_signal["stated"]["auroc"]
        ),
        "calibration": fit,
        "calls": meter.summary(),
        "elapsed_seconds": round(elapsed, 1),
    }


def _produced(method: str) -> tuple[str, ...]:
    from llm.models import SIGNALS_BY_METHOD

    return SIGNALS_BY_METHOD.get(method, ("stated",))


def _fmt(value, spec: str) -> str:
    return "n/a" if value is None else format(value, spec)


def _print_summary(summary: dict) -> None:
    header = f"{'signal':<22}{'n':>6}{'acc':>8}{'auroc':>8}{'ece':>8}{'brier':>8}  histogram (0..1)"
    print()
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for signal, row in summary["signals"].items():
        if not row.get("n"):
            print(f"{signal:<22}{0:>6}")
            continue
        hist = " ".join(str(c) for c in row["histogram"])
        print(f"{signal:<22}{row['n']:>6}{_fmt(row['accuracy'], '.3f'):>8}"
              f"{_fmt(row['auroc'], '.3f'):>8}{_fmt(row['ece'], '.3f'):>8}"
              f"{_fmt(row['brier'], '.3f'):>8}  {hist}")
    print("=" * len(header))
    print(f"units {summary['units']}, labelled {summary['labelled']} "
          f"({summary['label_sources']}), sources {summary['sources']}, "
          f"{summary['elapsed_seconds']}s")
    for model_id, row in summary["calls"].items():
        print(f"  {model_id}: {row['calls']} calls, p50 {row['seconds_p50']}s, "
              f"mean {row['seconds_mean']}s, {row['response_bytes_mean']} bytes/response, "
              f"{row['with_logprobs']} with logprobs")
    fit = summary["calibration"]
    if fit is None:
        print("\nNo calibration: no signal had both right and wrong answers to rank.")
        return
    if summary["beats_stated"]:
        verdict = f"{fit['signal']} beats stated on AUROC — the block below is worth pasting"
    elif fit["signal"] == "stated":
        verdict = ("stated ranks best: the measured signals add nothing here; a stated "
                   "calibration only corrects the level")
    else:
        verdict = (f"{fit['signal']} does NOT beat stated on AUROC — keep the stated "
                   "headline; the block is printed for the record")
    print(f"\n{verdict}. Under the entry in models.yaml (shadow: leave headline: stated "
          "until you decide to flip it):\n")
    points = ", ".join(f"[{x}, {y}]" for x, y in fit["points"])
    print("    confidence:")
    print(f"      method: {summary['method']}")
    print("      calibration:")
    print(f"        signal: {fit['signal']}")
    print(f"        points: [{points}]")
    print(f"        n: {fit['n']}")
    print(f"        fitted_on: \"{fit['fitted_on']}\"")
    print(f"        ground_truth: {fit['ground_truth']}")


def _export(path: str, records: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["file", "item", "page", "criterion", "fast_verdict", "fast_score",
                         "fast_reason", "deliberate_label", "expected"])
        for r in records:
            writer.writerow([r["file"], r["item"], r["page"], r["criterion"],
                             r["fast"]["verdict"], r["fast"]["score"], r["fast"]["reason"],
                             r["deliberate_label"] or "", ""])
    print(f"wrote {path} — fill in `expected`, then pass it back as --labels JSON")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args) -> dict:
    directory = pathlib.Path(args.docs).resolve()
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    criteria_map = _criteria(pathlib.Path(args.criteria).resolve(), [p.name for p in files])
    labels = _labels(args.labels)
    fast, deliberate = _specs(args)
    llm_usage.usage_store = _NoStore()
    meter = _Meter(llm_client._send)
    llm_client._send = meter

    print(f"model      : {fast.id} ({fast.served_name}) at {fast.url}, "
          f"confidence {fast.confidence.method}/{fast.confidence.headline}")
    print(f"deliberate : {deliberate.id} reasoning={deliberate.reasoning} "
          f"max_tokens.score={deliberate.max_tokens['score']} timeout={deliberate.timeout_s:g}s "
          f"x{args.samples}")
    print(f"concurrency: {args.concurrency}; documents under {directory}\n")

    started = time.monotonic()
    records: list[dict] = []
    for path in files:
        items = criteria_map.get(path.name) or []
        if not items:
            continue
        try:
            records += await _run_file(path, _criterion_inputs(items), fast, deliberate,
                                       args, labels)
        except Exception as exc:  # noqa: BLE001 — one bad file must not end the run
            print(f"{path.name}: skipped — {exc}", file=sys.stderr)

    summary = _summarise(records, fast, meter, time.monotonic() - started)
    _print_summary(summary)
    if args.export_labels:
        _export(args.export_labels, records)
    if args.out:
        out = pathlib.Path(args.out)
        out.write_text(json.dumps({"summary": summary, "records": records}, indent=2),
                       encoding="utf-8")
        print(f"\nwrote {out}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure and calibrate an llm model's confidence signals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--docs", required=True, help="directory of documents (recursive)")
    parser.add_argument("--criteria", required=True,
                        help='JSON: {"file": [criterion, ...]} or a bare list for every file')
    parser.add_argument("--model", default=None,
                        help="registry model id (GET /models; default: the registry default)")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="model requests at once in each pass (default 1)")
    parser.add_argument("--samples", type=int, default=1,
                        help="deliberate runs per unit; the label is their majority (default 1)")
    parser.add_argument("--labels", default=None,
                        help="human labels JSON {file: {criterion: verdict}}; overrides")
    parser.add_argument("--export-labels", default=None,
                        help="write a CSV of the units for a person to label")
    parser.add_argument("--deliberate-reasoning", default="xhigh",
                        help="the deliberate pass's reasoning value (default xhigh)")
    parser.add_argument("--deliberate-max-tokens", type=int, default=32768,
                        help="the deliberate pass's max_tokens.score (default 32768)")
    parser.add_argument("--deliberate-timeout", type=float, default=900,
                        help="the deliberate pass's timeout_s (default 900)")
    parser.add_argument("--out", default=None, help="write the full JSON report here")
    args = parser.parse_args()
    if args.concurrency < 1 or args.samples < 1:
        parser.error("--concurrency and --samples must be >= 1")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
