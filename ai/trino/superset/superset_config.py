"""Superset config.

Header-auth SSO via oauth2-proxy. Superset does NOT run its own OAuth
flow; the ``X-Auth-Request-Email`` header oauth2-proxy sets identifies
the session user, and the very first hit auto-creates the local
Superset account.

SECURITY: this is safe ONLY when Superset is unreachable from anywhere
except oauth2-proxy. If PORT_SUPERSET (host publish, default 8016) is
bound to 0.0.0.0, anyone on the LAN can spoof the header and log in as
any Superset account. Bind to 127.0.0.1 in docker-compose.trino.yml, or
drop the publish entirely, before exposing this on an untrusted
network. See ai/trino/TRINO.md § Header-auth trust boundary.
"""
from __future__ import annotations

import os

from flask_appbuilder.security.manager import AUTH_REMOTE_USER


SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

# ── Metadata DB ─────────────────────────────────────────────────────────
SQLALCHEMY_DATABASE_URI = (
    f"postgresql+psycopg2://{os.environ['SUPERSET_DB_USER']}:"
    f"{os.environ['SUPERSET_DB_PASSWORD']}@superset-db:5432/"
    f"{os.environ['SUPERSET_DB_NAME']}"
)

# ── Trusted-header auth ─────────────────────────────────────────────────
AUTH_TYPE = AUTH_REMOTE_USER

# The base FAB REMOTE_USER manager reads request.environ["REMOTE_USER"].
# We swap in the oauth2-proxy header instead. See bootstrap.sh for the
# admin creation on first boot.
AUTH_ROLES_SYNC_AT_LOGIN = True
AUTH_USER_REGISTRATION = True
AUTH_USER_REGISTRATION_ROLE = "Alpha"


from flask import request  # noqa: E402  (needs SECRET_KEY set first)
from flask_appbuilder.security.manager import BaseSecurityManager  # noqa: E402
from superset.security import SupersetSecurityManager  # noqa: E402


_REMOTE_USER_HEADER = os.environ.get(
    "SUPERSET_REMOTE_USER_HEADER", "X-Auth-Request-Email"
)


class HeaderRemoteUserSecurityManager(SupersetSecurityManager):
    """Trust ``X-Auth-Request-Email`` from oauth2-proxy as the session
    user. Falls back to ``REMOTE_USER`` for local dev without a proxy.
    """

    def get_user_identifier(self, req):  # noqa: D401 - FAB hook name
        header = req.headers.get(_REMOTE_USER_HEADER)
        if header:
            return header
        return req.environ.get("REMOTE_USER")


CUSTOM_SECURITY_MANAGER = HeaderRemoteUserSecurityManager

# ── Behind a reverse proxy ──────────────────────────────────────────────
# cloudflared → oauth2-proxy → superset is two hops. Superset needs to
# walk that many entries back through X-Forwarded-For / -Proto to build
# correct absolute URLs (dashboard permalinks, SQL Lab share links).
ENABLE_PROXY_FIX = True
PROXY_FIX_CONFIG = {
    "x_for": 2,
    "x_proto": 2,
    "x_host": 2,
    "x_port": 2,
    "x_prefix": 2,
}

# Static-asset URL prefix when served under a subpath. oauth2-proxy will
# route /superset/* to this container, so the Flask blueprint mount
# needs to match.
SUPERSET_WEBSERVER_ADDRESS = "0.0.0.0"
