#!/usr/bin/env python3
"""Generate every required SUPABASE_* secret for ai/supabase.

Standard library only — this runs on the box before anything is installed.

Why not upstream's ``utils/generate-keys.sh``: it ``sed``s UNPREFIXED names
(``POSTGRES_PASSWORD=``, ``MINIO_ROOT_PASSWORD=``, …) into ``./.env``. This repo
has ONE shared ``.env`` where ``MINIO_ROOT_PASSWORD`` belongs to Trino's MinIO
and ``POSTGRES_*`` is generic, so upstream's script would silently clobber
another subsystem's credentials. Everything here is ``SUPABASE_``-prefixed and
only ever touches lines whose key starts with that prefix.

Usage
-----
    # print the block, paste it into the .env Supabase section yourself
    python ai/supabase/bin/generate_keys.py

    # fill in only the EMPTY SUPABASE_* lines of an existing .env, in place
    python ai/supabase/bin/generate_keys.py --write-env .env

    # rotate: overwrite SUPABASE_* values that are already populated too
    python ai/supabase/bin/generate_keys.py --write-env .env --force

    # self-check: decode both JWTs, re-compute the HMAC, assert every length
    python ai/supabase/bin/generate_keys.py --verify

Rotation warning
----------------
Several of these are ONE-SHOT: Supavisor stores the database password in its
tenant row encrypted under ``VAULT_ENC_KEY``, and Realtime stores the tenant's
``jwt_secret`` encrypted under ``DB_ENC_KEY``. Rotating ``SUPABASE_POSTGRES_PASSWORD``,
``SUPABASE_JWT_SECRET``, ``SUPABASE_VAULT_ENC_KEY`` or
``SUPABASE_REALTIME_DB_ENC_KEY`` after first boot needs those tenant rows
re-seeded by hand. See ai/supabase/SUPABASE.md § Secrets that are one-shot.

Scope
-----
Legacy HS256 API keys only (``ANON_KEY`` / ``SERVICE_ROLE_KEY``). The ES256 /
``sb_publishable_…`` key pair is a documented follow-up — upstream's
``utils/add-new-auth-keys.sh`` is the path. See SUPABASE.md § Asymmetric keys.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
import time

# Five years, matching upstream's sample keys.
JWT_LIFETIME_SECONDS = 5 * 365 * 24 * 60 * 60
JWT_ISSUER = "supabase"


# ── primitives ──────────────────────────────────────────────────────────────
# Hex and standard-base64 alphabets only. Neither contains $ # | or &, which
# keeps every value safe for Compose's .env parsing (no accidental variable
# interpolation) and for the `sed s|…|…|` substitutions Envoy's entrypoint runs
# over lds.template.yaml.

def _hex(nbytes: int) -> str:
    return secrets.token_hex(nbytes)


def _b64(nbytes: int) -> str:
    return base64.b64encode(secrets.token_bytes(nbytes)).decode("ascii")


def _b64url(raw: bytes) -> str:
    """base64url, unpadded — the JWT wire encoding."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def make_jwt(role: str, secret: str, issued_at: int | None = None) -> str:
    """HS256 JWT in upstream's exact shape.

    Header ``{"alg":"HS256","typ":"JWT"}``; payload
    ``{"role":…,"iss":"supabase","iat":…,"exp":…}``. Compact JSON separators so
    the signing input is byte-stable.
    """
    issued_at = int(time.time()) if issued_at is None else issued_at
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "role": role,
        "iss": JWT_ISSUER,
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME_SECONDS,
    }
    parts = [
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ]
    signing_input = ".".join(parts).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    parts.append(_b64url(signature))
    return ".".join(parts)


# ── the secret set ──────────────────────────────────────────────────────────
# (group, key, generator) — groups only shape the printed output.

