"""Fetch a caller-supplied URL — SSRF-checked, bounded, no redirects.

Every service that grows a "give me a URL and I'll fetch it" input ends up
writing the same dozen lines: check the address, open an httpx client, GET,
``raise_for_status``, read the body. The classifier had them for document
URLs, the detector has a near-identical copy for image URLs, and the
classifier's SVG image inliner needed a third. :func:`fetch_url` is that one
copy, with the limits a fetch on a caller's behalf should always have:

  * :func:`common.net.validate_url` runs FIRST, so nothing is requested from
    an address inside the blocklist.
  * Redirects are NOT followed. A 3xx is a :class:`FetchError`: following one
    would re-point the request at an address the SSRF check never saw (a
    public URL that 302s to ``http://169.254.169.254/``).
  * The body is streamed and abandoned the moment it passes ``max_bytes``
    (and refused up front when ``Content-Length`` already says it will), so a
    multi-gigabyte response costs ``max_bytes``, not its size.
  * ``deadline`` optionally bounds the WHOLE fetch in wall-clock time —
    httpx's own timeouts are per network operation, so a server dripping one
    byte a second never trips them.

httpx is imported lazily (it is the ``net`` extra), so ``validate_url`` stays
importable in a consumer without it.

Known limitation, inherited from ``validate_url``: the resolve there and the
connect here are two lookups, so a hostile DNS server can answer differently
between them (DNS rebinding). See ``ssrf.py``.

Process flow position: called by a service's URL-input path; the service
maps ``BlockedURLError`` / ``FetchError`` to its own error shape.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

from .ssrf import validate_url

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx


class FetchError(ValueError):
    """A fetch that passed the SSRF check but did not produce a usable body.

    Covers a redirect, an HTTP error status, a body over ``max_bytes``, a
    timeout, and any transport failure. ``reason`` is a short phrase for a
    log line or a warning ("redirect not followed", "HTTP 404", "timed out",
    "larger than 5000000 bytes"); ``str(exc)`` is the fuller message.
    A ``ValueError`` for the same reason ``BlockedURLError`` is: a consumer
    that forgets to catch it fails the request instead of carrying on.
    """

    def __init__(self, message: str, reason: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason = reason or message


async def _fetch(
    url: str,
    *,
    timeout: Any,
    max_bytes: Optional[int],
    headers: Optional[Mapping[str, str]],
    transport: Optional["httpx.AsyncBaseTransport"],
) -> bytes:
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client:
            async with client.stream("GET", url, headers=dict(headers or {})) as response:
                status = response.status_code
                if 300 <= status < 400:
                    location = response.headers.get("location", "")
                    raise FetchError(
                        f"redirect not followed (HTTP {status}"
                        + (f" → {location[:200]}" if location else "")
                        + "); fetch the final URL directly",
                        reason="redirect not followed",
                    )
                if status >= 400:
                    raise FetchError(f"HTTP {status} fetching the URL", reason=f"HTTP {status}")
                if max_bytes is not None:
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError(
                            f"response declares {int(declared)} bytes, more than the "
                            f"{max_bytes}-byte limit",
                            reason=f"larger than {max_bytes} bytes",
                        )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if max_bytes is not None and len(body) > max_bytes:
                        raise FetchError(
                            f"response is larger than the {max_bytes}-byte limit",
                            reason=f"larger than {max_bytes} bytes",
                        )
                return bytes(body)
    except httpx.TimeoutException as exc:
        raise FetchError(f"timed out fetching the URL: {exc!r}", reason="timed out") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"could not fetch the URL: {exc}", reason="fetch failed") from exc


async def fetch_url(
    url: str,
    *,
    timeout: float,
    connect_timeout: Optional[float] = None,
    deadline: Optional[float] = None,
    max_bytes: Optional[int] = None,
    blocked_networks: Optional[
        Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]
    ] = None,
    headers: Optional[Mapping[str, str]] = None,
    transport: Optional["httpx.AsyncBaseTransport"] = None,
) -> bytes:
    """GET ``url`` on a caller's behalf and return the body.

    Args:
        url:              The caller-supplied URL (http or https only).
        timeout:          httpx timeout per network operation, in seconds.
        connect_timeout:  Separate connect timeout; defaults to ``timeout``.
        deadline:         Optional wall-clock bound on the whole fetch,
                          including the body. None = only ``timeout`` applies.
        max_bytes:        Abort once the body exceeds this many bytes. None =
                          unbounded (the caller bounds it some other way).
        blocked_networks: Passed to ``validate_url``; defaults to
                          ``DEFAULT_BLOCKED_NETWORKS``.
        headers:          Request headers (a User-Agent, typically — some
                          servers 403 the default one).
        transport:        An httpx transport override. Tests pass
                          ``httpx.MockTransport``; production leaves it None.

    Returns:
        The complete response body (a 2xx).

    Raises:
        BlockedURLError: The URL failed the SSRF check — nothing was requested.
        FetchError:      Redirect, HTTP error status, body over ``max_bytes``,
                         a timeout (``timeout`` or ``deadline``), or any other
                         transport failure.
    """
    import httpx

    # validate_url resolves the hostname with a blocking getaddrinfo; in a
    # thread, so a slow or unresolvable name stalls this fetch, not the event
    # loop every other request (and every concurrent fetch) is running on.
    await asyncio.to_thread(validate_url, url, blocked_networks)
    http_timeout = httpx.Timeout(
        timeout, connect=connect_timeout if connect_timeout is not None else timeout
    )
    fetch = _fetch(
        url, timeout=http_timeout, max_bytes=max_bytes, headers=headers, transport=transport
    )
    if deadline is None:
        return await fetch
    try:
        return await asyncio.wait_for(fetch, timeout=deadline)
    except asyncio.TimeoutError as exc:
        raise FetchError(
            f"timed out: the fetch took longer than {deadline:g}s", reason="timed out"
        ) from exc
