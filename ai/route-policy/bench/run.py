"""Classify decision points with candidate models x criteria sets, via /assess.

    python -m bench.run --points DATA/points.jsonl --results DATA/results.jsonl \\
        --models qwen3.5-4b,muse-glimmer,qwen3.8-solo \\
        --ruleset rulesets/default-llm.yaml --evaluator topic-llm

Every point x model x criteria set is ONE classification through
route-policy's own ``ClassifierClient`` -- the production ``/assess`` cycle
(locality guard, one ``text`` document and one ``llm`` presence criterion per
route, poll, read, always DELETE the job) under the classifier's own
per-model slot limits:

1. render the point the way the ``llm_classifier`` does --
   ``render_transcript(messages, exchanges=history.exchanges,
   max_chars=history.max_chars, upto_user=k)``;
2. ``assess(model, transcript, criteria texts)``;
3. map each criterion's score back to its route and pick the route with
   ``analysis.verdict.choose`` (``min_score``, ``otherwise``, the criteria
   order as the tie order) -- exactly as the analysis queue does.

**Criteria sets** are ``--ruleset`` / ``--evaluator`` pairs (repeat both to
compare several; one ``--ruleset`` with several ``--evaluator`` uses that
file for each), loaded and validated by ``criteria.load_criteria_set``.

**Load.** Work is point-major (every candidate gets the same points first,
so an interrupted run still compares like with like) and sequential by
default; ``--concurrency`` is capped at 2. More in flight would queue behind
the classifier's per-model limit anyway, and a chat model whose vLLM queue
stays busy makes LiteLLM's overflow hook spill live chat turns -- to Claude,
on the Claude-capable chains. Run it off-hours (BENCH.md).

**Resumable.** Results are appended to ``--results`` one line at a time;
a rerun of the same command skips every (point, model, criteria
fingerprint) already there -- with ``--retry-failed``, only those that
produced scores (``ok`` / ``incomplete``), so failures are tried again (the
report keeps the LAST line per key). One result line::

    {"point_id", "model", "criteria_set": "default-llm@1/topic-llm",
     "criteria_fingerprint", "route", "confidence", "scores": {route: 1-10 | null},
     "reason", "status", "error", "latency_ms", "queue_ms", "model_seconds",
     "calls", "prompt_tokens", "completion_tokens", "input_chars", "at"}

``reason`` is the winning criterion's reason (the classifier caps it at 200
characters, as route-policy stores it); ``status`` is the client's (``ok``,
``incomplete``, ``unavailable``, ``refused``, ``failed``, ``timeout``,
``not_local``) or ``skipped`` when the point rendered no transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, TextIO

from common.conversation import render_transcript

from route_policy.analysis.classifier_client import AssessOutcome, ClassifierClient
from route_policy.analysis.verdict import choose

from .criteria import CriteriaSet, load_criteria_set
from .jsonl import Appender, read_jsonl, warn
from .metrics import SCORED_STATUSES
from .results import ResultKey, latest

DEFAULT_MODELS = "qwen3.5-4b,muse-glimmer,qwen3.8-solo"
DEFAULT_CLASSIFIER_URL = "http://classifier:8000"
DEFAULT_RULESET = "rulesets/default-llm.yaml"
DEFAULT_EVALUATOR = "topic-llm"
MAX_CONCURRENCY = 2
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_POLL_S = 1.0


@dataclass(frozen=True)
class WorkItem:
    point: Dict[str, Any]
    model: str
    criteria: CriteriaSet

    @property
    def key(self) -> ResultKey:
        return (self.point["point_id"], self.model, self.criteria.fingerprint)


def finished_keys(results: Iterable[Dict[str, Any]], *, retry_failed: bool = False) -> Set[ResultKey]:
    """The keys a rerun skips. With ``retry_failed`` a key counts only when
    its LAST result produced scores."""
    last = latest(results)
    if not retry_failed:
        return set(last)
    return {key for key, result in last.items() if result.get("status") in SCORED_STATUSES}


def plan(points: Sequence[Dict[str, Any]], models: Sequence[str], sets: Sequence[CriteriaSet],
         done: Set[ResultKey]) -> List[WorkItem]:
    """Point-major: every (model, criteria set) of a point before the next point."""
    work = []
    for point in points:
        for criteria in sets:
            for model in models:
                item = WorkItem(point, model, criteria)
                if item.key not in done:
                    work.append(item)
    return work


def transcript_for(point: Dict[str, Any], criteria: CriteriaSet) -> str:
    return render_transcript(point.get("messages") or [], exchanges=criteria.exchanges,
                             max_chars=criteria.max_chars, upto_user=point.get("k"))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def result_line(item: WorkItem, outcome: Optional[AssessOutcome], *, input_chars: int,
                skipped: Optional[str] = None) -> Dict[str, Any]:
    """One results.jsonl line for ``item`` (``outcome`` None = skipped)."""
    criteria = item.criteria
    line: Dict[str, Any] = {
        "point_id": item.point["point_id"],
        "model": item.model,
        "criteria_set": criteria.id,
        "criteria_fingerprint": criteria.fingerprint,
    }
    if outcome is None:
        line.update({"route": None, "confidence": None, "scores": {r: None for r in criteria.routes},
                     "reason": None, "status": "skipped", "error": skipped, "latency_ms": None,
                     "queue_ms": None, "model_seconds": None, "calls": None, "prompt_tokens": None,
                     "completion_tokens": None, "input_chars": input_chars, "at": now_iso()})
        return line
    scores: Dict[str, Optional[int]] = {}
    reasons: Dict[str, Optional[str]] = {}
    for route, text in zip(criteria.routes, criteria.texts):
        scored = outcome.scores.get(text)
        scores[route] = scored.score if scored is not None else None
        reasons[route] = scored.reason if scored is not None else None
    route, confidence = (None, None)
    if outcome.status in SCORED_STATUSES:
        route, confidence = choose(scores, order=criteria.routes, min_score=criteria.min_score,
                                   otherwise=criteria.otherwise)
    line.update({
        "route": route,
        "confidence": round(confidence, 4) if confidence is not None else None,
        "scores": scores,
        "reason": reasons.get(route) if route is not None else None,
        "status": outcome.status,
        "error": outcome.error,
        "latency_ms": round(outcome.latency_ms, 1) if outcome.latency_ms is not None else None,
        "queue_ms": round(outcome.queue_ms, 1) if outcome.queue_ms is not None else None,
        "model_seconds": outcome.model_seconds,
        "calls": outcome.calls,
        "prompt_tokens": outcome.prompt_tokens,
        "completion_tokens": outcome.completion_tokens,
        "input_chars": input_chars,
        "at": now_iso(),
    })
    return line


async def classify(client: Any, item: WorkItem) -> Dict[str, Any]:
    text = transcript_for(item.point, item.criteria)
    if not text:
        return result_line(item, None, input_chars=0,
                           skipped=f"no transcript: k={item.point.get('k')!r} is not a user message of the point")
    outcome = await client.assess(model=item.model, text=text, criteria=item.criteria.texts)
    return result_line(item, outcome, input_chars=len(text))


class Progress:
    """A stderr line about every ``every`` points' worth of work."""

    def __init__(self, total: int, per_point: int, every: int, stream: Optional[TextIO] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.total, self.stream, self.clock = total, stream, clock
        self.step = max(1, every * max(1, per_point)) if every > 0 else 0
        self.done = 0
        self.statuses: Counter = Counter()
        self.latencies: List[float] = []
        self.started = clock()

    def add(self, result: Dict[str, Any]) -> None:
        self.done += 1
        self.statuses[result.get("status")] += 1
        if isinstance(result.get("latency_ms"), (int, float)):
            self.latencies.append(float(result["latency_ms"]))
        if self.step and self.done % self.step == 0 and self.done < self.total:
            self.report()   # the last one is execute()'s closing line

    def line(self) -> str:
        elapsed = self.clock() - self.started
        mean = sum(self.latencies) / len(self.latencies) / 1000.0 if self.latencies else 0.0
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(self.statuses.items(), key=lambda kv: str(kv[0])))
        return (f"run: {self.done}/{self.total} classifications ({statuses}); mean latency {mean:.1f} s; "
                f"elapsed {elapsed / 60.0:.1f} min")

    def report(self) -> None:
        warn(self.line(), self.stream)


