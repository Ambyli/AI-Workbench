"""What the hooks use of a vLLM server's HTTP surface: the server-root URLs
(/metrics, /tokenize -- outside /v1 and its API-key guard), the Prometheus
queue metrics, and the two httpx clients that call them."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

WAITING_METRIC = "vllm:num_requests_waiting"
RUNNING_METRIC = "vllm:num_requests_running"

# GET <url> -> the page text (raise on anything but a 200).
Fetch = Callable[[str], Awaitable[str]]
# POST <url> <json body> -> the JSON object (raise on anything but a 200).
Tokenize = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class ProbeError(Exception):
    pass


def server_url(api_base: str, path: str) -> str:
    """http://qwen3.8-solo:8000/v1 + /tokenize -> http://qwen3.8-solo:8000/tokenize

    vLLM serves /metrics and /tokenize at the server root, outside /v1 (and
    outside its /v1 API-key guard)."""
    base = api_base.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/" + path.lstrip("/")


def metrics_url(api_base: str) -> str:
    """http://qwen3.8-solo:8000/v1 -> http://qwen3.8-solo:8000/metrics"""
    return server_url(api_base, "/metrics")


def tokenize_url(api_base: str) -> str:
    """http://qwen3.8-solo:8000/v1 -> http://qwen3.8-solo:8000/tokenize"""
    return server_url(api_base, "/tokenize")


def parse_queue_metrics(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Sum vllm:num_requests_waiting / _running over every label set.

    Returns (waiting, running); a metric that never appears is None, so the
    caller can tell "zero" from "this is not the vLLM I expected".
    """
    sums: Dict[str, Optional[float]] = {WAITING_METRIC: None, RUNNING_METRIC: None}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        for name in sums:
            if not line.startswith(name):
                continue
            rest = line[len(name):]
            if rest[:1] not in ("{", " ", "\t"):
                continue  # a longer metric name sharing the prefix
            if rest.startswith("{"):
                close = rest.find("}")
                if close == -1:
                    continue
                rest = rest[close + 1:]
            parts = rest.split()
            if not parts:
                continue
            try:
                value = float(parts[0])
            except ValueError:
                continue
            if value != value:  # NaN
                continue
            sums[name] = (sums[name] or 0.0) + value
    return sums[WAITING_METRIC], sums[RUNNING_METRIC]


class HttpxFetch:
    """GET a /metrics page; raise on anything that is not a 200."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def __call__(self, url: str) -> str:
        import httpx  # LiteLLM depends on it; imported lazily for the tests

        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._loop = loop
        resp = await self._client.get(url)
        if resp.status_code != 200:
            raise ProbeError(f"HTTP {resp.status_code}")
        return resp.text


class HttpxTokenize:
    """POST a vLLM /tokenize request; return the JSON object or raise.

    The caller bounds the whole call with asyncio.wait_for -- httpx's own
    timeout is per read. No Authorization header: /tokenize sits outside
    vLLM's /v1 API-key guard. The response carries every token id (for a
    120k-token prompt, ~1 MB of JSON); there is no flag to omit them, which
    is one more reason it is only asked when the estimate says it matters."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def __call__(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        import httpx  # LiteLLM depends on it; imported lazily for the tests

        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._loop = loop
        resp = await self._client.post(url, json=body)
        if resp.status_code != 200:
            raise ProbeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise ProbeError("response is not a JSON object")
        return data
