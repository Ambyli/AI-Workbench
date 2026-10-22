"""``python -m signals.bench``: what the sidecar's models cost on THIS CPU.

The plan's box bench (ROUTE_POLICY_PHASE5_PLAN § 7.3), productised: the same
measurements, on the pinned models baked into the image, through the
sidecar's own code. Synthetic text only -- no customer data, nothing read
from a request.

    make exec signals signals CMD='python -m signals.bench'
    python -m signals.bench --threads 1,2,4 --n 30

It prints the CPU (model name, the SIMD flags onnxruntime cares about, the
cores this process may run on -- SIGNALS_CPUSET), then for each thread
count:

* ``model``: each ONNX model alone, p50 / p95 ms per text of 20, 200 and
  2,000 characters (PII scans every window; injection head + tail; embed
  the first 256 tokens) -- fastText once, it has no threads;
* ``call``: one /signals request through ``Engine.run`` with all four tasks
  on a cold cache, the three models running concurrently as in service:
  server p50 / p95 ms with no deadline, then how often each task is ``ok``
  inside an 80 ms deadline (what route-policy sends: its 0.1 s budget
  minus a 20 ms margin).

It loads a second copy of the models (~1.8 GB) and uses the same cores as
the running service: run it when the box is quiet, or the numbers measure
the contention, not the CPU. 4 threads per model on 8 pinned cores
oversubscribes them -- that is the point of measuring it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import re
import statistics
import sys
import time
from typing import Callable, Dict, List, Sequence, Tuple

from common.signals import Example, ExampleSet, TaskRequest

from . import config
from .cache import ExampleSets, FeatureCache
from .runtime import Models, load_models, load_specs, model_loader
from .scheduler import Engine

SENTENCE = ("I am planning to put solar panels on the roof next spring and want to understand how the "
            "permit process works, what the inspection involves and how long the utility takes. ")
EXAMPLES = (
    ("code-1", "local-code", "my script throws an error when I run it"),
    ("code-2", "local-code", "write a python function that parses this csv file"),
    ("code-3", "local-code", "why does this sql query return duplicate rows"),
    ("code-4", "local-code", "refactor this javascript to use async await"),
    ("reason-1", "local-reasoning", "work out step by step how many panels fit on a 40 by 20 foot roof"),
    ("reason-2", "local-reasoning", "compare the payback period of these two financing options"),
    ("reason-3", "local-reasoning", "prove that the sum of two even numbers is even"),
    ("reason-4", "local-reasoning", "estimate the yearly output of a 7 kW system in Utah"),
    ("general-1", "local-general", "write a friendly reminder email about tomorrow's appointment"),
    ("general-2", "local-general", "what does net metering mean"),
    ("general-3", "local-general", "summarise this paragraph in two sentences"),
    ("general-4", "local-general", "suggest a title for a blog post about clean energy"),
)
SIZES = (20, 200, 2000)

# The deadline route-policy sends (its ROUTE_POLICY_SIGNALS_BUDGET_S 0.1 s
# minus SIGNALS_DEADLINE_MARGIN_S 20 ms).
DEADLINE_MS = 80
FLAGS = ("avx2", "fma", "avx512f", "avx512_vnni", "avx_vnni", "amx_int8")


def synthetic(chars: int) -> str:
    return (SENTENCE * (chars // len(SENTENCE) + 1))[:chars]


def cpu() -> Tuple[str, List[str], str]:
    name, flags = platform.processor() or "unknown", []
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            info = fh.read()
        found = re.search(r"^model name\s*:\s*(.+)$", info, re.M)
        name = found.group(1).strip() if found else name
        flags = [f for f in FLAGS if re.search(rf"\b{f}\b", info)]
    except OSError:
        pass
    try:
        cores = sorted(os.sched_getaffinity(0))
        allowed = f"{len(cores)} ({_ranges(cores)})"
    except AttributeError:  # not Linux
        allowed = str(os.cpu_count())
    return name, flags, allowed


def _ranges(cores: Sequence[int]) -> str:
    out, start, prev = [], None, None
    for c in cores:
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = c
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


def drain(scan) -> object:
    while True:
        try:
            next(scan)
        except StopIteration as stop:
            return stop.value


def measure(fn: Callable[[], object], n: int) -> Tuple[float, float]:
    for _ in range(3):
        fn()
    samples = []
    for _ in range(n):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    return statistics.median(samples), samples[min(len(samples) - 1, int(0.95 * len(samples)))]


def load(threads: int) -> Models:
    models = Models(load_specs())
    load_models(models, model_loader(config.MODELS_DIR, {lane: threads for lane in config.ONNX_LANES}))
    if not models.ready():
        raise SystemExit(f"models failed to load: {models.snapshot()['errors']}")
    return models


def bench_models(models: Models, threads: int, n: int) -> None:
    pii, embed, injection = models.get("pii"), models.get("embed"), models.get("injection")
    runs = {
        "pii": lambda t: drain(pii.scan(t)),
        "injection": lambda t: drain(injection.scan(t, 256)),
        "embed": lambda t: drain(embed.scan(t, 256)),
    }
    for lane, run in runs.items():
        row = []
        for size in SIZES:
            text = synthetic(size)
            p50, p95 = measure(lambda run=run, text=text: run(text), n)
            row.append(f"{size:>5} ch {p50:7.1f} / {p95:7.1f}")
        print(f"model  {lane:9} t={threads}  " + "   ".join(row) + "   (p50 / p95 ms)", flush=True)


async def bench_call(models: Models, threads: int, n: int) -> None:
    cache = FeatureCache(config.CACHE_ENTRIES)
    engine = Engine(models, cache, ExampleSets(config.EXAMPLE_SETS_MAX))
    engine.start()
    try:
        examples = ExampleSet.build((Example(*e) for e in EXAMPLES), model=models.model_id("embed"))
        warm = [TaskRequest("nearest", examples=examples)]
        while (await engine.run({}, warm, 10_000))["nearest"].status == "warming":
            await asyncio.sleep(0.05)
        for size in SIZES:
            texts = {"u1": synthetic(size)}
            tasks = [TaskRequest("pii", ("u1",)),
                     TaskRequest("nearest", ("u1",), max_tokens=256, k=5, examples=examples),
                     TaskRequest("injection", ("u1",), max_tokens=256, windows="head_tail"),
                     TaskRequest("language", ("u1",))]
            walls = []
            for _ in range(n):
                cache.clear()
                started = time.perf_counter()
                await engine.run(texts, tasks, 10_000)
                walls.append((time.perf_counter() - started) * 1000.0)
            walls.sort()
            in_time: Dict[str, int] = {t.task: 0 for t in tasks}
            for _ in range(n):
                cache.clear()
                for task, outcome in (await engine.run(texts, tasks, DEADLINE_MS)).items():
                    in_time[task] += outcome.status == "ok"
                await asyncio.sleep(0.5)  # let a late PII job finish: each run starts cold and alone
            p95 = walls[min(n - 1, int(0.95 * n))]
            ok = "  ".join(f"{task} {100 * c // n}%" for task, c in in_time.items())
            print(f"call   all four  t={threads}  {size:>5} ch  {statistics.median(walls):7.1f} / {p95:7.1f} ms"
                  f"   ok within {DEADLINE_MS} ms: {ok}", flush=True)
    finally:
        engine.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m signals.bench", description=__doc__.splitlines()[0])
    parser.add_argument("--threads", default=str(config.THREADS["pii"]),
                        help="comma-separated intra-op thread counts per model (default: SIGNALS_THREADS_PII)")
    parser.add_argument("--n", type=int, default=30, help="samples per measurement")
    args = parser.parse_args(argv)
    import onnxruntime

    name, flags, allowed = cpu()
    print(f"cpu: {name}  flags: {' '.join(flags) or 'none'}  cores allowed: {allowed}  "
          f"onnxruntime {onnxruntime.__version__}", flush=True)
    models = load(1)
    language = models.get("language")
    row = []
    for size in SIZES:
        text = synthetic(size)
        p50, p95 = measure(lambda text=text: language.detect(text), args.n)
        row.append(f"{size:>5} ch {p50:7.2f} / {p95:7.2f}")
    print("model  language  inline " + "   ".join(row) + "   (p50 / p95 ms)", flush=True)
    for threads in [int(t) for t in args.threads.split(",") if t.strip()]:
        models = load(threads)
        bench_models(models, threads, args.n)
        asyncio.run(bench_call(models, threads, max(5, args.n // 3)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
