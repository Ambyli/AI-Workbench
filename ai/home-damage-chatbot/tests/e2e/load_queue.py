"""Queue and load test against the RUNNING mock's chatbot API (release test plan 5.6).

15 simulated customers arrive at once. Each has its own client address (sent as
X-Forwarded-For from localhost, which the API trusts exactly like the production
proxy), so the per-client rate limit and session cap apply per customer, as in real
traffic. Expected with MAX_ACTIVE_USERS=3 and MAX_QUEUE_SIZE=10:
  * 3 chat immediately, 10 wait with a position, 2 are told the line is full
  * positions only ever count down; every waiting customer is eventually served
  * every served customer completes a full request on the real model
Reports per-turn latency under load.

    python tests/e2e/load_queue.py [--customers 15]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PIDS = json.loads((ROOT / "mock" / ".pids.json").read_text(encoding="utf-8-sig"))
API = f"http://127.0.0.1:{PIDS['apiPort']}"
FLOW = ["Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Electrical", "6", "skip",
        "Yes", "No", "No", "No", "Kitchen", "The kitchen outlets stopped working after the storm.",
        "skip", "yes"]


async def customer(i: int, latencies: list[float], log: list[dict]) -> dict:
    ip = f"198.51.100.{i + 1}"
    rec = {"id": i, "first": None, "positions": [], "served": False, "done": False, "error": None}
    async with httpx.AsyncClient(base_url=API, timeout=40, headers={"X-Forwarded-For": ip}) as c:
        r = await c.post("/api/chat", json={"message": ""})
        j = r.json()
        rec["first"] = j.get("state")
        if j.get("state") == "queue_full":
            log.append(rec)
            return rec
        sid = j["session_id"]
        if j.get("state") == "queued":
            rec["positions"].append(j.get("queue_position"))
        while j.get("state") == "queued":
            await asyncio.sleep(3)  # the page polls every 3 s
            s = (await c.post("/api/queue/status", json={"session_id": sid})).json()
            if s.get("state") == "queued":
                rec["positions"].append(s.get("queue_position"))
                continue
            j = (await c.post("/api/chat", json={"session_id": sid, "message": ""})).json()
            if j.get("state") == "queued":  # lost the slot race: still waiting
                rec["positions"].append(j.get("queue_position"))
        rec["served"] = True
        for a in FLOW:
            t0 = time.perf_counter()
            r = await c.post("/api/chat", json={"session_id": sid, "message": a})
            latencies.append(time.perf_counter() - t0)
            if r.status_code != 200:
                rec["error"] = f"{a!r}: HTTP {r.status_code} {r.text[:80]}"
                break
            j = r.json()
        rec["done"] = j.get("outcome") == "submitted"
    log.append(rec)
    return rec


async def run(n: int) -> int:
    latencies: list[float] = []
    log: list[dict] = []
    t0 = time.perf_counter()
    recs = await asyncio.gather(*(customer(i, latencies, log) for i in range(n)))
    wall = time.perf_counter() - t0
    first = [r["first"] for r in recs]
    immediate, queued, full = first.count("collect"), first.count("queued"), first.count("queue_full")
    monotonic = all(all(a >= b for a, b in zip(r["positions"], r["positions"][1:])) for r in recs)
    served = [r for r in recs if r["served"]]
    completed = [r for r in served if r["done"]]
    errors = [r["error"] for r in recs if r["error"]]
    lat = sorted(latencies)
    p95 = lat[max(0, int(round(0.95 * len(lat))) - 1)] if lat else 0.0
    print(f"customers={n}  immediate={immediate}  queued={queued}  queue_full={full}  wall={wall:.0f}s")
    print(f"served={len(served)}  completed={len(completed)}  positions only count down={monotonic}")
    print(f"turn latency under load: p50={statistics.median(lat):.2f}s  p95={p95:.2f}s  max={lat[-1]:.2f}s  ({len(lat)} turns)")
    for e in errors:
        print("  error:", e)
    ok = (immediate == 3 and queued == min(10, n - 3) and full == max(0, n - 13) and monotonic
          and len(completed) == len(served) == immediate + queued and not errors and p95 <= 8.0)
    print(f"\nload_queue: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--customers", type=int, default=15)
    sys.exit(asyncio.run(run(ap.parse_args().customers)))
