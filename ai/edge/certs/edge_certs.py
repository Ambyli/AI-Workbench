#!/usr/bin/env python3
"""edge-certs helper: keep the edge's TLS certificate on disk and in the running HAProxy.

Runs inside the `edge-certs` container (certbot/dns-cloudflare, Python 3.14),
stdlib only, called by entrypoint.sh and by certbot's --deploy-hook. Commands:

  placeholder <name>  Write a self-signed certificate + key for <name> to
                      /certs/edge.pem (the `openssl` CLI the image ships).
                      Used only while no certificate exists yet: HAProxy
                      refuses to start when an `ssl crt` file is missing.
  sync                If /etc/letsencrypt/live/$EDGE_TLS_DOMAIN exists, build
                      the HAProxy PEM from it (fullchain + key) and replace
                      /certs/edge.pem atomically when it differs. Then make
                      sure the RUNNING edge serves what is on disk: compare
                      the SHA-1 fingerprint `show ssl cert` reports with the
                      file's, and when they differ push the file through the
                      runtime API (`set ssl cert` + `commit ssl cert`), no
                      restart. Also certbot's --deploy-hook ($RENEWED_LINEAGE
                      is set there; a lineage other than EDGE_TLS_DOMAIN's
                      is ignored).
  status              What is on disk vs what the edge serves.

The file and the runtime push are both needed: the push updates the running
workers, and a later `make up edge` reload (USR2) or recreate re-reads the
file from disk.

The runtime API is HAProxy's stats socket at /run/haproxy-api/admin.sock,
shared with the edge through the `edge_api` volume (haproxy.cfg `global`).
No docker.sock: sandbox-runner stays the only container that mounts it.

Never prints secrets: the Cloudflare token is not in this process's view
(entrypoint.sh unsets it before certbot runs), and the PEM's private key is
only ever written to the file and the socket.

See ai/edge/EDGE.md § TLS.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

PEM_PATH = os.environ.get("EDGE_CERTS_PEM", "/certs/edge.pem")
# The name HAProxy knows the certificate by: the `crt` path in haproxy.cfg.
# The edge mounts the same volume at the same path, so it equals PEM_PATH
# in production; tests run HAProxy elsewhere and override them separately.
HAPROXY_CERT_NAME = os.environ.get("EDGE_CERTS_HAPROXY_NAME", "/certs/edge.pem")
SOCKET_PATH = os.environ.get("EDGE_CERTS_SOCKET", "/run/haproxy-api/admin.sock")
LIVE_DIR = os.environ.get("EDGE_CERTS_LIVE_DIR", "/etc/letsencrypt/live")
# Group of the `haproxy` user the official haproxy:*-alpine image runs as
# (/etc/group: haproxy:x:99:). The PEM holds the private key, so it is
# 0640 root:99 -- readable by HAProxy, by nobody else.
HAPROXY_GID = int(os.environ.get("HAPROXY_GID", "99"))
PLACEHOLDER_DAYS = 3650
CLI_TIMEOUT_S = 10.0

_BLOCK = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)
_FINGERPRINT = re.compile(r"^SHA1 FingerPrint:\s*([0-9A-Fa-f]+)\s*$", re.M)


class HelperError(Exception):
    """Something the operator has to look at; printed as one line."""


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} edge-certs: {message}", flush=True)


# ── PEM handling ────────────────────────────────────────────────────────────


def pem_blocks(data: bytes) -> list:
    """[(label, base64 body without whitespace)] for every PEM block in data."""
    out = []
    for match in _BLOCK.finditer(data):
        body = b"".join(match.group(2).split())
        try:
            base64.b64decode(body, validate=True)
        except binascii.Error as exc:
            raise HelperError(f"a {match.group(1).decode()} block is not valid base64") from exc
        out.append((match.group(1).decode(), body))
    return out


def _format_block(label: str, body: bytes) -> bytes:
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return b"".join([f"-----BEGIN {label}-----\n".encode(), b"\n".join(lines), f"\n-----END {label}-----\n".encode()])


def build_pem(fullchain: bytes, privkey: bytes) -> bytes:
    """HAProxy's `crt` format: the certificate chain, then the private key.

    Rebuilt from the PEM blocks alone, so the result has no blank line --
    the runtime API ends a payload at the first empty line.
    """
    certs = [b for b in pem_blocks(fullchain) if b[0] == "CERTIFICATE"]
    keys = [b for b in pem_blocks(privkey) if b[0].endswith("PRIVATE KEY")]
    if not certs:
        raise HelperError("no CERTIFICATE block in the certificate file")
    if len(keys) != 1:
        raise HelperError(f"expected one PRIVATE KEY block in the key file, found {len(keys)}")
    return b"".join(_format_block(label, body) for label, body in certs + keys)


def fingerprint(pem: bytes) -> str:
    """SHA-1 of the first (leaf) certificate's DER, uppercase hex -- what `show ssl cert` prints."""
    certs = [body for label, body in pem_blocks(pem) if label == "CERTIFICATE"]
    if not certs:
        raise HelperError("no CERTIFICATE block")
    return hashlib.sha1(base64.b64decode(certs[0])).hexdigest().upper()


