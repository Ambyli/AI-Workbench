"""Tests for common.net.fetch_url — the SSRF-checked, bounded fetch.

Like test_net_ssrf.py, these lean on the refusal paths: a blocked host is
refused BEFORE any request is made, a redirect is an error rather than a
second request, an oversized body is abandoned, and a slow server trips the
deadline. DNS is stubbed and the HTTP side is ``httpx.MockTransport``, so
nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from common.net import BlockedURLError, FetchError, fetch_url


def _stub_dns(monkeypatch, mapping: dict[str, list[str]]):
    """Make getaddrinfo answer from ``mapping`` and raise for anything else."""

    def fake(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))
            for addr in mapping[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def public_dns(monkeypatch):
    _stub_dns(monkeypatch, {"example.com": ["93.184.216.34"], "evil.test": ["10.0.0.5"]})


def test_happy_path_returns_the_body_and_sends_headers(public_dns) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"PNGDATA")

    body = _run(fetch_url(
        "https://example.com/a.png",
        timeout=5,
        headers={"User-Agent": "Test/1.0"},
        transport=httpx.MockTransport(handler),
    ))
    assert body == b"PNGDATA"
    assert seen[0].headers["user-agent"] == "Test/1.0"


def test_a_blocked_host_is_refused_before_any_request(public_dns) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"x")

    with pytest.raises(BlockedURLError, match="blocked network"):
        _run(fetch_url("http://evil.test/x", timeout=5, transport=httpx.MockTransport(handler)))
    assert calls == []


def test_a_non_http_scheme_is_refused(public_dns) -> None:
    with pytest.raises(BlockedURLError):
        _run(fetch_url("file:///etc/passwd", timeout=5))


def test_a_redirect_is_not_followed(public_dns) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    with pytest.raises(FetchError) as exc:
        _run(fetch_url("https://example.com/a.png", timeout=5,
                       transport=httpx.MockTransport(handler)))
    assert exc.value.reason == "redirect not followed"
    assert "169.254.169.254" in str(exc.value)
    assert calls == ["https://example.com/a.png"]  # one request, never the second


def test_an_http_error_status_is_a_fetch_error(public_dns) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    with pytest.raises(FetchError) as exc:
        _run(fetch_url("https://example.com/missing.png", timeout=5, transport=transport))
    assert exc.value.reason == "HTTP 404"


def test_a_body_over_max_bytes_is_abandoned(public_dns) -> None:
    sent: list[int] = []

    async def stream():
        for _ in range(100):
            sent.append(1)
            yield b"x" * 1000

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=stream()))
    with pytest.raises(FetchError) as exc:
        _run(fetch_url("https://example.com/big", timeout=5, max_bytes=5000,
                       transport=transport))
    assert exc.value.reason == "larger than 5000 bytes"
    assert len(sent) < 100  # stopped reading, did not drain the body


def test_a_declared_content_length_over_max_bytes_is_refused_up_front(public_dns) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Length": "999999"}, content=b"")
    )
    with pytest.raises(FetchError, match="declares 999999 bytes"):
        _run(fetch_url("https://example.com/big", timeout=5, max_bytes=1000,
                       transport=transport))


def test_a_body_at_max_bytes_is_accepted(public_dns) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1000))
    assert len(_run(fetch_url("https://example.com/ok", timeout=5, max_bytes=1000,
                              transport=transport))) == 1000


def test_an_httpx_timeout_is_a_fetch_error(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(FetchError) as exc:
        _run(fetch_url("https://example.com/slow", timeout=5,
                       transport=httpx.MockTransport(handler)))
    assert exc.value.reason == "timed out"


def test_the_deadline_bounds_the_whole_fetch(public_dns) -> None:
    async def drip():
        for _ in range(50):
            await asyncio.sleep(0.05)
            yield b"x"

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=drip()))
    with pytest.raises(FetchError) as exc:
        _run(fetch_url("https://example.com/drip", timeout=5, deadline=0.2,
                       transport=transport))
    assert exc.value.reason == "timed out"


def test_a_transport_failure_is_a_fetch_error(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(FetchError) as exc:
        _run(fetch_url("https://example.com/x", timeout=5,
                       transport=httpx.MockTransport(handler)))
    assert exc.value.reason == "fetch failed"
