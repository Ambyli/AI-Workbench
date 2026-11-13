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

**``--mode fast``** (ZT-603: the fast readout and adaptive's threshold). The
default above (``--mode reasoned``) is unchanged. For every LABELLED
(criterion, page) — ``--labels`` is required: a person's verdicts, or the
``--export-labels`` CSV's deliberate labels turned into that JSON — it runs:

  1. **The readout**: ``analyze_document`` with the criterion's
     ``options.mode`` "fast" on ``--model`` (which needs a ``fast:`` block),
     capturing each unit's raw ``common.logprobs.OptionReadout`` (an
     incomplete / capped readout is recorded as such — adaptive would
     escalate it);
  2. **The reasoned answer**: the same criteria reasoned on the escalation
     model — ``--escalation-model``, else the registry's
     (``CLASSIFIER_ESCALATION_MODEL``, else the default) — what adaptive
     would escalate to.

Then it fits a TEMPERATURE for the ``rating`` answer type — the one that
minimises the log loss of the labelled verdict's band under ``p ∝
exp(logprob / T)``, a stdlib grid search over ``TEMPERATURE_GRID`` — and
prints accuracy / AUROC / ECE / Brier / seconds per unit for the raw readout,
the calibrated one and the reasoned baseline (the signal: each answer's
headline confidence / 100; over the units that answer answered, ``n``) —
and ``acc/all``, each one's accuracy over EVERY labelled unit with an
unanswered one counted wrong — and an **escalation curve**: for each
threshold 50..95 (step 5), the share of units adaptive would escalate
(calibrated readout confidence under it, or an incomplete / capped readout),
the accuracy of the adaptive result over every labelled unit (the readout at
or over the threshold, else the reasoned answer — a reasoned call that
failed keeps a complete readout's; a readout whose own request failed is
not escalated, the unit errors and counts wrong, as in production) and the
model-seconds per unit (the readout, plus the reasoned call when escalated).
The suggested ``escalate_below`` is the lowest threshold whose accuracy is
within ``--max-accuracy-loss`` (0.01) of the reasoned ``acc/all`` (else the
highest), printed with the calibration as a block to merge under the
entry's ``fast:``::

    docker exec classifier uv run python bin/confidence_calibration.py --mode fast \\
        --docs /data/calibration/docs --criteria /data/calibration/criteria.json \\
        --labels /data/calibration/labels.json --model qwen3.8-solo \\
        --concurrency 1 --out /data/calibration-fast.json

Fit on the evaluation set's ``tune`` split; check ``holdout`` once with the
block pasted (a fitted entry's readouts are reported calibrated, and the
records carry the raw logprobs either way). ``--samples``, the
``--deliberate-*`` knobs and ``--export-labels`` belong to the default mode.

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
import math
import pathlib
import statistics
import sys
import time
import types
from collections import Counter
from typing import Any, Optional

# The classifier's modules import each other by bare name (the container's
# WORKDIR). Running this from `bin/` needs the parent on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import analysis  # noqa: E402
from api.schemas import CriterionInput  # noqa: E402
from common.logprobs import OptionReadout, softmax_temperature  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_ANSWER_TYPE,
    FAST_DIGITS,
    FAST_SCORE_OFFSET,
)
from llm import client as llm_client  # noqa: E402
from llm import confidence as llm_confidence  # noqa: E402
from llm import usage as llm_usage  # noqa: E402
from llm.models import ConfidenceConfig, ModelSpec, Registry, get_registry, use_registry  # noqa: E402
from utils import verdict_from_score  # noqa: E402

VERDICTS = ("PASS", "MARGINAL", "FAIL")
SIGNALS = ("stated", "verdict_probability", "certainty", "agreement")
BINS = 10