def generate() -> "list[tuple[str, str, str]]":
    """Return [(group, KEY, value)] for every required SUPABASE_* secret."""
    jwt_secret = _b64(30)  # 40 chars; upstream requires >= 32
    now = int(time.time())
    return [
        ("Database", "SUPABASE_POSTGRES_PASSWORD", _hex(24)),
        ("JWT + legacy API keys", "SUPABASE_JWT_SECRET", jwt_secret),
        ("JWT + legacy API keys", "SUPABASE_ANON_KEY", make_jwt("anon", jwt_secret, now)),
        ("JWT + legacy API keys", "SUPABASE_SERVICE_ROLE_KEY", make_jwt("service_role", jwt_secret, now)),
        ("Dashboard (Studio basic auth)", "SUPABASE_DASHBOARD_PASSWORD", _hex(16)),
        # >= 64 chars: Realtime + Supavisor cookie/session signing.
        ("Service encryption keys", "SUPABASE_SECRET_KEY_BASE", _b64(48)),
        # EXACTLY 16 chars.
        ("Service encryption keys", "SUPABASE_REALTIME_DB_ENC_KEY", _hex(8)),
        # EXACTLY 32 chars.
        ("Service encryption keys", "SUPABASE_VAULT_ENC_KEY", _hex(16)),
        # >= 32 chars.
        ("Service encryption keys", "SUPABASE_PG_META_CRYPTO_KEY", _b64(24)),
        ("Storage S3-protocol endpoint", "SUPABASE_S3_PROTOCOL_ACCESS_KEY_ID", _hex(16)),
        ("Storage S3-protocol endpoint", "SUPABASE_S3_PROTOCOL_ACCESS_KEY_SECRET", _hex(32)),
        ("Trino federation", "SUPABASE_TRINO_READER_PASSWORD", _hex(24)),
    ]


# Length contracts the services enforce at runtime. (key, comparison, length)
LENGTH_RULES = [
    ("SUPABASE_JWT_SECRET", "min", 32),
    ("SUPABASE_SECRET_KEY_BASE", "min", 64),
    ("SUPABASE_REALTIME_DB_ENC_KEY", "exact", 16),
    ("SUPABASE_VAULT_ENC_KEY", "exact", 32),
    ("SUPABASE_PG_META_CRYPTO_KEY", "min", 32),
]

UNSAFE_CHARS = set("$#|&'\" \t")


# ── output modes ────────────────────────────────────────────────────────────

def print_block(secrets_list: "list[tuple[str, str, str]]") -> None:
    print("# Generated by ai/supabase/bin/generate_keys.py")
    print("# Paste these into the '## Supabase subsystem' block of .env.")
    print("# Treat every line below as a production credential.")
    current_group = None
    for group, key, value in secrets_list:
        if group != current_group:
            print()
            print(f"# {group}")
            current_group = group
        print(f"{key}={value}")


def write_env(path: str, secrets_list: "list[tuple[str, str, str]]", force: bool) -> int:
    """Fill SUPABASE_* assignments in `path` in place.

    Only lines matching ``^SUPABASE_<KNOWN_KEY>=`` are ever touched, and without
    --force only those whose value is empty. Every other byte of the file is
    preserved exactly, comments and unrelated subsystems included.
    """
    wanted = {key: value for _, key, value in secrets_list}
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            original = fh.read()
    except FileNotFoundError:
        print(f"error: {path} does not exist", file=sys.stderr)
        return 1

    # Split keeping line endings so we never rewrite CRLF/LF on untouched lines.
    lines = original.splitlines(keepends=True)
    pattern = re.compile(r"^(SUPABASE_[A-Z0-9_]+)=(.*?)(\r?\n?)$")

    written, skipped, missing = [], [], set(wanted)
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        key, existing, eol = m.group(1), m.group(2), m.group(3)
        if key not in wanted:
            continue
        missing.discard(key)
        if existing.strip() and not force:
            skipped.append(key)
            continue
        lines[i] = f"{key}={wanted[key]}{eol}"
        written.append(key)

    if written:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines))

    for key in written:
        print(f"set      {key}")
    for key in sorted(skipped):
        print(f"kept     {key}  (already populated; --force to rotate)")
    for key in sorted(missing):
        print(f"MISSING  {key}  (no such line in {path} — add it, then re-run)")

    if missing:
        print(
            f"\n{len(written)} written, {len(skipped)} kept, {len(missing)} missing.",
            file=sys.stderr,
        )
        return 1
    print(f"\n{len(written)} written, {len(skipped)} kept.")
    return 0


