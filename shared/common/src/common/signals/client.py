"""The async client of the ``signals`` sidecar: one ``POST /signals`` per
call, inside a wall-clock budget, and never an exception.

``SignalsClient(base_url).fetch(request, budget_s=0.1)`` returns a
:class:`SignalsOutcome` whatever happens remotely -- every failure is a
``status``:

* ``ok``          -- the sidecar answered 200 with a well-formed response,
                     cut to what the request asked
                     (:meth:`~common.signals.wire.SignalsResponse.for_request`);
                     individual tasks may still say ``deadline`` / ``skipped``
                     / ``warming`` / ``error``, or ``partial`` -- usable for
                     the texts it carries, with ``error`` naming the type a
                     missing text failed with, if one did;
* ``timeout``     -- no answer within ``budget_s`` (counted from the call,
                     the locality check included);
* ``unavailable`` -- no sidecar configured (an empty URL), it could not be
                     reached, or it answered 5xx (still loading: ``/health``
                     is 503 until its models are warm);
* ``error``       -- a 4xx (413 over the caps, 422 a malformed request), an
                     answer that is not the wire format, a request the
                     client refused to send (over the caps), or a URL that
                     is not ``http(s)://host``;
* ``not_local``   -- the URL's host does not resolve only to this box's
                     networks, so nothing was sent.

**Locality guard.** Before anything is sent, the base URL's host must
resolve only inside ``common.net.DEFAULT_BLOCKED_NETWORKS`` (private,
loopback, link-local -- :func:`common.net.locality_problem`), the rule
route-policy's classifier client applies to the document classifier's
model endpoints. The request carries conversation text; a URL pointed at a
public host by mistake must not carry it off the box. The verdict is cached
per host for ``models_ttl_s`` (the resolve runs in a thread and finishes,
filling the cache, even when the budget cuts the call that started it), so
the steady state costs no DNS at all -- except a host that did not resolve
(or resolved to no address), which is cached for ``unresolved_ttl_s`` only:
Docker DNS answers only for running containers, so a sidecar that restarts,
or starts after its caller, would otherwise read as ``not_local`` for a
whole ``models_ttl_s`` after it is back.

**No text out.** The client logs nothing; ``error`` strings are built from
status codes, exception type names and :class:`~common.signals.wire.WireError`
messages (which never quote input); a response body is never quoted. A
text with a lone surrogate is sent with it replaced (``?``) rather than
failing the encode.

httpx is imported lazily (the ``signals`` extra, or the consumer's own
httpx), on the first call; the wire dataclasses need nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from ..net.locality import LocalityVerdict, Resolver, check_locality
from .wire import ExampleSet, SignalsRequest, SignalsResponse, TaskRequest, WireError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

OUTCOME_STATUSES: Tuple[str, ...] = ("ok", "timeout", "error", "unavailable", "not_local")
ERROR_MAX_CHARS = 300


@dataclass(frozen=True)
class SignalsOutcome:
    """What one :meth:`SignalsClient.fetch` came to: ``status``
    (:data:`OUTCOME_STATUSES`), the response (``ok`` only), the wall-clock
    time of the call in ms, and a short reason for anything but ``ok``
    (never request text)."""

    status: str
    response: Optional[SignalsResponse]
    ms: float
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class _Stop(Exception):
    def __init__(self, status: str, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


class SignalsClient:
    """``POST /signals`` against one sidecar (``base_url``, e.g.
    ``http://signals:8000``; empty = none configured, every call
    ``unavailable``). One ``httpx.AsyncClient`` per instance, created on the
    first call; :meth:`aclose` at shutdown (or ``async with``).

    ``models_ttl_s`` is how long a host's locality verdict is cached, and
    ``unresolved_ttl_s`` how long one for a host that did not resolve is;
    ``transport`` (an ``httpx`` transport, tests pass ``MockTransport``) and
    ``resolver`` (host -> addresses) replace the network and DNS."""

    def __init__(self, base_url: str, *, models_ttl_s: float = 60.0, unresolved_ttl_s: float = 2.0,
                 transport: Optional["httpx.AsyncBaseTransport"] = None,
                 resolver: Optional[Resolver] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.models_ttl_s = float(models_ttl_s)
        self.unresolved_ttl_s = float(unresolved_ttl_s)
        self._transport = transport
        self._resolver = resolver
        self._clock = clock
        self._http: Optional["httpx.AsyncClient"] = None
        self._hosts: Dict[str, Tuple[float, Optional[str]]] = {}   # host -> (expires at, problem)
        self._resolving: Dict[str, "asyncio.Future[LocalityVerdict]"] = {}
        parsed = urlparse(self.base_url) if self.base_url else None
        self._url_problem: Optional[str] = None
        self._host: Optional[str] = None
        if parsed is not None:
            if parsed.scheme not in ("http", "https"):
                self._url_problem = "the signals URL is not http(s)"
            elif not parsed.hostname:
                self._url_problem = "the signals URL has no host"
            else:
                self._host = parsed.hostname

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> "SignalsClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- the calls -------------------------------------------------------------------

    async def fetch(self, request: SignalsRequest, *, budget_s: float) -> SignalsOutcome:
        """One ``POST /signals`` for ``request``, answered within
        ``budget_s`` seconds. Never raises (a cancellation of the calling
        task still propagates)."""
        started = self._clock()
        try:
            if not self.base_url:
                raise _Stop("unavailable", "no signals URL configured")
            if self._url_problem is not None:
                raise _Stop("error", self._url_problem)
            if not isinstance(request, SignalsRequest):
                raise _Stop("error", "not a SignalsRequest")
            caps = request.cap_problem()
            if caps is not None:
                raise _Stop("error", f"request over the caps, not sent: {caps}")
            if budget_s is None or budget_s <= 0:
                raise _Stop("timeout", "no budget left")
            response = await asyncio.wait_for(self._call(request, budget_s), timeout=budget_s)
            return self._outcome("ok", started, response=response)
        except _Stop as stop:
            return self._outcome(stop.status, started, error=stop.error)
        except asyncio.TimeoutError:
            return self._outcome("timeout", started, error=f"no answer within {budget_s:g}s")
        except Exception as exc:  # noqa: BLE001 -- the contract: never raise for a remote failure
            return self._outcome("error", started, error=f"unexpected {type(exc).__name__}")

    async def warm(self, examples: ExampleSet, *, budget_s: float, max_tokens: Optional[int] = None,
                   request_id: Optional[str] = None) -> SignalsOutcome:
        """Ask the sidecar to embed ``examples`` now (a ``nearest`` task over
        no texts), so a later call naming its fingerprint finds it ready; the
        answer's ``nearest`` status is ``warming`` (started) or ``ok``
        (already cached). Also fills the locality cache."""
        try:
            request = SignalsRequest((), (TaskRequest("nearest", (), max_tokens=max_tokens, examples=examples),),
                                     request_id=request_id)
        except WireError as exc:
            return SignalsOutcome("error", None, 0.0, str(exc))
        return await self.fetch(request, budget_s=budget_s)

    # -- inside the budget -----------------------------------------------------------

    async def _call(self, request: SignalsRequest, budget_s: float) -> SignalsResponse:
        assert self._host is not None
        problem = await self._locality(self._host)
        if problem is not None:
            raise _Stop("not_local", f"refusing to send text: {problem}")
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(transport=self._transport, follow_redirects=False)
        body = json.dumps(request.to_json(), ensure_ascii=False, separators=(",", ":"))
        try:
            r = await self._http.post(f"{self.base_url}/signals",
                                      content=body.encode("utf-8", "replace"),
                                      headers={"content-type": "application/json"},
                                      timeout=httpx.Timeout(budget_s))
        except httpx.TimeoutException:
            raise asyncio.TimeoutError from None
        except httpx.HTTPError as exc:
            raise _Stop("unavailable", f"POST /signals: {type(exc).__name__}") from None
        if r.status_code >= 500:
            raise _Stop("unavailable", f"POST /signals answered {r.status_code}")
        if r.status_code != 200:
            raise _Stop("error", f"POST /signals answered {r.status_code}")
        try:
            payload = r.json()
        except ValueError:
            raise _Stop("error", "POST /signals answered something that is not JSON") from None
        try:
            return SignalsResponse.from_json(payload).for_request(request)
        except WireError as exc:
            raise _Stop("error", f"malformed response: {exc}") from None

    async def _locality(self, host: str) -> Optional[str]:
        """The cached verdict for ``host`` (None = local), resolving at most
        once at a time per host. The resolve is shielded: a budget that runs
        out while it is in flight does not stop it from filling the cache. A
        host that did not resolve is kept ``unresolved_ttl_s``, any other
        verdict -- local or public -- ``models_ttl_s``."""
        now = self._clock()
        cached = self._hosts.get(host)
        if cached is not None and now < cached[0]:
            return cached[1]
        pending = self._resolving.get(host)
        if pending is None:
            pending = asyncio.ensure_future(asyncio.to_thread(check_locality, host, self._resolver))
            self._resolving[host] = pending

            def done(task: "asyncio.Future[LocalityVerdict]") -> None:
                self._resolving.pop(host, None)
                if not task.cancelled() and task.exception() is None:
                    verdict = task.result()
                    ttl = self.models_ttl_s if verdict.resolved else self.unresolved_ttl_s
                    self._hosts[host] = (self._clock() + ttl, verdict.problem)

            pending.add_done_callback(done)
        return (await asyncio.shield(pending)).problem

    def _outcome(self, status: str, started: float, *, response: Optional[SignalsResponse] = None,
                 error: Optional[str] = None) -> SignalsOutcome:
        if error is not None and len(error) > ERROR_MAX_CHARS:
            error = error[:ERROR_MAX_CHARS - 1] + "…"
        return SignalsOutcome(status, response, round((self._clock() - started) * 1000.0, 1), error)