async def run_bench(work: Sequence[WorkItem], client: Any, sink: Any, *, concurrency: int = 1,
                    progress: Optional[Progress] = None) -> Counter:
    """Classify every item with at most ``concurrency`` in flight, writing
    each result to ``sink.write`` as it finishes. Returns the status counts."""
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY}")
    statuses: Counter = Counter()
    pending = iter(work)

    async def worker() -> None:
        for item in pending:          # shared iterator: no await between next() calls
            result = await classify(client, item)
            sink.write(result)
            statuses[result["status"]] += 1
            if progress is not None:
                progress.add(result)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return statuses


def criteria_sets(rulesets: Sequence[str], evaluators: Sequence[str]) -> List[CriteriaSet]:
    """The ``--ruleset`` / ``--evaluator`` pairs (one ruleset is reused for
    every evaluator). Two pairs with the same fingerprint are one set."""
    rulesets = list(rulesets) or [DEFAULT_RULESET]
    evaluators = list(evaluators) or [DEFAULT_EVALUATOR]
    if len(rulesets) == 1 and len(evaluators) > 1:
        rulesets = rulesets * len(evaluators)
    if len(rulesets) != len(evaluators):
        raise ValueError("give one --evaluator per --ruleset (or one --ruleset for all of them)")
    out: List[CriteriaSet] = []
    seen = set()
    for path, evaluator in zip(rulesets, evaluators):
        loaded = load_criteria_set(path, evaluator)
        if loaded.fingerprint not in seen:
            seen.add(loaded.fingerprint)
            out.append(loaded)
    return out