# --mode fast: the temperatures the log-loss fit tries (log-spaced from MIN to
# MAX; 1.0 — the raw readout — is one of them), the floor on a probability
# whose log is taken, and the escalation curve's thresholds.
TEMPERATURE_MIN = 0.25
TEMPERATURE_MAX = 8.0
TEMPERATURE_STEPS = 81
TEMPERATURE_GRID: tuple[float, ...] = tuple(
    round(TEMPERATURE_MIN * (TEMPERATURE_MAX / TEMPERATURE_MIN) ** (i / (TEMPERATURE_STEPS - 1)), 4)
    for i in range(TEMPERATURE_STEPS)
)
LOG_LOSS_FLOOR = 1e-12
CURVE_THRESHOLDS: tuple[int, ...] = tuple(range(50, 100, 5))
DEFAULT_MAX_ACCURACY_LOSS = 0.01


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
# --mode fast: the readout's temperature and adaptive's escalation curve
# ---------------------------------------------------------------------------


def _fast_specs(args) -> tuple[ModelSpec, ModelSpec]:
    """``(readout model, escalation model)`` registered in a COPY of the
    registry, each with this process's own limit (production's slots are the
    container's). The escalation model is ``--escalation-model``, else the
    registry's (``CLASSIFIER_ESCALATION_MODEL``, else the default)."""
    registry = get_registry()
    try:
        spec = registry.check_selectable(args.model) if args.model else registry.default
    except ValueError as exc:
        raise SystemExit(f"--model: {exc}")
    if spec.fast is None:
        raise SystemExit(
            f"--mode fast: model {spec.id!r} has no fast block in models.yaml "
            f"(fast-capable: {registry.fast_ids()}); pass --model"
        )
    try:
        escalation = registry.check_selectable(args.escalation_model or registry.escalation_id)
    except ValueError as exc:
        raise SystemExit(f"--escalation-model: {exc}")
    fast = dataclasses.replace(spec, max_concurrent=max(args.concurrency, spec.confidence.slots))
    reasoned = fast if escalation.id == spec.id else dataclasses.replace(
        escalation, max_concurrent=max(args.concurrency, escalation.confidence.slots),
    )
    models = {**registry.models, fast.id: fast, reasoned.id: reasoned}
    use_registry(Registry(models, registry.default_id, path=registry.path, escalation=reasoned.id))
    return fast, reasoned


class _UnitMeter(_Meter):
    """``_Meter``, plus the request seconds per (criterion, item) and pass
    (``phase``: "fast" | "reasoned", set by the caller around each pass) —
    the unit is the one ``analysis.scheduler`` scoped the call to."""

    def __init__(self, send):
        super().__init__(send)
        self.phase = "fast"
        self.unit_seconds: dict[tuple[str, int, str], float] = {}

    async def __call__(self, prompt: dict) -> dict:
        unit = llm_usage.unit_var.get() or {}
        t0 = time.monotonic()
        try:
            return await super().__call__(prompt)
        finally:
            key = (unit.get("criterion"), unit.get("item"), self.phase)
            self.unit_seconds[key] = self.unit_seconds.get(key, 0.0) + time.monotonic() - t0


class _ReadoutCapture:
    """Wraps ``llm.client.call_readout``: each unit's raw readout (an
    ``OptionReadout``), or the ``ReadoutIncomplete`` it raised, by
    (criterion, item)."""

    def __init__(self, call):
        self._call = call
        self.seen: dict[tuple[str, int], Any] = {}

    async def __call__(self, *args, **kwargs):
        unit = llm_usage.unit_var.get() or {}
        key = (unit.get("criterion"), unit.get("item"))
        try:
            readout = await self._call(*args, **kwargs)
        except llm_client.ReadoutIncomplete as exc:
            self.seen[key] = exc
            raise
        self.seen[key] = readout
        return readout


def _all_units(result: dict) -> dict[tuple[str, int], dict]:
    """(criterion, item) → the item entry, whatever its status."""
    out = {}
    for name, entry in result["assessment"]["per_criterion_scores"].items():
        for unit in entry.get("items") or []:
            if unit.get("item") is not None:
                out[(name, unit["item"])] = unit
    return out


def _fast_criteria(items: list[dict], mode: Optional[str]) -> list[CriterionInput]:
    """The criteria for one pass: their own options without ``model`` /
    ``mode`` / ``escalate_below`` (each pass sets the model and the mode),
    ``mode`` set when given, ``boxes`` off."""
    out = []
    for c in items:
        options = {k: v for k, v in c["options"].items()
                   if k not in ("model", "mode", "escalate_below")}
        if mode is not None:
            options["mode"] = mode
        out.append({"name": c["name"], "options": options})
    return _criterion_inputs(out)


