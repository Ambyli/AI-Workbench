"""Example sets for the nearest task (§ 3.2 "Example sets"): the first request
naming an unknown fingerprint starts embedding it in the background and
answers ``warming``; later ones hit the embedded set; at most 8 are held;
the same fingerprint with other items is embedded again; a warming hands
the embed model to a waiting request between two batches."""

from __future__ import annotations

import asyncio
import time

from common.signals import Example, SignalsRequest, SignalsResponse
from sig_helpers import (EXAMPLES, FakeEmbed, Recorder, app_client, examples_json, fakes, features, nearest_task, run,
                         running_engine, wait_until)

from signals import metrics


def _ready(engine) -> int:
    return engine.sets.states()["ready"]


def test_the_first_request_warms_and_later_ones_vote():
    rec = Recorder()
    embed = FakeEmbed(rec, batch_s=0.02)
    with running_engine(fakes(rec, embed=embed)) as engine:
        first = run(engine, {"u1": "please fix my code"}, [nearest_task()])
        assert first["nearest"].status == "warming" and first["nearest"].texts == {} and first["nearest"].ms is None
        assert wait_until(lambda: _ready(engine) == 1)
        second = run(engine, {"u1": "please fix my code"}, [nearest_task()])
    found = features(second["nearest"])["u1"]
    assert second["nearest"].status == "ok"
    assert [nb["id"] for nb in found["neighbours"]] == ["code-1", "code-2", "code-3"]
    assert found["routes"] == {"local-code": {"share": 1.0, "best": 1.0, "count": 3}}
    assert embed.sets_embedded == 1


def test_the_startup_warm_up_carries_no_text():
    rec = Recorder()
    with running_engine(fakes(rec, embed=FakeEmbed(rec, batch_s=0.02))) as engine:
        assert run(engine, {}, [nearest_task(())])["nearest"].status == "warming"
        assert wait_until(lambda: _ready(engine) == 1)
        out = run(engine, {}, [nearest_task(())])
    assert out["nearest"].status == "ok" and out["nearest"].texts == {}


def test_one_fingerprint_with_other_items_is_embedded_again():
    rec = Recorder()
    embed = FakeEmbed(rec, batch_s=0.02)
    other = EXAMPLES[:3] + (Example("general-9", "local-general", "sing a song"),)
    with running_engine(fakes(rec, embed=embed)) as engine:
        run(engine, {}, [nearest_task(())])
        assert wait_until(lambda: _ready(engine) == 1)
        changed = run(engine, {"u1": "sing"}, [nearest_task(("u1",), examples=other)])
        assert changed["nearest"].status == "warming"
        assert wait_until(lambda: embed.sets_embedded == 2 and _ready(engine) == 1)
        out = run(engine, {"u1": "sing"}, [nearest_task(("u1",), examples=other)])
    ids = [nb["id"] for nb in features(out["nearest"])["u1"]["neighbours"]]
    assert set(ids) <= {e.id for e in other}                        # answered from the new set, not the old one


def test_at_most_eight_sets_are_held():
    rec = Recorder()
    with running_engine(fakes(rec, embed=FakeEmbed(rec, batch_s=0.01))) as engine:
        for i in range(10):
            run(engine, {}, [nearest_task((), fingerprint=f"fp-{i}")])
            assert wait_until(lambda: engine.sets.states()["warming"] == 0)
        assert len(engine.sets) == 8
        # The oldest were evicted: naming one again warms it again.
        assert run(engine, {}, [nearest_task((), fingerprint="fp-0")])["nearest"].status == "warming"


def test_a_failed_warming_is_retried_by_the_next_request():
    failed = metrics.warmings_total.labels("failed")
    before = failed._value.get()
    rec = Recorder()
    with running_engine(fakes(rec, embed=FakeEmbed(rec, fail_sets=True))) as engine:
        run(engine, {}, [nearest_task(())])
        assert wait_until(lambda: failed._value.get() == before + 1)
        assert len(engine.sets) == 0
        assert run(engine, {}, [nearest_task(())])["nearest"].status == "warming"
        assert wait_until(lambda: failed._value.get() == before + 2)


def test_a_warming_hands_the_model_to_a_waiting_request_between_batches():
    rec = Recorder()
    big = tuple(Example(f"g{i}", "local-general", f"example number {i}") for i in range(48))   # 3 batches
    embed = FakeEmbed(rec, batch_s=0.1)
    with running_engine(fakes(rec, embed=embed)) as engine:
        run(engine, {}, [nearest_task((), fingerprint="small", examples=EXAMPLES[:2])])
        assert wait_until(lambda: _ready(engine) == 1)

        async def go():
            run_warm = await engine.run({}, [nearest_task((), fingerprint="big", examples=big)], 1000)
            await asyncio.sleep(0.02)                                # batch 1 of 3 is running
            started = time.perf_counter()
            out = await engine.run({"u1": "fix my code"},
                                   [nearest_task(("u1",), fingerprint="small", examples=EXAMPLES[:2])], 2000)
            return run_warm, out, time.perf_counter() - started

        warm, out, waited = asyncio.run(go())
        still_warming = engine.sets.states()["warming"]
        assert wait_until(lambda: _ready(engine) == 2, timeout=3.0)
    assert warm["nearest"].status == "warming"
    assert out["nearest"].status == "ok"
    assert waited < 0.18                                             # after batch 1, not after all 3 (0.3 s)
    assert still_warming == 1


def test_warming_over_http_then_a_vote_the_wire_accepts():
    request = {"deadline_ms": 1000, "texts": [{"id": "u1", "text": "reason about it"}],
               "tasks": [{"task": "nearest", "texts": ["u1"], "k": 2, "examples": examples_json()}]}
    rec = Recorder()
    with app_client(fakes(rec, embed=FakeEmbed(rec, batch_s=0.05))) as client:
        post = lambda: client.post("/signals", json=request).json()  # noqa: E731
        assert post()["results"]["nearest"]["status"] == "warming"
        assert wait_until(lambda: post()["results"]["nearest"]["status"] == "ok")
        body = post()
    parsed = SignalsResponse.from_json(body)
    assert parsed.for_request(SignalsRequest.from_json(request)).result("nearest").status == "ok"
    routes = body["results"]["nearest"]["texts"]["u1"]["routes"]
    assert list(routes) == ["local-reasoning"] and routes["local-reasoning"]["count"] == 2
