"""SSRF protection — validates URLs before the service fetches them."""

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

from logger import logger

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918 private
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local
    ipaddress.ip_network("0.0.0.0/8"),         # "this" network
    ipaddress.ip_network("100.64.0.0/10"),     # shared address space
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


def validate_url(url: str) -> None:
    """Raise HTTP 400 if the URL targets a private/internal network address.

    Prevents SSRF attacks where a crafted URL causes the service to reach
    internal infrastructure (e.g. http://192.168.1.1, http://db:5432).
    """
    logger.debug("validate_url: checking url=%s", url[:120])

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"URL scheme '{parsed.scheme}' is not allowed. Use http or https.",
        )

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL: missing hostname.")

    try:
        resolved = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(resolved)
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                logger.warning("validate_url: blocked SSRF attempt url=%s resolved=%s", url[:120], resolved)
                raise HTTPException(
                    status_code=400,
                    detail=f"URL resolves to a blocked network address.",
                )
    except socket.gaierror as exc:
        raise HTTPException(
            status_code=400,
            detail=f"URL hostname '{hostname}' could not be resolved: {exc}",
        )

    logger.debug("validate_url: url passed SSRF check")