def _bands(logprobs: dict[str, float], temperature: float) -> dict[str, float]:
    """The verdict bands' probabilities of a readout under ``temperature``
    (``p ∝ exp(logprob / T)`` over the digits it saw — the scaling
    ``llm.confidence.rate_readout`` applies), unrounded for the log loss."""
    probs = softmax_temperature(logprobs, temperature)
    bands = dict.fromkeys(VERDICTS, 0.0)
    for digit in FAST_DIGITS:
        bands[verdict_from_score(int(digit) + FAST_SCORE_OFFSET)] += probs.get(digit, 0.0)
    return bands


def _rate(row: dict, temperature: float) -> dict:
    """``{verdict, confidence}`` of a recorded readout under ``temperature`` —
    ``llm.confidence.rate_readout``, so it is what production would report
    with that calibration fitted."""
    readout = types.SimpleNamespace(
        logprobs=row["logprobs"], probs={}, coverage=row["coverage"],
        unseen_bound=row.get("unseen_bound") or 0.0,
    )
    rated = llm_confidence.rate_readout(readout, types.SimpleNamespace(temperature=temperature))
    return {"verdict": rated["verdict"], "confidence": rated["confidence"]}


def _log_loss(rows: list[dict], temperature: float) -> float:
    """Mean −log P(the labelled verdict's band) under ``temperature``."""
    return statistics.fmean(
        -math.log(max(LOG_LOSS_FLOOR, _bands(r["logprobs"], temperature)[r["label"]]))
        for r in rows
    )


def _fit_temperature(rows: list[dict]) -> Optional[dict]:
    """The ``TEMPERATURE_GRID`` value with the least log loss on ``rows``
    (labelled, complete readouts); a tie goes to the one nearest 1. None
    without rows."""
    if not rows:
        return None
    losses = {t: _log_loss(rows, t) for t in TEMPERATURE_GRID}
    best = min(TEMPERATURE_GRID, key=lambda t: (round(losses[t], 12), abs(math.log(t))))
    return {"temperature": best, "log_loss": round(losses[best], 4),
            "log_loss_raw": round(_log_loss(rows, 1.0), 4), "n": len(rows)}


async def _run_file_fast(path: pathlib.Path, items: list[dict], fast: ModelSpec,
                         reasoned: ModelSpec, labels: dict, meter: _UnitMeter,
                         capture: _ReadoutCapture) -> list[dict]:
    raw = path.read_bytes()
    doc = analysis.load_document_bytes(raw, path.name, None, keep_source=True)
    # Both are keyed by (criterion, item), and item indices restart at 0 in
    # every file: cleared per file, or a criterion name shared by several
    # files would carry the earlier files' readouts and seconds.
    capture.seen.clear()
    meter.unit_seconds.clear()
    meter.phase = "fast"
    fast_result = await analysis.analyze_document(
        doc, _fast_criteria(items, "fast"), default_model=fast.id)
    readouts = dict(capture.seen)
    meter.phase = "reasoned"
    reasoned_units = _units(await analysis.analyze_document(
        doc, _fast_criteria(items, None), default_model=reasoned.id))

    records = []
    for (name, item), unit in _all_units(fast_result).items():
        label = _human_label(labels, path.name, name, item)
        seen = readouts.get((name, item))
        complete = isinstance(seen, OptionReadout)
        readout = seen if complete else getattr(seen, "readout", None)
        answer = reasoned_units.get((name, item))
        records.append({
            "file": path.name,
            "item": item,
            "page": unit.get("page"),
            "criterion": name,
            "label": label,
            "readout": ("ok" if complete else "cap" if isinstance(seen, llm_client.ReadoutCapped)
                        else "incomplete" if seen is not None else "error"),
            "logprobs": dict(readout.logprobs) if complete else None,
            "coverage": round(float(readout.coverage), 4) if readout is not None else None,
            "unseen_bound": round(float(readout.unseen_bound), 6) if readout is not None else None,
            "fast": ({"verdict": unit["verdict"], "confidence": unit["confidence"]}
                     if unit.get("status") == "ok" else None),
            "fast_seconds": round(meter.unit_seconds.get((name, item, "fast"), 0.0), 4),
            "reasoned": ({"verdict": answer["verdict"], "confidence": answer["confidence"]}
                         if answer is not None else None),
            "reasoned_seconds": round(meter.unit_seconds.get((name, item, "reasoned"), 0.0), 4),
        })
    labelled = [r for r in records if r["label"]]
    print(f"{path.name}: {len(records)} unit(s), {len(labelled)} labelled, "
          f"{sum(1 for r in labelled if r['readout'] != 'ok')} unreadable readout(s)")
    return records


