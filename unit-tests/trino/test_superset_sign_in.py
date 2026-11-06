"""Checks for Superset's header sign-in behind oauth2-proxy-superset
(ai/trino/superset/superset_config.py, ai/oauth2-proxy/docker-compose.oauth2-proxy.yml,
ai/edge/haproxy.cfg). TRINO.md § Header-auth trust boundary.

No Superset install is needed: superset_config.py is executed with a stub
for the one Flask-AppBuilder constant it imports. What the tests hold in place:

* Flask-AppBuilder 4.4.1's AUTH_REMOTE_USER login view reads only
  request.environ[AUTH_REMOTE_USER_ENV_VAR], so that key must be the WSGI name
  of the header oauth2-proxy-superset sets on the REQUEST (X-Forwarded-Email,
  from PASS_USER_HEADERS) -- never X-Auth-Request-*, which oauth2-proxy only
  puts on responses and does not strip from requests.
* The edge's LAN port deletes that header and every underscore spelling of
  it (gunicorn files X_Forwarded_Email under the same environ key).
* Superset is served from the root of SUPERSET_PUBLIC_HOST, and ProxyFix
  counts the proxies that write each X-Forwarded-* header on that path.

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/trino -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = _ROOT / "ai" / "trino" / "superset" / "superset_config.py"
TRINO_COMPOSE = _ROOT / "ai" / "trino" / "docker-compose.trino.yml"
PROXY_COMPOSE = _ROOT / "ai" / "oauth2-proxy" / "docker-compose.oauth2-proxy.yml"
EDGE_CFG = _ROOT / "ai" / "edge" / "haproxy.cfg"
ENV_EXAMPLE = _ROOT / ".env.example"

# Flask-AppBuilder 4.4.1, flask_appbuilder/const.py.
FAB_AUTH_REMOTE_USER = 3
HOST_VAR = "${SUPERSET_PUBLIC_HOST:-superset.zeoenergy.com}"


def _yaml():
    return pytest.importorskip("yaml")


def _services(path: pathlib.Path) -> dict:
    return _yaml().safe_load(path.read_text(encoding="utf-8"))["services"]


def _superset_env() -> dict:
    return _services(TRINO_COMPOSE)["superset"]["environment"]


def _proxy(name: str) -> dict:
    return _services(PROXY_COMPOSE)[name]


def _load_config(monkeypatch, header: str | None) -> types.ModuleType:
    manager = types.ModuleType("flask_appbuilder.security.manager")
    manager.AUTH_REMOTE_USER = FAB_AUTH_REMOTE_USER
    for name, module in (
        ("flask_appbuilder", types.ModuleType("flask_appbuilder")),
        ("flask_appbuilder.security", types.ModuleType("flask_appbuilder.security")),
        ("flask_appbuilder.security.manager", manager),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    for var in ("SUPERSET_SECRET_KEY", "SUPERSET_DB_USER", "SUPERSET_DB_PASSWORD", "SUPERSET_DB_NAME"):
        monkeypatch.setenv(var, "test")
    if header is None:
        monkeypatch.delenv("SUPERSET_REMOTE_USER_HEADER", raising=False)
    else:
        monkeypatch.setenv("SUPERSET_REMOTE_USER_HEADER", header)
    spec = importlib.util.spec_from_file_location("superset_config_under_test", CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gunicorn_environ_key(header_name: str) -> str:
    # gunicorn 21.2.0 (Superset 4.0.2's pin), http/message.py + http/wsgi.py:
    # the name is upper-cased, underscores are legal, then
    # key = 'HTTP_' + name.replace('-', '_').
    return "HTTP_" + header_name.upper().replace("-", "_")


def _listen(name: str) -> list[str]:
    lines, inside = [], False
    for raw in EDGE_CFG.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not raw[:1].isspace() and line:
            inside = line == f"listen {name}"
            continue
        if inside and line:
            lines.append(line)
    return lines


# ── superset_config.py ──────────────────────────────────────────────────


def test_login_reads_the_environ_key_of_the_header_the_proxy_sets(monkeypatch):
    header = _superset_env()["SUPERSET_REMOTE_USER_HEADER"]
    assert header == "X-Forwarded-Email"
    config = _load_config(monkeypatch, header)
    assert config.AUTH_TYPE == FAB_AUTH_REMOTE_USER
    assert config.AUTH_REMOTE_USER_ENV_VAR == "HTTP_X_FORWARDED_EMAIL"
    assert config.AUTH_REMOTE_USER_ENV_VAR == _gunicorn_environ_key(header)
    assert config.AUTH_USER_REGISTRATION is True
    # The dead hook it replaces: FAB 4.4.1 has no get_user_identifier to call.
    assert not hasattr(config, "CUSTOM_SECURITY_MANAGER")


def test_default_header_is_the_one_compose_sets(monkeypatch):
    config = _load_config(monkeypatch, None)
    assert config.AUTH_REMOTE_USER_ENV_VAR == _gunicorn_environ_key(_superset_env()["SUPERSET_REMOTE_USER_HEADER"])


def test_never_trusts_a_response_only_header(monkeypatch):
    for header in ("X-Auth-Request-Email", "X-Auth-Request-User"):
        assert _superset_env()["SUPERSET_REMOTE_USER_HEADER"] != header
    assert "X_AUTH_REQUEST" not in _load_config(monkeypatch, None).AUTH_REMOTE_USER_ENV_VAR


def test_proxy_fix_counts_the_writers_on_the_public_path(monkeypatch):
    config = _load_config(monkeypatch, None)
    assert config.ENABLE_PROXY_FIX is True
    # Cloudflare sets X-Forwarded-For and oauth2-proxy appends cloudflared;
    # only Cloudflare writes X-Forwarded-Proto; nobody writes -Host/-Port/-Prefix.
    assert config.PROXY_FIX_CONFIG == {"x_for": 2, "x_proto": 1, "x_host": 0, "x_port": 0, "x_prefix": 0}


def test_served_from_the_root(monkeypatch):
    config = _load_config(monkeypatch, None)
    assert not getattr(config, "APPLICATION_ROOT", None)
    assert not getattr(config, "STATIC_ASSETS_PREFIX", None)


# ── the edge: LAN port 8016 ─────────────────────────────────────────────


def test_edge_deletes_the_header_and_every_underscore_spelling():
    header = _superset_env()["SUPERSET_REMOTE_USER_HEADER"]
    rules = _listen("superset")
    assert f"http-request del-header {header}" in rules
    # gunicorn maps X_Forwarded_Email to the same key as X-Forwarded-Email.
    assert _gunicorn_environ_key("X_Forwarded_Email") == _gunicorn_environ_key(header)
    assert "http-request del-header _ -m sub" in rules
    # Unconditional: no ACL may exempt a request from the deletes.
    assert not [r for r in rules if r.startswith("http-request del-header") and (" if " in r or " unless " in r)]


# ── oauth2-proxy-superset ───────────────────────────────────────────────


def test_proxy_serves_superset_at_the_root_of_its_own_host():
    env = _proxy("oauth2-proxy-superset")["environment"]
    assert env["OAUTH2_PROXY_UPSTREAMS"].split(",") == ["http://superset:8088/", "http://oauth2-assets:80/assets/"]
    assert env["OAUTH2_PROXY_REDIRECT_URL"] == f"https://{HOST_VAR}/oauth2/callback"
    assert env["OAUTH2_PROXY_COOKIE_DOMAINS"] == HOST_VAR
    assert env["OAUTH2_PROXY_WHITELIST_DOMAINS"] == HOST_VAR
    assert env["OAUTH2_PROXY_COOKIE_SAMESITE"] == "lax"
    assert env["OAUTH2_PROXY_COOKIE_NAME"] not in ("", "_oauth2_proxy")
    assert "OAUTH2_PROXY_COOKIE_NAME" not in _proxy("oauth2-proxy-openwebui")["environment"]


def test_proxy_sets_the_request_header_and_nothing_superset_does_not_need():
    env = _proxy("oauth2-proxy-superset")["environment"]
    assert env["OAUTH2_PROXY_PASS_USER_HEADERS"] == "true"
    assert env["OAUTH2_PROXY_SET_XAUTHREQUEST"] == "false"
    assert env["OAUTH2_PROXY_PASS_ACCESS_TOKEN"] == "false"


def test_proxy_shares_image_gate_and_files_with_the_chat_instance():
    superset, chat = _proxy("oauth2-proxy-superset"), _proxy("oauth2-proxy-openwebui")
    assert superset["image"] == chat["image"]
    assert superset["healthcheck"]["test"] == chat["healthcheck"]["test"]
    assert superset["volumes"] == chat["volumes"]
    for var in ("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_EMAIL_DOMAINS",
                "OAUTH2_PROXY_GOOGLE_GROUPS", "OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL",
                "OAUTH2_PROXY_GOOGLE_SERVICE_ACCOUNT_JSON", "OAUTH2_PROXY_SKIP_AUTH_ROUTES",
                "OAUTH2_PROXY_HTTP_ADDRESS"):
        assert superset["environment"][var] == chat["environment"][var], var


def test_proxy_is_swap_class_without_ports_or_an_edge_listener():
    svc = _proxy("oauth2-proxy-superset")
    assert svc["labels"]["com.zeo.deploy"] == "swap"
    assert "ports" not in svc and "container_name" not in svc
    assert not _listen("oauth2-proxy-superset")


# ── chat.zeoenergy.com no longer routes Superset ────────────────────────


def test_chat_has_no_superset_path_and_the_host_variable_is_documented():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    upstreams = re.search(r"^OAUTH2_PROXY_UPSTREAMS=(.*)$", text, re.M).group(1).split(",")
    assert not [u for u in upstreams if "superset" in u]
    assert re.search(r"^SUPERSET_PUBLIC_HOST=superset\.zeoenergy\.com\s*$", text, re.M)
