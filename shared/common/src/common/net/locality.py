"""Locality: does a host resolve ONLY to this box's own networks?

The mirror image of :mod:`common.net.ssrf`. ``validate_url`` refuses a
caller-supplied URL that resolves INTO the private ranges; a service that
sends conversation text to another box-internal service needs the opposite
guarantee — that the host it is about to send to resolves only INSIDE them,
so a misconfigured URL (a public hostname, a typo that a search domain
completes, an ``https://`` endpoint on the internet) can never carry the
text off the box. Both use the one list, :data:`DEFAULT_BLOCKED_NETWORKS`:
what a URL from outside must never reach is exactly where text from inside
may go.

:func:`locality_problem` resolves the host (``getaddrinfo``, so IPv6 too and
every address of a multi-homed name) and returns ``None`` when every
address is private / loopback / link-local, else a short reason that names
the host and the rule, never an address. A host that does not resolve at
all fails: Docker DNS answers only for RUNNING containers, so a stopped
service is "not local" rather than an error to retry blindly.
:func:`check_locality` returns the same reason with whether the host
resolved at all, because a caller that caches the verdict must not keep an
unresolved one for long: a service that restarts, or starts after its
caller, does not resolve for a few seconds and then does.

Same limitation as ``ssrf.py``: this is a check, not a pinned connection; a
DNS answer that changes between this resolve and the caller's connect is
not caught. On ``ai_shared`` the resolver is Docker's own, which is the
trust boundary that matters here.

Blocking (``getaddrinfo``): an async caller runs it in a thread and caches
the verdict (``common.signals.SignalsClient`` keeps a resolved one for its
``models_ttl_s``, an unresolved one for ``unresolved_ttl_s``). Consumers:
``common.signals`` (the signals sidecar client); route-policy's classifier
client implements the same rule.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from .ssrf import DEFAULT_BLOCKED_NETWORKS

# host -> every address it resolves to.
Resolver = Callable[[str], Iterable[str]]


@dataclass(frozen=True)
class LocalityVerdict:
    """:func:`check_locality`'s answer: ``problem`` exactly as
    :func:`locality_problem` returns it (None = local), and ``resolved`` --
    False when the host could not be resolved or resolved to no address, a
    verdict that is often transient (Docker DNS answers only for running
    containers)."""

    problem: Optional[str]
    resolved: bool


def resolve_host(host: str) -> List[str]:
    """Every address ``host`` resolves to, sorted (``socket.getaddrinfo``,
    so IPv6 too, and every address of a multi-homed name). Raises
    ``socket.gaierror`` when it does not resolve."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


def check_locality(
    host: str,
    resolver: Optional[Resolver] = None,
    *,
    networks: Optional[Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]] = None,
) -> LocalityVerdict:
    """:func:`locality_problem`'s verdict, with whether ``host`` resolved
    (:class:`LocalityVerdict`). Never raises."""
    nets = tuple(networks if networks is not None else DEFAULT_BLOCKED_NETWORKS)
    if not isinstance(host, str) or not host:
        return LocalityVerdict("no host", True)
    try:
        addresses = list((resolver or resolve_host)(host))
    except Exception as exc:  # noqa: BLE001 -- gaierror, or anything a resolver raises
        return LocalityVerdict(f"host {host!r} could not be resolved ({type(exc).__name__})", False)
    if not addresses:
        return LocalityVerdict(f"host {host!r} resolved to no address", False)
    for raw in addresses:
        try:
            # An IPv6 literal from getaddrinfo can carry a %scope suffix.
            ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            return LocalityVerdict(f"host {host!r} resolved to an unusable address", True)
        if not any(ip.version == net.version and ip in net for net in nets):
            return LocalityVerdict(f"host {host!r} resolves to a public address", True)
    return LocalityVerdict(None, True)


def locality_problem(
    host: str,
    resolver: Optional[Resolver] = None,
    *,
    networks: Optional[Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]] = None,
) -> Optional[str]:
    """``None`` when every address ``host`` resolves to lies inside
    ``networks`` (default :data:`DEFAULT_BLOCKED_NETWORKS`: private,
    loopback, link-local); otherwise why not — ``"host 'x' resolves to a
    public address"``, ``"... could not be resolved (gaierror)"``, ``"...
    resolved to no address"``. Never raises; ``resolver`` (tests) replaces
    :func:`resolve_host`."""
    return check_locality(host, resolver, networks=networks).problem


def resolves_locally(
    host: str,
    resolver: Optional[Resolver] = None,
    *,
    networks: Optional[Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]] = None,
) -> bool:
    """``locality_problem(host, ...) is None``."""
    return locality_problem(host, resolver, networks=networks) is None
