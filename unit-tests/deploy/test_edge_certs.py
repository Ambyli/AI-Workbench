"""Tests for ai/edge/certs/edge_certs.py -- the edge-certs helper that builds
the HAProxy PEM, writes it atomically and pushes it through the runtime API.

The runtime API is faked (a stand-in for `cli`), so no HAProxy and no UNIX
socket are needed; the same code was exercised against the haproxy 3.4.6
binary when it was written (EDGE.md § TLS, "How this was verified").

    uv run --no-sync pytest unit-tests/deploy -q
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("edge_certs", _ROOT / "ai" / "edge" / "certs" / "edge_certs.py")
ec = importlib.util.module_from_spec(_spec)
sys.modules["edge_certs"] = ec
_spec.loader.exec_module(ec)


def block(label: str, payload: bytes, width: int = 64) -> bytes:
    b64 = base64.b64encode(payload).decode()
    lines = "\n".join(b64[i:i + width] for i in range(0, len(b64), width))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n".encode()


LEAF, INTER, KEY = b"leaf-der" * 20, b"intermediate-der" * 20, b"key-der" * 20
FULLCHAIN = block("CERTIFICATE", LEAF) + b"\n" + block("CERTIFICATE", INTER, width=76)
PRIVKEY = b"\n" + block("PRIVATE KEY", KEY) + b"\n\n"


# ── PEM handling ────────────────────────────────────────────────────────────


def test_build_pem_is_chain_then_key_with_no_blank_line():
    pem = ec.build_pem(FULLCHAIN, PRIVKEY)
    labels = [label for label, _ in ec.pem_blocks(pem)]
    assert labels == ["CERTIFICATE", "CERTIFICATE", "PRIVATE KEY"]
    text = pem.decode()
    assert "\n\n" not in text and text.endswith("-----END PRIVATE KEY-----\n")
    assert all(len(line) <= 64 for line in text.splitlines())
    # The DER content survives the re-wrapping.
    assert [base64.b64decode(b) for _, b in ec.pem_blocks(pem)] == [LEAF, INTER, KEY]


def test_build_pem_accepts_ec_and_rsa_key_labels():
    for label in ("EC PRIVATE KEY", "RSA PRIVATE KEY"):
        pem = ec.build_pem(FULLCHAIN, block(label, KEY))
        assert ec.pem_blocks(pem)[-1][0] == label


@pytest.mark.parametrize("fullchain,privkey,needle", [
    (b"", PRIVKEY, "no CERTIFICATE"),
    (FULLCHAIN, b"", "found 0"),
    (FULLCHAIN, PRIVKEY + PRIVKEY, "found 2"),
    (FULLCHAIN, b"-----BEGIN PRIVATE KEY-----\n!!notbase64!!\n-----END PRIVATE KEY-----\n", "not valid base64"),
])
def test_build_pem_refuses_incomplete_input(fullchain, privkey, needle):
    with pytest.raises(ec.HelperError, match=needle):
        ec.build_pem(fullchain, privkey)


def test_fingerprint_is_sha1_of_the_leaf_der_like_show_ssl_cert():
    pem = ec.build_pem(FULLCHAIN, PRIVKEY)
    assert ec.fingerprint(pem) == hashlib.sha1(LEAF).hexdigest().upper()


def test_write_atomic_replaces_the_file_and_leaves_no_temp(tmp_path):
    path = tmp_path / "edge.pem"
    path.write_bytes(b"old")
    ec.write_atomic(str(path), b"new")
    assert path.read_bytes() == b"new"
    assert [p.name for p in tmp_path.iterdir()] == ["edge.pem"]


# ── runtime API framing ─────────────────────────────────────────────────────


def test_cli_message_without_payload_is_one_line():
    assert ec.cli_message("show ssl cert /certs/edge.pem") == b"show ssl cert /certs/edge.pem\n"


def test_cli_message_payload_follows_double_angle_and_ends_at_an_empty_line():
    pem = ec.build_pem(FULLCHAIN, PRIVKEY)
    msg = ec.cli_message("set ssl cert /certs/edge.pem", pem).decode()
    head, body = msg.split("\n", 1)
    assert head == "set ssl cert /certs/edge.pem <<"
    assert body.endswith("-----END PRIVATE KEY-----\n\n")
    assert body.count("\n\n") == 1  # the terminator, and nothing earlier cuts the payload


def test_cli_message_refuses_a_payload_with_an_empty_line():
    with pytest.raises(ec.HelperError, match="empty line"):
        ec.cli_message("set ssl cert x", "a\n\nb\n")


# ── sync / push against a fake runtime API ──────────────────────────────────


SHOW = "Filename: /certs/edge.pem\nStatus: Used\nSHA1 FingerPrint: {fp}\nSubject: /CN={cn}\nIssuer: /CN=Test CA\nnotAfter: Jan  1 00:00:00 2027 GMT\n\n"


class FakeHAProxy:
    """Answers the CLI like haproxy 3.4.6 does (strings captured from the binary)."""

    def __init__(self, fp: str | None, refuse: str = ""):
        self.fp = fp
        self.pending = None
        self.refuse = refuse
        self.calls = []

    def __call__(self, command, payload=None, sock_path=None, timeout=None):
        self.calls.append(command)
        name = command.split()[-1]
        if command.startswith("show ssl cert"):
            return SHOW.format(fp=self.fp, cn="x") if self.fp else "Can't display the certificate: Not found!\n\n"
        if command.startswith("set ssl cert"):
            if self.refuse == "set":
                return f"unable to load certificate from file '{name}': wrong tag.\nCan't update {name}!\n\n"
            self.pending = ec.fingerprint(payload if isinstance(payload, bytes) else payload.encode())
            return f"Transaction created for certificate {name}!\n\n"
        if command.startswith("commit ssl cert"):
            if self.refuse == "commit":
                return "Can't commit: something\n\n"
            self.fp, self.pending = self.pending, None
            return f"Committing {name}.\nSuccess!\n\n"
        if command.startswith("abort ssl cert"):
            self.pending = None
            return "Transaction aborted for certificate!\n\n"
        raise AssertionError(command)


@pytest.fixture
def env(tmp_path, monkeypatch):
    live = tmp_path / "live"
    (live / "ai.example").mkdir(parents=True)
    (live / "ai.example" / "fullchain.pem").write_bytes(FULLCHAIN)
    (live / "ai.example" / "privkey.pem").write_bytes(PRIVKEY)
    certs = tmp_path / "certs"
    certs.mkdir()
    monkeypatch.setattr(ec, "PEM_PATH", str(certs / "edge.pem"))
    monkeypatch.setattr(ec, "LIVE_DIR", str(live))
    monkeypatch.setenv("EDGE_TLS_DOMAIN", "ai.example")
    monkeypatch.delenv("RENEWED_LINEAGE", raising=False)
    return tmp_path


WANT = hashlib.sha1(LEAF).hexdigest().upper()


def test_sync_writes_the_lineage_and_pushes_it(env, monkeypatch, capsys):
    haproxy = FakeHAProxy(fp="AAAA0000BBBB1111")
    monkeypatch.setattr(ec, "cli", haproxy)
    assert ec.sync() == 0
    assert (env / "certs" / "edge.pem").read_bytes() == ec.build_pem(FULLCHAIN, PRIVKEY)
    assert haproxy.fp == WANT
    assert [c.split()[0] for c in haproxy.calls] == ["show", "set", "commit", "show"]
    out = capsys.readouterr().out
    assert "now serves /certs/edge.pem" in out and "was AAAA0000BBBB1111" in out
    assert "PRIVATE KEY" not in out


def test_sync_is_a_no_op_when_disk_and_edge_already_agree(env, monkeypatch, capsys):
    (env / "certs" / "edge.pem").write_bytes(ec.build_pem(FULLCHAIN, PRIVKEY))
    haproxy = FakeHAProxy(fp=WANT)
    monkeypatch.setattr(ec, "cli", haproxy)
    assert ec.sync() == 0
    assert haproxy.calls == ["show ssl cert /certs/edge.pem"]
    assert capsys.readouterr().out == ""


def test_sync_pushes_the_file_on_disk_when_the_edge_serves_something_else(env, monkeypatch):
    """No lineage (TLS off, or not issued yet): the edge must still serve the file."""
    monkeypatch.setenv("EDGE_TLS_DOMAIN", "")
    (env / "certs" / "edge.pem").write_bytes(ec.build_pem(FULLCHAIN, PRIVKEY))
    haproxy = FakeHAProxy(fp="CCCC2222")
    monkeypatch.setattr(ec, "cli", haproxy)
    assert ec.sync() == 0 and haproxy.fp == WANT


def test_sync_ignores_a_renewed_lineage_for_another_name(env, monkeypatch, capsys):
    monkeypatch.setenv("RENEWED_LINEAGE", "/etc/letsencrypt/live/old.example")
    haproxy = FakeHAProxy(fp="X")
    monkeypatch.setattr(ec, "cli", haproxy)
    assert ec.sync() == 0
    assert haproxy.calls == [] and not (env / "certs" / "edge.pem").exists()
    assert "ignoring renewed certificate" in capsys.readouterr().out


def test_sync_with_the_edge_down_still_writes_the_file(env, monkeypatch, capsys):
    def unreachable(*a, **k):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(ec, "cli", unreachable)
    assert ec.sync() == 0
    assert (env / "certs" / "edge.pem").exists()
    assert "loads" in capsys.readouterr().out


@pytest.mark.parametrize("refuse", ["set", "commit"])
def test_a_refused_push_aborts_the_transaction_and_fails(env, monkeypatch, refuse):
    haproxy = FakeHAProxy(fp="DDDD3333", refuse=refuse)
    monkeypatch.setattr(ec, "cli", haproxy)
    assert ec.main(["sync"]) == 1
    assert haproxy.calls[-1].startswith("abort ssl cert")
    assert haproxy.fp == "DDDD3333"


def test_main_usage_and_placeholder_name_check(capsys):
    assert ec.main([]) == 2
    assert ec.main(["placeholder", "bad/name"]) == 1
    assert "is not a hostname" in capsys.readouterr().out
