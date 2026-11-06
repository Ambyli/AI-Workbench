"""Superset config.

Header-auth SSO via oauth2-proxy-superset (ai/oauth2-proxy), which serves
Superset at the root of its own hostname, SUPERSET_PUBLIC_HOST
(superset.zeoenergy.com). Superset does NOT run its own OAuth flow; the
``X-Forwarded-Email`` header that proxy sets identifies the session user, and
the very first hit auto-creates the local Superset account.

How the sign-in happens, in Flask-AppBuilder 4.4.1 (the version
apache/superset:4.0.2 pins): under AUTH_REMOTE_USER, the /login/ view
(AuthRemoteUserView.login) reads ``request.environ[AUTH_REMOTE_USER_ENV_VAR]``
and logs that username in. It never reads request headers itself, and the
default key, REMOTE_USER, is set by nothing behind a proxy. So
AUTH_REMOTE_USER_ENV_VAR below names the WSGI environ key the header arrives
under: gunicorn files X-Forwarded-Email as HTTP_X_FORWARDED_EMAIL.

SECURITY: this is safe ONLY when nothing but oauth2-proxy-superset can put
that key there. That proxy deletes every client copy of X-Forwarded-Email,
underscore spellings included, before it sets the real one. PORT_SUPERSET
(host publish, default 8016) is published by ai/edge, whose `listen superset`
deletes the identity headers AND every header with an underscore in its
name: gunicorn 21.2.0 files X_Forwarded_Email under the same environ key.
Bind that port to 127.0.0.1, or drop it, before exposing the host on an
untrusted network. See ai/trino/TRINO.md § Header-auth trust boundary.
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

# The header oauth2-proxy-superset sets on every authenticated request
# (OAUTH2_PROXY_PASS_USER_HEADERS). Not X-Auth-Request-Email: oauth2-proxy
# only ever puts X-Auth-Request-* on RESPONSES, and it does not delete a
# client's own copy on the way in. Changing it also needs a `del-header` for
# the new name in ai/edge/haproxy.cfg's `listen superset`.
_REMOTE_USER_HEADER = os.environ.get(
    "SUPERSET_REMOTE_USER_HEADER", "X-Forwarded-Email"
)


def _wsgi_environ_key(header: str) -> str:
    """The WSGI environ key a request header arrives under (PEP 3333)."""
    return "HTTP_" + header.strip().upper().replace("-", "_")


# Read by FAB's AuthRemoteUserView.login. See bootstrap.sh for the admin
# creation on first boot.
AUTH_REMOTE_USER_ENV_VAR = _wsgi_environ_key(_REMOTE_USER_HEADER)
AUTH_ROLES_SYNC_AT_LOGIN = True
AUTH_USER_REGISTRATION = True
AUTH_USER_REGISTRATION_ROLE = "Alpha"

# ── Behind a reverse proxy ──────────────────────────────────────────────
# Public path: Cloudflare → cloudflared → oauth2-proxy-superset → superset.
# Each count is how many proxies WRITE that header (cloudflared 2026.10.0
# ingress/origin_proxy.go, oauth2-proxy v7.15.5 pkg/upstream/http.go), which
# ProxyFix needs to pick the right entry:
#   x_for 2    Cloudflare sets the client's address and oauth2-proxy appends
#              cloudflared's; cloudflared adds nothing.
#   x_proto 1  Cloudflare sets https and both proxies pass it through. At 2,
#              ProxyFix would ignore the single value and build http:// URLs
#              -- FAB's post-login redirect (next=request.url) among them.
#   x_host 0   nobody sets X-Forwarded-Host; both proxies keep the Host header
#              (SUPERSET_PUBLIC_HOST), and a client-sent one must not steer it.
#   x_port 0, x_prefix 0   served from the root; nothing sets them.
# LAN requests through the edge's PORT_SUPERSET carry one X-Forwarded-For
# entry and no X-Forwarded-Proto, so they keep the edge's address and http.
ENABLE_PROXY_FIX = True
PROXY_FIX_CONFIG = {
    "x_for": 2,
    "x_proto": 1,
    "x_host": 0,
    "x_port": 0,
    "x_prefix": 0,
}

# Served from the root of SUPERSET_PUBLIC_HOST, so no APPLICATION_ROOT or
# STATIC_ASSETS_PREFIX: Superset 4.0.2 cannot run under a path prefix (its
# pages load /static/..., call /api/v1/... and redirect to /login/), which is
# why it left chat.zeoenergy.com/superset/. This address is `superset run`'s
# listener; the image's gunicorn binds 0.0.0.0:8088 itself (run-server.sh).
SUPERSET_WEBSERVER_ADDRESS = "0.0.0.0"
