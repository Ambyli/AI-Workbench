"""SSRF (Server-Side Request Forgery) protection for caller-supplied URLs.

When a service fetches a URL on a caller's behalf, the caller is choosing the
destination. On ``ai_shared`` that destination can be ``http://litellm:4000``,
``http://roofix-db:5432``, or any other container — so a URL input is a
request to proxy into the private network unless the address is checked.

:func:`validate_url` resolves the hostname and refuses the fetch when the
resolved address falls inside a private, loopback, or link-local range. It is
the *only* thing standing between a JSON body and the infrastructure, so the
rule lives here once and every service imports it.

Known limitation — this is a check, not a tunnel. Between the resolve here and
the connect inside httpx, a hostile DNS server can answer differently (the
classic DNS-rebinding race). Closing that hole means pinning the resolved
address into the transport, which neither consumer needs today; a caller who
must be protected against a rebinding attacker should be on an egress proxy
with an allowlist (the pattern ``ai/sandbox`` uses), not on this function.

Process flow position: called by a service's URL-input path immediately
before any HTTP fetch.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable, Sequence
from urllib.parse import urlparse

# Private/internal ranges a caller-supplied URL must never resolve to. Never
# remove the RFC1918 or loopback entries — on ai_shared that would let a URL
# reach litellm, the databases, or the vLLM containers. A consumer may pass a
# LONGER list to fence off more of its own infrastructure; passing a shorter
# one is how a service becomes an open proxy.
DEFAULT_BLOCKED_NETWORKS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918 private
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local
    ipaddress.ip_network("0.0.0.0/8"),         # "this" network
    ipaddress.ip_network("100.64.0.0/10"),     # shared address space (RFC6598)
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
)


class BlockedURLError(ValueError):
    """A caller-supplied URL was refused before any fetch.

    A ``ValueError`` so a consumer that forgets to catch it still fails the
    request rather than fetching anyway. ``common`` has no web framework, so
    each service maps this to its own error shape — both the classifier and
    the detector turn it into an HTTP 400 with ``str(exc)`` as the detail,
    which is safe to show a caller: it names the rule, never the resolved
    address of anything internal.
    """


def validate_url(
    url: str,
    blocked_networks: Iterable[
        ipaddress.IPv4Network | ipaddress.IPv6Network
    ] | None = None,
) -> None:
    """Raise :class:`BlockedURLError` if ``url`` targets an internal address.

    Steps:
      1. Parse the URL and reject non-http/https schemes.
      2. Resolve the hostname to every address DNS offers.
      3. Check EVERY resolved address against the blocklist — a hostname that
         answers with one public and one private address must not pass on the
         strength of the public one.
      4. Return silently when all of them are safe.

    Args:
        url:              The URL string supplied by the caller.
        blocked_networks: Networks to refuse. Defaults to
                          :data:`DEFAULT_BLOCKED_NETWORKS`.

    Raises:
        BlockedURLError: Bad scheme, missing or unresolvable hostname, or a
            resolved address inside a blocked network.
    """
    networks: Sequence = tuple(
        blocked_networks if blocked_networks is not None else DEFAULT_BLOCKED_NETWORKS
    )

    # Step 1 — only allow standard web schemes.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedURLError(
            f"URL scheme '{parsed.scheme}' is not allowed. Use http or https."
        )

    hostname = parsed.hostname
    if not hostname:
        raise BlockedURLError("Invalid URL: missing hostname.")

    # Step 2 — resolve. getaddrinfo (not gethostbyname) so IPv6-only hosts
    # resolve at all, and so a multi-homed name yields every address rather
    # than whichever one the resolver happened to return first.
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedURLError(
            f"URL hostname '{hostname}' could not be resolved: {exc}"
        ) from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise BlockedURLError(f"URL hostname '{hostname}' resolved to no address.")

    # Step 3 — every address must clear the blocklist.
    for raw in addresses:
        try:
            # An IPv6 literal from getaddrinfo can carry a %scope suffix.
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            raise BlockedURLError(
                f"URL hostname '{hostname}' resolved to an unusable address."
            )
        for network in networks:
            if ip.version == network.version and ip in network:
                raise BlockedURLError(
                    "URL resolves to a blocked network address."
                )

    # Step 4 — safe to fetch.
