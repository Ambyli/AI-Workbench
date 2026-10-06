"""
Per-profile login credentials for ``login_actions``.

A ``/capture`` request may carry ``login_actions`` — steps that run only when
the tab lands on a login wall (see INTERCEPTOR.md § Login actions). Their
``fill`` values can reference stored credentials by name, ``"${password}"``,
so a caller (or a model) never holds the secret. The values live server-side,
one file per profile, at ``INTERCEPTOR_LOGINS_DIR/<profile>.json``::

    {
      "allowed_origins": ["https://sso.enphaseenergy.com"],
      "values": {
        "username": "${ENV:INTERCEPTOR_LOGIN_ENPHASE_USERNAME}",
        "password": "${ENV:INTERCEPTOR_LOGIN_ENPHASE_PASSWORD}"
      }
    }

The file holds no secrets, so it is checked in: every value is an
``${ENV:INTERCEPTOR_LOGIN_…}`` reference to an environment variable (set in
``.env`` and passed through the interceptor's compose ``environment:``),
read when the file is. The prefix keeps a logins file from pulling any other
secret in the environment into a fill.

The directory is deliberately NOT under ``PROFILES_ROOT``: ``unpack_profile``
rmtree's a profile dir on every refresh, and ``list_profiles`` would advertise
a ``logins`` dir as a profile. In the container it is a read-only bind mount.

Rules enforced here (every error names a key, a step or an environment
variable, never a value):

- Every entry in ``values`` is ``${ENV:INTERCEPTOR_LOGIN_<NAME>}`` and the
  variable must be set and non-empty — a literal credential is refused.
- ``allowed_origins`` is required and non-empty. A credential-carrying fill
  only types into a tab whose ``location.origin`` is on it — so a request
  cannot point ``login_url_patterns`` at ``.*`` and have the password typed
  into some other site's form.
- ``${key}`` is only resolved in the ``value`` of a ``fill`` step. A ``${``
  anywhere else in ``login_actions`` is refused. ``$$`` is a literal ``$``; a
  ``$`` followed by anything else is literal too.
- The file (and the variables it names) is re-read on every request, so
  editing the file needs no restart — a changed ``.env`` value still needs
  ``make up interceptor`` to reach the container's environment.

Resolved values exist only on the in-memory ``Action`` objects handed to the
``InterceptorClient``; nothing here logs, stores, or returns them.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from common.cdp_interceptor import Action

import profiles


LOGINS_DIR = os.environ.get("INTERCEPTOR_LOGINS_DIR", "/config/logins")

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
ENV_PREFIX = "INTERCEPTOR_LOGIN_"
# A whole value: ``${ENV:INTERCEPTOR_LOGIN_<NAME>}``. Anything else is refused.
_ENV_REF_RE = re.compile(r"\$\{ENV:(" + ENV_PREFIX + r"[A-Z0-9_]+)\}")
# One token at a time: ``$$`` | ``${...}`` (well-formed or not) | a lone ``$``.
_TOKEN_RE = re.compile(r"\$\$|\$\{([^}]*)\}?|\$")
_STRING_FIELDS = ("selector", "text", "value", "key", "script")


class LoginConfigError(ValueError):
    """A logins file or a ``login_actions`` reference is unusable. The message
    is safe to return to the caller: it never contains a credential value."""


@dataclass(frozen=True)
class LoginConfig:
    allowed_origins: tuple[str, ...]
    values: Mapping[str, str] = field(repr=False)


def _path(profile: str) -> Path:
    profiles.validate_name(profile)
    return Path(LOGINS_DIR) / f"{profile}.json"


def _normalize_origin(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise LoginConfigError(f"{where}: each allowed origin must be a non-empty string")
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LoginConfigError(
            f"{where}: {raw!r} is not an origin — write it as scheme://host[:port], "
            "e.g. https://sso.example.com"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        raise LoginConfigError(f"{where}: {raw!r} must be a bare origin (no path, query or userinfo)")
    # The same shape location.origin has: lowercase, default port dropped.
    host = parts.hostname
    port = parts.port
    if port is not None and (parts.scheme, port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}"


def load(profile: str) -> LoginConfig:
    """Read and validate ``LOGINS_DIR/<profile>.json``. Raises
    ``LoginConfigError`` when it is missing or malformed."""
    path = _path(profile)
    where = f"logins file {path.name}"
    if not path.is_file():
        raise LoginConfigError(
            f"profile {profile!r} has no logins file ({path} under INTERCEPTOR_LOGINS_DIR) — "
            "login_actions need one; see INTERCEPTOR.md § Login actions"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # exc.msg / lineno only — str(exc) is safe too, but never echo the doc.
        raise LoginConfigError(f"{where}: not valid JSON ({exc.msg} at line {exc.lineno})") from None
    except OSError as exc:
        raise LoginConfigError(f"{where}: cannot be read ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise LoginConfigError(f"{where}: must be a JSON object")
    unknown = set(data) - {"allowed_origins", "values"}
    if unknown:
        raise LoginConfigError(
            f"{where}: unexpected key(s) {', '.join(sorted(map(str, unknown)))} — "
            "it takes allowed_origins and values"
        )

    origins = data.get("allowed_origins")
    if not isinstance(origins, list) or not origins:
        raise LoginConfigError(
            f"{where}: allowed_origins is required and must be a non-empty list — "
            "credentials are only typed into a page on one of these origins"
        )
    allowed = tuple(dict.fromkeys(_normalize_origin(o, where) for o in origins))

    values = data.get("values")
    if not isinstance(values, dict):
        raise LoginConfigError(f"{where}: values must be an object of name → string")
    resolved: dict[str, str] = {}
    for k, v in values.items():
        if not isinstance(k, str) or not _KEY_RE.fullmatch(k):
            raise LoginConfigError(
                f"{where}: value name {k!r} is not valid — use letters, digits and _ "
                "(not starting with a digit)"
            )
        if not isinstance(v, str):
            raise LoginConfigError(f"{where}: values.{k} must be a string")
        m = _ENV_REF_RE.fullmatch(v.strip())
        if m is None:
            # Never echo v: a literal here is most likely the credential itself.
            raise LoginConfigError(
                f"{where}: values.{k} must be an environment reference "
                f"${{ENV:{ENV_PREFIX}<NAME>}} — credentials live in .env, not in this file"
            )
        env_name = m.group(1)
        env_value = os.environ.get(env_name)
        if not env_value:
            raise LoginConfigError(
                f"{where}: values.{k} reads {env_name}, which is not set in the "
                "interceptor's environment — set it in .env, pass it through the "
                "environment block of docker-compose.interceptor.yml, then "
                "`make up interceptor`"
            )
        resolved[k] = env_value
    return LoginConfig(allowed_origins=allowed, values=resolved)


def _substitute(text: str, where: str, values: Mapping[str, str] | None) -> tuple[str, set[str]]:
    """Expand ``$$`` / ``${key}`` in one fill value. With ``values=None`` only
    syntax is checked and the referenced names collected."""
    out: list[str] = []
    keys: set[str] = set()
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        tok = m.group(0)
        if tok == "$$":
            out.append("$")
        elif tok == "$":
            out.append("$")
        else:
            name = m.group(1)
            if not tok.endswith("}"):
                raise LoginConfigError(f"{where}: unterminated reference '${{' — close it with }}")
            if not _KEY_RE.fullmatch(name):
                raise LoginConfigError(
                    f"{where}: ${{{name}}} is not a valid reference name — use letters, "
                    "digits and _ (write $$ for a literal $)"
                )
            keys.add(name)
            if values is not None:
                if name not in values:
                    raise LoginConfigError(
                        f"{where}: references ${{{name}}}, which the profile's logins file "
                        f"does not define (it defines: {', '.join(sorted(values)) or 'nothing'})"
                    )
                out.append(values[name])
    out.append(text[pos:])
    return "".join(out), keys


def _scan(actions: Iterable[Action], values: Mapping[str, str] | None,
          label: str) -> tuple[list[Action], set[str]]:
    resolved: list[Action] = []
    keys: set[str] = set()
    for i, a in enumerate(actions):
        where = f"{label}[{i}] ({a.type})"
        for name in _STRING_FIELDS:
            v = getattr(a, name, None)
            if not isinstance(v, str) or (a.type == "fill" and name == "value"):
                continue
            if "${" in v:
                raise LoginConfigError(
                    f"{where}: {name} contains '${{' — credential references are only "
                    "allowed in the value of a fill step"
                )
        if a.type == "fill" and a.value is not None:
            new_value, found = _substitute(a.value, f"{where} value", values)
            keys |= found
            resolved.append(dataclasses.replace(a, value=new_value))
        else:
            resolved.append(a)
    return resolved, keys


def referenced_keys(actions: Iterable[Action], *, label: str = "login_actions") -> set[str]:
    """Names referenced by ``${…}`` in the fill values. Raises
    ``LoginConfigError`` on a reference outside a fill value or bad syntax."""
    return _scan(actions, None, label)[1]


def resolve(actions: Iterable[Action], cfg: LoginConfig, *,
            label: str = "login_actions") -> list[Action]:
    """New ``Action`` objects with every ``${key}`` / ``$$`` in a fill value
    expanded from ``cfg.values``. The inputs are not modified."""
    return _scan(actions, cfg.values, label)[0]


def describe(profile: str) -> dict:
    """What a caller may know about a profile's logins: the reference names
    and the allowed origins — never a value. A profile with no file reports
    empty lists; an unusable file adds ``error``."""
    try:
        cfg = load(profile)
    except LoginConfigError as exc:
        try:
            missing = not _path(profile).is_file()
        except profiles.InvalidProfileNameError:
            missing = True
        out: dict = {"keys": [], "allowed_origins": []}
        if not missing:
            out["error"] = str(exc)
        return out
    return {"keys": sorted(cfg.values), "allowed_origins": list(cfg.allowed_origins)}