def _curve(rows: list[dict], temperature: float) -> list[dict]:
    """The escalation curve over ``rows`` (labelled): per threshold, the
    escalation rate, adaptive's accuracy and model-seconds per unit, with
    the readout calibrated at ``temperature`` — what production's
    ``analysis.llm_eval._adaptive`` would do with each unit: a readout at or
    over the threshold stands; an unsure or unreadable (incomplete / capped)
    one escalates, and a failed escalation keeps a complete readout's
    answer; a readout whose own request failed, or a unit that failed before
    it (``readout`` "error"), is NOT escalated — the unit errors, so it
    counts wrong. Every row is in the denominator, as in the reasoned
    baseline's ``accuracy_all``."""
    out = []
    for threshold in CURVE_THRESHOLDS:
        escalated = correct = 0
        seconds = 0.0
        for r in rows:
            seconds += r["fast_seconds"]
            if r["readout"] == "error":
                continue
            rated = _rate(r, temperature) if r["readout"] == "ok" else None
            if rated is not None and rated["confidence"] >= threshold:
                correct += rated["verdict"] == r["label"]
                continue
            escalated += 1
            seconds += r["reasoned_seconds"]
            if r["reasoned"] is not None:
                correct += r["reasoned"]["verdict"] == r["label"]
            elif rated is not None:
                # A failed escalation keeps a complete readout's answer.
                correct += rated["verdict"] == r["label"]
        n = len(rows)
        out.append({
            "threshold": threshold,
            "escalation_rate": round(escalated / n, 4) if n else None,
            "accuracy": round(correct / n, 4) if n else None,
            "model_seconds_per_item": round(seconds / n, 4) if n else None,
        })
    return out


def _accuracy_all(right: list[bool], labelled: list[dict]) -> Optional[float]:
    """The share of EVERY labelled unit answered right — a unit with no
    answer (an unreadable readout in mode fast, a failed reasoned call)
    counts wrong — so the rows and the curve compare like for like."""
    return round(sum(right) / len(labelled), 4) if labelled else None


def _summarise_fast(records: list[dict], fast: ModelSpec, reasoned: ModelSpec, args,
                    meter: _UnitMeter, elapsed: float) -> dict:
    """Each row's metrics are over the units it answered (``n``; AUROC / ECE /
    Brier need its confidence), plus ``accuracy_all`` over every labelled
    unit. The curve's accuracy is over every labelled unit too, so the
    suggested threshold is compared with the reasoned ``accuracy_all``."""
    labelled = [r for r in records if r["label"]]
    complete = [r for r in labelled if r["readout"] == "ok"]
    fit = _fit_temperature(complete)
    temperature = fit["temperature"] if fit else 1.0
    rows = {}
    for row_name, t in (("fast_raw", 1.0), ("fast_calibrated", temperature)):
        rated = [(_rate(r, t), r) for r in complete]
        right = [x["verdict"] == r["label"] for x, r in rated]
        rows[row_name] = {
            **_metrics([x["confidence"] / 100.0 for x, _ in rated], right),
            "accuracy_all": _accuracy_all(right, labelled),
            "temperature": t,
            "seconds_mean": (round(statistics.fmean(r["fast_seconds"] for r in complete), 4)
                             if complete else None),
        }
    answered = [r for r in labelled if r["reasoned"] is not None]
    right = [r["reasoned"]["verdict"] == r["label"] for r in answered]
    rows["reasoned"] = {
        **_metrics([r["reasoned"]["confidence"] / 100.0 for r in answered], right),
        "accuracy_all": _accuracy_all(right, labelled),
        "model": reasoned.id,
        "seconds_mean": (round(statistics.fmean(r["reasoned_seconds"] for r in answered), 4)
                         if answered else None),
    }
    curve = _curve(labelled, temperature)
    baseline = rows["reasoned"]["accuracy_all"]
    suggested = None
    if curve and baseline is not None:
        floor = baseline - args.max_accuracy_loss
        within = [c for c in curve if c["accuracy"] is not None and c["accuracy"] >= floor]
        suggested = within[0]["threshold"] if within else curve[-1]["threshold"]
    block = None
    if fit is not None:
        block = {
            "calibration": {DEFAULT_ANSWER_TYPE: {
                "temperature": temperature, "n": fit["n"],
                "fitted_on": dt.date.today().isoformat(), "ground_truth": args.ground_truth,
            }},
            "escalate_below": {DEFAULT_ANSWER_TYPE: suggested} if suggested is not None else {},
        }
    return {
        "mode": "fast",
        "model": fast.id,
        "escalation_model": reasoned.id,
        "units": len(records),
        "labelled": len(labelled),
        "unreadable": dict(Counter(r["readout"] for r in labelled if r["readout"] != "ok")),
        "reasoned_failed": len(labelled) - len(answered),
        "temperature_fit": fit,
        "rows": rows,
        "curve": curve,
        "max_accuracy_loss": args.max_accuracy_loss,
        "suggested_escalate_below": suggested,
        "fast_block": block,
        "calls": meter.summary(),
        "elapsed_seconds": round(elapsed, 1),
    }