def write_atomic(path: str, data: bytes, gid=None) -> None:
    """Replace path with data in one rename, so HAProxy never reads half a file."""
    gid = HAPROXY_GID if gid is None else gid
    fd, tmp = tempfile.mkstemp(prefix=".edge-certs.", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o640)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(tmp, 0, gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_or_none(path: str):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


# ── HAProxy runtime API ─────────────────────────────────────────────────────


def cli_message(command: str, payload=None) -> bytes:
    """One CLI request. A payload follows `<<` on the next lines and ends at an empty line."""
    if payload is None:
        return f"{command}\n".encode()
    text = payload.decode("ascii") if isinstance(payload, bytes) else payload
    body = text.rstrip("\n")
    if any(not line.strip() for line in body.split("\n")):
        raise HelperError("payload contains an empty line; the runtime API would cut it there")
    return f"{command} <<\n{body}\n\n".encode()


def cli(command: str, payload=None, sock_path=None, timeout: float = CLI_TIMEOUT_S) -> str:
    """Send one command (non-interactive mode: HAProxy answers, then closes)."""
    sock_path = sock_path or SOCKET_PATH
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(sock_path)
        sock.sendall(cli_message(command, payload))
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def loaded_fingerprint(name=None, sock_path=None):
    """SHA-1 fingerprint of the certificate HAProxy serves as <name>, or None if it has none."""
    name = name or HAPROXY_CERT_NAME
    match = _FINGERPRINT.search(cli(f"show ssl cert {name}", sock_path=sock_path))
    return match.group(1).upper() if match else None


def push(pem: bytes, name=None, sock_path=None) -> str:
    """Replace the certificate the running HAProxy serves as <name>; returns `show ssl cert`."""
    name = name or HAPROXY_CERT_NAME
    out = cli(f"set ssl cert {name}", pem, sock_path=sock_path)
    if "Transaction created" not in out and "Transaction updated" not in out:
        _abort(name, sock_path)
        raise HelperError(f"HAProxy refused `set ssl cert {name}`: {' '.join(out.split())}")
    out = cli(f"commit ssl cert {name}", sock_path=sock_path)
    if "Success!" not in out:
        _abort(name, sock_path)
        raise HelperError(f"HAProxy refused `commit ssl cert {name}`: {' '.join(out.split())}")
    shown = cli(f"show ssl cert {name}", sock_path=sock_path)
    match = _FINGERPRINT.search(shown)
    if not match or match.group(1).upper() != fingerprint(pem):
        raise HelperError(f"after the commit HAProxy still does not serve the new certificate as {name}")
    return shown


def _abort(name: str, sock_path) -> None:
    try:
        cli(f"abort ssl cert {name}", sock_path=sock_path)
    except OSError:
        pass


def _summary(shown: str) -> str:
    """The lines of `show ssl cert` an operator wants in a log line (empty ones skipped:
    Let's Encrypt leaves the subject empty and names the host in the SAN)."""
    keep = ("Subject Alternative Name:", "Subject:", "Issuer:", "notAfter:")
    lines = (line.strip() for line in shown.splitlines())
    return "; ".join(line for line in lines if line.startswith(keep) and line.split(":", 1)[1].strip())


# ── commands ────────────────────────────────────────────────────────────────


def placeholder(name: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9.-]+", name):
        raise HelperError(f"placeholder name {name!r} is not a hostname")
    with tempfile.TemporaryDirectory() as tmp:
        crt, key = os.path.join(tmp, "crt.pem"), os.path.join(tmp, "key.pem")
        result = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
             "-noenc", "-days", str(PLACEHOLDER_DAYS), "-subj", f"/CN={name}",
             "-addext", f"subjectAltName=DNS:{name}", "-keyout", key, "-out", crt],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise HelperError(f"openssl could not make the placeholder: {result.stderr.strip()}")
        with open(crt, "rb") as c, open(key, "rb") as k:
            pem = build_pem(c.read(), k.read())
    write_atomic(PEM_PATH, pem)
    log(f"wrote a self-signed placeholder for {name} to {PEM_PATH}")
    return 0


