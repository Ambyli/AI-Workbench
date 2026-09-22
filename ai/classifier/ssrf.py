"""SSRF (Server-Side Request Forgery) protection.

When a caller supplies an image URL, the service fetches it on their behalf.
Without validation, a malicious URL like http://192.168.1.1/admin or
http://db:5432 could cause the container to reach internal infrastructure.

validate_url() blocks requests to private, loopback, and link-local IP ranges
by resolving the hostname and checking the resolved IP against a blocklist.
It is called in analysis._load_bytes_from_input() before any HTTP fetch.

Process flow position: called by analysis.py whenever type="url" is received.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

from config import BLOCKED_NETWORKS
from logger import logger

# The blocklist itself (BLOCKED_NETWORKS) lives in config.py § SSRF blocklist.


def validate_url(url: str) -> None:
    """Raise HTTP 400 if the URL targets a private or internal network address.

    Steps:
      1. Parse the URL and reject non-http/https schemes.
      2. Resolve the hostname to an IP address via DNS.
      3. Check the resolved IP against all blocked networks.
      4. Raise 400 if blocked; return silently if safe.

    Args:
        url: The URL string supplied by the caller.

    Raises:
        HTTPException(400): If the URL scheme is invalid, the hostname cannot
            be resolved, or the resolved IP is in a blocked network.
    """
    logger.debug("validate_url: checking url=%s", url[:120])

    # Step 1 — only allow standard web schemes
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
        # Step 2 — resolve hostname to IP
        resolved = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(resolved)

        # Step 3 — check against every blocked network
        for network in BLOCKED_NETWORKS:
            if ip in network:
                logger.warning("validate_url: blocked SSRF attempt url=%s resolved=%s",
                               url[:120], resolved)
                raise HTTPException(
                    status_code=400,
                    detail="URL resolves to a blocked network address.",
                )
    except socket.gaierror as exc:
        raise HTTPException(
            status_code=400,
            detail=f"URL hostname '{hostname}' could not be resolved: {exc}",
        )

    # Step 4 — URL is safe to fetch
    logger.debug("validate_url: url passed SSRF check")