def verify() -> int:
    """Self-check: decode both JWTs, re-compute the HMAC, assert every rule."""
    failures = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    secrets_list = generate()
    values = {key: value for _, key, value in secrets_list}

    # 1. every declared secret is present and non-empty
    check("all secrets generated", all(v for v in values.values()), f"{len(values)} keys")

    # 2. length contracts
    for key, comparison, length in LENGTH_RULES:
        actual = len(values[key])
        ok = actual >= length if comparison == "min" else actual == length
        check(f"{key} length {comparison} {length}", ok, f"got {actual}")

    # 3. shell/compose-safe alphabet
    for key, value in values.items():
        bad = sorted(UNSAFE_CHARS & set(value))
        check(f"{key} free of $ # | & quotes/space", not bad, f"found {bad}" if bad else "")

    # 4. the two JWTs decode to the expected claims
    jwt_secret = values["SUPABASE_JWT_SECRET"]
    for key, expected_role in (
        ("SUPABASE_ANON_KEY", "anon"),
        ("SUPABASE_SERVICE_ROLE_KEY", "service_role"),
    ):
        token = values[key]
        segments = token.split(".")
        if len(segments) != 3:
            check(f"{key} has three segments", False, f"got {len(segments)}")
            continue
        check(f"{key} has three segments", True)

        header = json.loads(_b64url_decode(segments[0]))
        check(f"{key} header alg=HS256 typ=JWT", header == {"alg": "HS256", "typ": "JWT"}, str(header))

        payload = json.loads(_b64url_decode(segments[1]))
        check(f"{key} role={expected_role}", payload.get("role") == expected_role, str(payload.get("role")))
        check(f"{key} iss={JWT_ISSUER}", payload.get("iss") == JWT_ISSUER, str(payload.get("iss")))
        lifetime = payload.get("exp", 0) - payload.get("iat", 0)
        check(f"{key} exp - iat == 5 years", lifetime == JWT_LIFETIME_SECONDS, f"{lifetime}s")

        # 5. re-compute the signature over the wire bytes
        expected_sig = hmac.new(
            jwt_secret.encode("utf-8"),
            ".".join(segments[:2]).encode("ascii"),
            hashlib.sha256,
        ).digest()
        check(
            f"{key} HMAC-SHA256 signature verifies",
            hmac.compare_digest(_b64url(expected_sig), segments[2]),
        )

        # 6. a wrong secret must NOT verify
        wrong_sig = hmac.new(b"not-the-secret", ".".join(segments[:2]).encode("ascii"), hashlib.sha256).digest()
        check(f"{key} rejects a wrong secret", _b64url(wrong_sig) != segments[2])

    # 7. the two role keys are distinct
    check(
        "anon and service_role keys differ",
        values["SUPABASE_ANON_KEY"] != values["SUPABASE_SERVICE_ROLE_KEY"],
    )

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the SUPABASE_* secrets for ai/supabase.",
        epilog="With no options, prints a paste-ready block to stdout.",
    )
    parser.add_argument(
        "--write-env",
        metavar="PATH",
        help="fill empty SUPABASE_* lines in PATH in place (e.g. .env)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --write-env, also overwrite SUPABASE_* values that are already set (rotation)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="self-check the generator and exit; writes nothing",
    )
    args = parser.parse_args(argv)

    if args.verify:
        if args.write_env:
            parser.error("--verify writes nothing; drop --write-env")
        return verify()

    if args.force and not args.write_env:
        parser.error("--force only means something with --write-env")

    secrets_list = generate()
    if args.write_env:
        return write_env(args.write_env, secrets_list, args.force)
    print_block(secrets_list)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