def sync() -> int:
    domain = os.environ.get("EDGE_TLS_DOMAIN", "").strip()
    renewed = os.environ.get("RENEWED_LINEAGE", "")
    if renewed and os.path.basename(renewed.rstrip("/")) != domain:
        log(f"ignoring renewed certificate {renewed}: EDGE_TLS_DOMAIN is {domain or '(empty)'}")
        return 0

    current = read_or_none(PEM_PATH)
    lineage = os.path.join(LIVE_DIR, domain) if domain else ""
    fullchain = read_or_none(os.path.join(lineage, "fullchain.pem")) if lineage else None
    if fullchain is not None:
        privkey = read_or_none(os.path.join(lineage, "privkey.pem"))
        if privkey is None:
            raise HelperError(f"{lineage} has fullchain.pem but no privkey.pem")
        pem = build_pem(fullchain, privkey)
        if pem != current:
            write_atomic(PEM_PATH, pem)
            log(f"wrote the certificate for {domain} to {PEM_PATH} (fingerprint {fingerprint(pem)})")
            current = pem
    if current is None:
        raise HelperError(f"{PEM_PATH} does not exist")

    want = fingerprint(current)
    try:
        loaded = loaded_fingerprint()
    except OSError as exc:
        log(f"the edge's runtime API ({SOCKET_PATH}) is not reachable ({exc.__class__.__name__}); "
            f"the edge loads {PEM_PATH} from disk when it starts or reloads")
        return 0
    if loaded == want:
        return 0
    shown = push(current)
    log(f"the running edge now serves {HAPROXY_CERT_NAME} with fingerprint {want} "
        f"(was {loaded or 'none'}): {_summary(shown)}")
    return 0


def status() -> int:
    current = read_or_none(PEM_PATH)
    print(f"on disk: {PEM_PATH} " + (f"fingerprint {fingerprint(current)}" if current else "MISSING"))
    try:
        shown = cli(f"show ssl cert {HAPROXY_CERT_NAME}")
    except OSError as exc:
        print(f"edge:    runtime API {SOCKET_PATH} not reachable ({exc.__class__.__name__})")
        return 1
    print("edge:    " + shown.strip().replace("\n", "\n         "))
    match = _FINGERPRINT.search(shown)
    same = bool(current and match and match.group(1).upper() == fingerprint(current))
    print("match:   " + ("yes, the edge serves the file on disk" if same else "NO -- run `python3 /edge-certs/edge_certs.py sync`"))
    return 0 if same else 1


def main(argv: list) -> int:
    try:
        if len(argv) == 2 and argv[0] == "placeholder":
            return placeholder(argv[1])
        if argv == ["sync"]:
            return sync()
        if argv == ["status"]:
            return status()
        print(__doc__, file=sys.stderr)
        return 2
    except HelperError as exc:
        log(f"ERROR: {exc}")
        return 1
    except OSError as exc:
        log(f"ERROR: {exc.__class__.__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
