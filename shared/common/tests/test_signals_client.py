"""common.signals.SignalsClient — one POST /signals inside a budget, never
an exception, and no text anywhere but the request body.

The sidecar is faked with ``httpx.MockTransport`` and DNS with a resolver
function, so nothing touches the network. Every outcome status is driven
here; a sentinel string in the request text is checked against every field
of every outcome, including when the fake sidecar echoes the request back
in an error body, in a malformed answer, or in an exception message.

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_signals_client.py -q
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from common.signals import (
    OUTCOME_STATUSES,
    Example,
    ExampleSet,
    PiiFeatures,
    SignalsClient,
    SignalsOutcome,
    SignalsRequest,
    TaskRequest,
    TaskResult,
    TextItem,
)

SENTINEL = "ZZ-SENTINEL-8015-ZZ"
URL = "http://signals:8000"
LOCAL = ["172.18.0.5"]
EXAMPLES = ExampleSet.build([Example("code-001", "local-code", "my script throws an error"),
                             Example("gen-001", "local-general", "what is net metering")],
                            model="intfloat/multilingual-e5-small")


def request(**kwargs) -> SignalsRequest:
    return SignalsRequest(
        (TextItem("u2", f"{SENTINEL} call me at 801-555-0147"), TextItem("u1", "hello")),
        (TaskRequest("pii", ("u2", "u1")), TaskRequest("nearest", ("u2",), k=5, examples=EXAMPLES),
         TaskRequest("language", ("u2",))),
        request_id="dec-1", deadline_ms=90, **kwargs)


def answer(body: dict) -> dict:
    """What a well-behaved sidecar answers to ``body``."""
    pii = {tid: {"entities": {"phone": {"count": 1, "max_score": 0.99}}, "complete": True, "cached": False}
           for tid in body["tasks"][0]["texts"]} if body["tasks"] and body["tasks"][0]["task"] == "pii" else {}
    return {"request_id": body.get("request_id"), "latency_ms": 12.5,
            "models": {"pii": "gravitee-io/bert-small-pii-detection@abc", "embed": "intfloat/multilingual-e5-small@def",
                       "language": "fasttext/lid.176@0123"},
            "results": {"pii": {"status": "ok", "ms": 3.0, "texts": pii},
                        "nearest": {"status": "warming", "ms": None, "texts": {}},
                        "language": {"status": "ok", "ms": 0.1, "texts": {"u2": {"language": "en", "confidence": 0.97}}}}}


class FakeSidecar:
    """A MockTransport handler that records what it was sent."""

    def __init__(self, respond=None) -> None:
        self.requests: list = []
        self.respond = respond or (lambda body: httpx.Response(200, json=answer(body)))

    async def __call__(self, http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        self.requests.append((http_request, body))
        result = self.respond(body)
        if asyncio.iscoroutine(result):
            result = await result
        return result


class CountingResolver:
    def __init__(self, addresses=LOCAL, delay_s: float = 0.0, fail: bool = False) -> None:
        self.addresses, self.delay_s, self.fail = addresses, delay_s, fail
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, host: str):
        with self._lock:
            self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise OSError("no such host")
        return list(self.addresses)


def client(sidecar=None, resolver=None, url=URL, **kwargs) -> SignalsClient:
    return SignalsClient(url, transport=httpx.MockTransport(sidecar or FakeSidecar()),
                         resolver=resolver or CountingResolver(), **kwargs)


def fetch(c: SignalsClient, req=None, budget_s: float = 1.0) -> SignalsOutcome:
    async def go():
        async with c:
            return await c.fetch(req if req is not None else request(), budget_s=budget_s)
    return asyncio.run(go())


def no_sentinel(outcome: SignalsOutcome) -> None:
    assert outcome.status in OUTCOME_STATUSES
    assert SENTINEL not in repr(outcome)
    assert outcome.error is None or (SENTINEL not in outcome.error and len(outcome.error) <= 300)


# ── ok ────────────────────────────────────────────────────────────────────────
def test_ok_sends_the_request_and_returns_the_cut_response():
    sidecar = FakeSidecar()
    outcome = fetch(client(sidecar))
    no_sentinel(outcome)
    assert outcome.status == "ok" and outcome.ok and outcome.error is None and outcome.ms >= 0
    (http_request, body), = sidecar.requests
    assert http_request.method == "POST" and str(http_request.url) == f"{URL}/signals"
    assert http_request.headers["content-type"] == "application/json"
    assert body == request().to_json()
    response = outcome.response
    assert set(response.results) == {"pii", "nearest", "language"}
    assert response.result("pii").texts["u2"] == PiiFeatures.from_json(answer(body)["results"]["pii"]["texts"]["u2"])
    assert response.result("nearest") == TaskResult("warming")
    assert response.model_for("nearest") == "intfloat/multilingual-e5-small@def"
    assert response.request_id == "dec-1"


def test_a_lone_surrogate_is_sent_replaced_not_failed():
    sidecar = FakeSidecar()
    req = SignalsRequest((TextItem("u1", "bad \ud800 text"),), (TaskRequest("pii", ("u1",)),))
    outcome = fetch(client(sidecar), req)
    assert outcome.status == "ok"
    assert sidecar.requests[0][1]["texts"][0]["text"] == "bad ? text"


# ── unavailable ───────────────────────────────────────────────────────────────
def test_no_url_is_unavailable_and_sends_nothing():
    sidecar = FakeSidecar()
    for url in ("", "   "):
        outcome = fetch(client(sidecar, url=url))
        assert (outcome.status, outcome.error) == ("unavailable", "no signals URL configured")
    assert sidecar.requests == []


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_5xx_is_unavailable(status):
    sidecar = FakeSidecar(lambda body: httpx.Response(status, json={"detail": f"echo {body['texts']}"}))
    outcome = fetch(client(sidecar))
    no_sentinel(outcome)
    assert (outcome.status, outcome.error) == ("unavailable", f"POST /signals answered {status}")


def test_a_connection_failure_is_unavailable_and_its_message_is_not_kept():
    def refuse(body):
        raise httpx.ConnectError(f"refused while sending {body['texts'][0]['text']}")

    outcome = fetch(client(FakeSidecar(refuse)))
    no_sentinel(outcome)
    assert (outcome.status, outcome.error) == ("unavailable", "POST /signals: ConnectError")


# ── error ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [400, 404, 413, 422, 307])
def test_a_4xx_or_redirect_is_an_error_without_its_body(status):
    sidecar = FakeSidecar(lambda body: httpx.Response(status, json={"detail": body},
                                                      headers={"location": "http://elsewhere/"}))
    outcome = fetch(client(sidecar))
    no_sentinel(outcome)
    assert (outcome.status, outcome.error) == ("error", f"POST /signals answered {status}")
    assert len(sidecar.requests) == 1  # the redirect was not followed


@pytest.mark.parametrize("respond,error", [
    (lambda body: httpx.Response(200, content=b"<html>" + json.dumps(body).encode() + b"</html>"),
     "POST /signals answered something that is not JSON"),
    (lambda body: httpx.Response(200, json={"results": body}),
     None),
    (lambda body: httpx.Response(200, json=[body]),
     "malformed response: response: must be an object"),
    (lambda body: httpx.Response(200, json={"results": {}, "models": {"pii": body["texts"]}}),
     "malformed response: models value: must be a string"),
])
def test_a_malformed_answer_is_an_error_that_quotes_nothing(respond, error):
    outcome = fetch(client(FakeSidecar(respond)))
    no_sentinel(outcome)
    if error is None:
        # {"results": <the request>}: no known task in it, so every requested
        # task comes back as missing -- still "ok", nothing invented.
        assert outcome.status == "ok"
        assert {r.error for r in outcome.response.results.values()} == {"missing from the response"}
    else:
        assert (outcome.status, outcome.error) == ("error", error)


def test_an_echoing_task_result_is_dropped():
    def echo(body):
        data = answer(body)
        data["results"]["language"] = {"status": "ok", "texts": {"u2": {"language": body["texts"][0]["text"],
                                                                        "confidence": 0.5}}}
        data["results"]["pii"]["error"] = body["texts"][0]["text"]
        return httpx.Response(200, json=data)

    outcome = fetch(client(FakeSidecar(echo)))
    no_sentinel(outcome)
    assert outcome.response.result("language") == TaskResult("error", error="malformed language result")
    assert outcome.response.result("pii").error is None


def test_over_the_caps_is_refused_before_sending():
    sidecar = FakeSidecar()
    req = SignalsRequest((TextItem("u1", SENTINEL + "x" * 20_000),), (TaskRequest("pii", ("u1",)),))
    outcome = fetch(client(sidecar), req)
    no_sentinel(outcome)
    assert outcome.status == "error"
    assert outcome.error.startswith("request over the caps, not sent: text 'u1' has ")
    assert sidecar.requests == []


@pytest.mark.parametrize("url,error", [("ftp://signals:8000", "the signals URL is not http(s)"),
                                       ("http://:8000", "the signals URL has no host")])
def test_a_bad_url_is_an_error(url, error):
    outcome = fetch(client(url=url))
    assert (outcome.status, outcome.error) == ("error", error)


def test_anything_unexpected_is_an_error_not_an_exception():
    def explode(body):
        raise RuntimeError(body["texts"][0]["text"])

    outcome = fetch(client(FakeSidecar(explode)))
    no_sentinel(outcome)
    assert (outcome.status, outcome.error) == ("error", "unexpected RuntimeError")
    wrong = fetch(client(), req={"texts": [], "tasks": []})  # type: ignore[arg-type]
    assert (wrong.status, wrong.error) == ("error", "not a SignalsRequest")


# ── timeout ───────────────────────────────────────────────────────────────────
def test_a_slow_sidecar_is_a_timeout_within_the_budget():
    async def slow(body):
        await asyncio.sleep(2.0)
        return httpx.Response(200, json=answer(body))

    started = time.perf_counter()
    outcome = fetch(client(FakeSidecar(slow)), budget_s=0.05)
    elapsed = time.perf_counter() - started
    no_sentinel(outcome)
    assert (outcome.status, outcome.error) == ("timeout", "no answer within 0.05s")
    assert elapsed < 1.0 and outcome.ms < 1000


def test_an_httpx_timeout_is_a_timeout():
    def read_timeout(body):
        raise httpx.ReadTimeout("timed out")

    assert fetch(client(FakeSidecar(read_timeout))).status == "timeout"


@pytest.mark.parametrize("budget", [0, -1, None])
def test_no_budget_is_a_timeout_without_sending(budget):
    sidecar = FakeSidecar()
    outcome = fetch(client(sidecar), budget_s=budget)
    assert (outcome.status, outcome.error) == ("timeout", "no budget left")
    assert sidecar.requests == []


# ── not_local ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("resolver,error", [
    (CountingResolver(["93.184.216.34"]), "refusing to send text: host 'signals' resolves to a public address"),
    (CountingResolver(["172.18.0.5", "8.8.8.8"]), "refusing to send text: host 'signals' resolves to a public address"),
    (CountingResolver(fail=True), "refusing to send text: host 'signals' could not be resolved (OSError)"),
    (CountingResolver([]), "refusing to send text: host 'signals' resolved to no address"),
])
def test_a_host_outside_the_private_networks_is_refused(resolver, error):
    sidecar = FakeSidecar()
    outcome = fetch(client(sidecar, resolver))
    no_sentinel(outcome)
    assert (outcome.status, outcome.error) == ("not_local", error)
    assert sidecar.requests == []


def test_the_locality_verdict_is_cached_for_the_ttl():
    now = [1000.0]
    resolver = CountingResolver()
    c = client(resolver=resolver, models_ttl_s=60, clock=lambda: now[0])

    async def go():
        async with c:
            first = await c.fetch(request(), budget_s=1.0)
            second = await c.fetch(request(), budget_s=1.0)
            now[0] += 61
            third = await c.fetch(request(), budget_s=1.0)
            return first, second, third

    outcomes = asyncio.run(go())
    assert [o.status for o in outcomes] == ["ok", "ok", "ok"]
    assert resolver.calls == 2


@pytest.mark.parametrize("unresolved", [{"fail": True}, {"addresses": []}])
def test_an_unresolved_host_is_retried_after_a_short_ttl(unresolved):
    # Docker DNS answers only for running containers: a sidecar that restarts
    # (or starts after route-policy) does not resolve for a few seconds. That
    # verdict is kept unresolved_ttl_s (2 s), not models_ttl_s (60 s).
    now = [1000.0]
    resolver = CountingResolver(**unresolved)
    sidecar = FakeSidecar()
    c = client(sidecar, resolver, models_ttl_s=60, clock=lambda: now[0])
    assert c.unresolved_ttl_s == 2.0

    async def go():
        async with c:
            down = await c.fetch(request(), budget_s=1.0)
            now[0] += 1.0
            still = await c.fetch(request(), budget_s=1.0)          # within 2 s: cached, no resolve
            calls = resolver.calls
            resolver.fail, resolver.addresses = False, LOCAL         # the container is back
            now[0] += 1.5
            back = await c.fetch(request(), budget_s=1.0)
            now[0] += 30.0
            later = await c.fetch(request(), budget_s=1.0)          # a resolved verdict: 60 s
            return down, still, calls, back, later

    down, still, calls, back, later = asyncio.run(go())
    assert (down.status, still.status, back.status, later.status) == ("not_local", "not_local", "ok", "ok")
    assert calls == 1 and resolver.calls == 2 and len(sidecar.requests) == 2


def test_a_public_verdict_is_cached_for_the_full_ttl():
    now = [1000.0]
    resolver = CountingResolver(["93.184.216.34"])
    sidecar = FakeSidecar()
    c = client(sidecar, resolver, models_ttl_s=60, unresolved_ttl_s=2, clock=lambda: now[0])

    async def go():
        async with c:
            first = await c.fetch(request(), budget_s=1.0)
            resolver.addresses = LOCAL
            now[0] += 30.0
            second = await c.fetch(request(), budget_s=1.0)         # still the cached public verdict
            now[0] += 31.0
            third = await c.fetch(request(), budget_s=1.0)
            return first, second, third

    first, second, third = asyncio.run(go())
    assert [o.status for o in (first, second, third)] == ["not_local", "not_local", "ok"]
    assert resolver.calls == 2 and len(sidecar.requests) == 1


def test_a_slow_resolve_times_out_but_still_fills_the_cache():
    resolver = CountingResolver(delay_s=0.3)
    sidecar = FakeSidecar()
    c = client(sidecar, resolver)

    async def go():
        async with c:
            first = await c.fetch(request(), budget_s=0.05)
            await asyncio.sleep(0.5)  # the shielded resolve finishes meanwhile
            second = await c.fetch(request(), budget_s=0.05)
            return first, second

    first, second = asyncio.run(go())
    assert first.status == "timeout" and second.status == "ok"
    assert resolver.calls == 1 and len(sidecar.requests) == 1


def test_concurrent_calls_share_one_resolve():
    resolver = CountingResolver(delay_s=0.05)
    sidecar = FakeSidecar()
    c = client(sidecar, resolver)

    async def go():
        async with c:
            return await asyncio.gather(*(c.fetch(request(), budget_s=2.0) for _ in range(5)))

    outcomes = asyncio.run(go())
    assert [o.status for o in outcomes] == ["ok"] * 5
    assert resolver.calls == 1 and len(sidecar.requests) == 5


# ── warm ──────────────────────────────────────────────────────────────────────
def test_warm_sends_the_example_set_with_no_texts():
    sidecar = FakeSidecar(lambda body: httpx.Response(200, json={
        "results": {"nearest": {"status": "warming", "texts": {}}}, "models": {}}))
    c = client(sidecar)

    async def go():
        async with c:
            return await c.warm(EXAMPLES, budget_s=1.0, max_tokens=256)

    outcome = asyncio.run(go())
    assert outcome.status == "ok" and outcome.response.result("nearest").status == "warming"
    (_, body), = sidecar.requests
    assert body == {"texts": [], "tasks": [{"task": "nearest", "texts": [], "max_tokens": 256,
                                             "examples": EXAMPLES.to_json()}]}


def test_warm_with_a_bad_param_is_an_error_not_an_exception():
    async def go():
        async with client() as c:
            return await c.warm(EXAMPLES, budget_s=1.0, max_tokens=10_000)

    outcome = asyncio.run(go())
    assert outcome.status == "error" and "max_tokens" in outcome.error


def test_aclose_is_idempotent_and_the_client_reopens():
    sidecar = FakeSidecar()
    c = client(sidecar)

    async def go():
        await c.aclose()                       # before any call: nothing to close
        first = await c.fetch(request(), budget_s=1.0)
        await c.aclose()
        await c.aclose()
        second = await c.fetch(request(), budget_s=1.0)
        await c.aclose()
        return first, second

    assert [o.status for o in asyncio.run(go())] == ["ok", "ok"]