def trimmed_too_far(points: Sequence[Dict[str, Any]], criteria: CriteriaSet) -> int:
    """Points whose kept history is shorter than the criteria set renders
    (``points.py --keep-exchanges`` cut more than it may)."""
    return sum(1 for p in points if (p.get("trimmed_exchanges") or 0) > 0
               and (p.get("k") or 0) < criteria.exchanges)


async def execute(args: argparse.Namespace, client_factory: Callable[[argparse.Namespace], Any]) -> int:
    sets = criteria_sets(args.ruleset, args.evaluator)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    points, _ = read_jsonl(args.points)
    if args.only_points:
        wanted = {r.get("point_id") for r in read_jsonl(args.only_points, skip_bad=True)[0]}
        points = [p for p in points if p.get("point_id") in wanted]
    if args.limit:
        points = points[: args.limit]
    done: Set[ResultKey] = set()
    try:
        previous, unreadable = read_jsonl(args.results, skip_bad=True)
        done = finished_keys(previous, retry_failed=args.retry_failed)
        if unreadable:
            warn(f"run: {unreadable} unreadable line(s) in {args.results} (an interrupted write); redone")
    except FileNotFoundError:
        pass
    for criteria in sets:
        cut = trimmed_too_far(points, criteria)
        if cut:
            warn(f"run: WARNING {cut} point(s) keep fewer exchanges than {criteria.id} renders "
                 f"({criteria.exchanges}); their transcripts differ from production -- rebuild the points "
                 f"with a larger --keep-exchanges")
    work = plan(points, models, sets, done)
    warn(f"run: {len(points)} points x {len(models)} model(s) x {len(sets)} criteria set(s) "
         f"[{', '.join(c.id for c in sets)}]: {len(work)} to classify, "
         f"{len(points) * len(models) * len(sets) - len(work)} already in {args.results}")
    if not work:
        return 0
    client = client_factory(args)
    progress = Progress(len(work), len(models) * len(sets), args.progress_every)
    try:
        with Appender(args.results) as sink:
            statuses = await run_bench(work, client, sink, concurrency=args.concurrency, progress=progress)
    finally:
        await client.aclose()
    progress.report()
    failed = sum(n for status, n in statuses.items() if status not in SCORED_STATUSES)
    warn(f"run: finished; {failed} without scores" + (" -- rerun with --retry-failed to try them again"
                                                       if failed else ""))
    return 0


def make_client(args: argparse.Namespace) -> ClassifierClient:
    return ClassifierClient(args.classifier_url, timeout_s=args.timeout_s, poll_s=args.poll_s)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bench.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--points", required=True, help="points.jsonl (or a sample of it)")
    p.add_argument("--results", required=True, help="results.jsonl to append to (resumed when it exists)")
    p.add_argument("--models", default=DEFAULT_MODELS, help=f"classifier registry ids (default {DEFAULT_MODELS})")
    p.add_argument("--ruleset", action="append", default=[],
                   help=f"ruleset file holding a criteria set (repeatable; default {DEFAULT_RULESET})")
    p.add_argument("--evaluator", action="append", default=[],
                   help=f"its llm_classifier evaluator id (repeatable; default {DEFAULT_EVALUATOR})")
    p.add_argument("--classifier-url", default=DEFAULT_CLASSIFIER_URL,
                   help=f"the document classifier (default {DEFAULT_CLASSIFIER_URL})")
    p.add_argument("--concurrency", type=int, default=1, help=f"classifications in flight, 1..{MAX_CONCURRENCY}")
    p.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, help="one /assess cycle's limit")
    p.add_argument("--poll-s", type=float, default=DEFAULT_POLL_S, help="job poll interval")
    p.add_argument("--only-points", help="only the point_ids in this JSONL (e.g. labels.jsonl or sample.jsonl)")
    p.add_argument("--limit", type=int, default=0, help="only the first N points (0 = all)")
    p.add_argument("--retry-failed", action="store_true", help="classify again what produced no scores")
    p.add_argument("--progress-every", type=int, default=10, help="a progress line about every N points")
    return p


def main(argv: Optional[List[str]] = None,
         client_factory: Callable[[argparse.Namespace], Any] = make_client) -> int:
    args = parser().parse_args(argv)
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        parser().error(f"--concurrency must be 1..{MAX_CONCURRENCY}: more would push a chat model into "
                       f"'busy' and spill live chat turns (BENCH.md)")
    if args.timeout_s <= 0 or args.poll_s <= 0 or args.limit < 0:
        parser().error("--timeout-s and --poll-s must be positive, --limit >= 0")
    for path in (args.points, args.only_points):
        if path and not os.path.isfile(path):
            parser().error(f"{path}: no such file")
    try:
        return asyncio.run(execute(args, client_factory))
    except KeyboardInterrupt:
        warn("run: interrupted -- every finished result is saved; rerun the same command to resume")
        return 130


if __name__ == "__main__":
    sys.exit(main())