def _print_fast_summary(summary: dict) -> None:
    header = (f"{'answer':<26}{'n':>6}{'acc':>8}{'acc/all':>9}{'auroc':>8}{'ece':>8}"
              f"{'brier':>8}{'s/unit':>9}")
    print()
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    names = {
        "fast_raw": "fast readout (raw)",
        "fast_calibrated": f"fast readout (T={summary['rows']['fast_calibrated']['temperature']})",
        "reasoned": f"reasoned ({summary['escalation_model']})",
    }
    for key, title in names.items():
        row = summary["rows"][key]
        if not row.get("n"):
            print(f"{title:<26}{0:>6}{_fmt(None, ''):>8}"
                  f"{_fmt(row.get('accuracy_all'), '.3f'):>9}")
            continue
        print(f"{title:<26}{row['n']:>6}{_fmt(row['accuracy'], '.3f'):>8}"
              f"{_fmt(row['accuracy_all'], '.3f'):>9}"
              f"{_fmt(row['auroc'], '.3f'):>8}{_fmt(row['ece'], '.3f'):>8}"
              f"{_fmt(row['brier'], '.3f'):>8}{_fmt(row['seconds_mean'], '.2f'):>9}")
    print("=" * len(header))
    print(f"n / acc / auroc / ece / brier: the units each answer answered; acc/all: all "
          f"{summary['labelled']} labelled units, an unanswered one counted wrong (as the "
          "curve below)")
    print(f"units {summary['units']}, labelled {summary['labelled']}, unreadable readouts "
          f"{summary['unreadable'] or 0} (adaptive escalates incomplete / cap; error: the "
          f"readout's request failed — the unit errors), failed reasoned calls "
          f"{summary['reasoned_failed']}, {summary['elapsed_seconds']}s")
    fit = summary["temperature_fit"]
    if fit is None:
        print("\nNo calibration: no labelled unit had a complete readout.")
        return
    print(f"temperature {fit['temperature']}: log loss {fit['log_loss']} "
          f"(raw {fit['log_loss_raw']}) on {fit['n']} readouts")
    print("\nEscalation curve (the calibrated readout's confidence under the threshold — or "
          "an incomplete / capped readout — escalates; accuracy over all labelled units):")
    print(f"  {'below':>5}  {'escalated':>9}  {'accuracy':>8}  {'model-s/item':>12}")
    for c in summary["curve"]:
        print(f"  {c['threshold']:>5}  {_fmt(c['escalation_rate'], '.1%'):>9}  "
              f"{_fmt(c['accuracy'], '.3f'):>8}  {_fmt(c['model_seconds_per_item'], '.2f'):>12}")
    suggested = summary["suggested_escalate_below"]
    baseline = summary["rows"]["reasoned"].get("accuracy_all")
    if suggested is not None:
        print(f"\nsuggested escalate_below: {suggested} (the lowest threshold within "
              f"{summary['max_accuracy_loss']} of the reasoned acc/all {baseline})")
    block = summary["fast_block"]
    print(f"\nMerge under the {summary['model']} entry's `fast:` block in models.yaml "
          "(then `docker restart classifier`):\n")
    cal = block["calibration"][DEFAULT_ANSWER_TYPE]
    print("      calibration:")
    print(f"        {DEFAULT_ANSWER_TYPE}: {{temperature: {cal['temperature']}, n: {cal['n']}, "
          f"fitted_on: \"{cal['fitted_on']}\", ground_truth: {cal['ground_truth']}}}")
    if block["escalate_below"]:
        print(f"      escalate_below: {{{DEFAULT_ANSWER_TYPE}: "
              f"{block['escalate_below'][DEFAULT_ANSWER_TYPE]}}}")


async def run_fast(args) -> dict:
    """``--mode fast`` (the module docstring): the readout and the reasoned
    answer on every labelled unit, the temperature, the curve, the block."""
    directory = pathlib.Path(args.docs).resolve()
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    criteria_map = _criteria(pathlib.Path(args.criteria).resolve(), [p.name for p in files])
    labels = _labels(args.labels)
    if not labels:
        raise SystemExit(
            "--mode fast needs --labels: {file: {criterion: verdict}} (fill the --export-labels "
            "CSV of the default mode, or a person's verdicts)"
        )
    fast, reasoned = _fast_specs(args)
    llm_usage.usage_store = _NoStore()
    meter = _UnitMeter(llm_client._send)
    llm_client._send = meter
    capture = _ReadoutCapture(llm_client.call_readout)
    llm_client.call_readout = capture

    print(f"readout    : {fast.id} ({fast.served_name}) at {fast.url}, top_logprobs "
          f"{fast.fast.top_logprobs}")
    print(f"escalation : {reasoned.id} ({reasoned.served_name}) at {reasoned.url}, "
          f"confidence {reasoned.confidence.method}/{reasoned.confidence.headline}")
    print(f"concurrency: {args.concurrency}; documents under {directory}\n")

    started = time.monotonic()
    records: list[dict] = []
    for path in files:
        items = criteria_map.get(path.name) or []
        if not items:
            continue
        try:
            records += await _run_file_fast(path, items, fast, reasoned, labels, meter, capture)
        except Exception as exc:  # noqa: BLE001 — one bad file must not end the run
            print(f"{path.name}: skipped — {exc}", file=sys.stderr)

    summary = _summarise_fast(records, fast, reasoned, args, meter, time.monotonic() - started)
    _print_fast_summary(summary)
    if args.out:
        out = pathlib.Path(args.out)
        out.write_text(json.dumps({"summary": summary, "records": records}, indent=2),
                       encoding="utf-8")
        print(f"\nwrote {out}")
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args) -> dict:
    if getattr(args, "mode", "reasoned") == "fast":
        return await run_fast(args)
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
    parser.add_argument("--mode", choices=("reasoned", "fast"), default="reasoned",
                        help="reasoned (default): the scoring call's confidence signals; fast: "
                             "the readout's temperature and adaptive's escalation curve")
    parser.add_argument("--escalation-model", default=None,
                        help="--mode fast: the reasoned model (default: the registry's "
                             "escalation model, CLASSIFIER_ESCALATION_MODEL, else the default)")
    parser.add_argument("--max-accuracy-loss", type=float, default=DEFAULT_MAX_ACCURACY_LOSS,
                        help="--mode fast: the suggested escalate_below is the lowest threshold "
                             "whose accuracy is within this of the reasoned baseline's "
                             f"(default {DEFAULT_MAX_ACCURACY_LOSS})")
    parser.add_argument("--ground-truth", default="human",
                        help="--mode fast: the calibration's ground_truth (default human)")
    args = parser.parse_args()
    if args.concurrency < 1 or args.samples < 1:
        parser.error("--concurrency and --samples must be >= 1")
    if args.mode == "fast" and not args.labels:
        parser.error("--mode fast needs --labels")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
