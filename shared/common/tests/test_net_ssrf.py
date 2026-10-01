"""Tests for common.net.ssrf.validate_url.

This is a security control, so the tests lean on the REFUSAL paths: every
private range, every scheme that is not http(s), a hostname that resolves to
a mix of public and private addresses, and an IPv6 literal with a scope
suffix. DNS is stubbed — a test suite must not depend on what the network
says today, and stubbing is the only way to exercise "this name resolves to
10.0.0.5" without owning a domain that does.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from common.net import DEFAULT_BLOCKED_NETWORKS, BlockedURLError, validate_url


def _stub_dns(monkeypatch, mapping: dict[str, list[str]]):
    """Make getaddrinfo answer from ``mapping`` and raise for anything else."""

    def fake(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))
            for addr in mapping[host]
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


# ── schemes ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "redis://cache:6379",
        "//example.com/no-scheme",
    ],
)
def test_non_http_schemes_are_refused(url, monkeypatch):
    _stub_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    with pytest.raises(BlockedURLError):
        validate_url(url)


def test_missing_hostname_is_refused():
    with pytest.raises(BlockedURLError, match="missing hostname"):
        validate_url("http:///just-a-path")


# ── the blocklist ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",         # RFC1918
        "172.16.0.1",       # RFC1918
        "192.168.1.1",      # RFC1918
        "127.0.0.1",        # loopback
        "169.254.169.254",  # link-local — the cloud metadata endpoint
        "0.0.0.0",          # "this" network
        "100.64.0.1",       # RFC6598 shared address space
    ],
)
def test_private_addresses_are_refused(address, monkeypatch):
    _stub_dns(monkeypatch, {"evil.test": [address]})
    with pytest.raises(BlockedURLError, match="blocked network"):
        validate_url("http://evil.test/x")


def test_a_public_address_passes(monkeypatch):
    _stub_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    validate_url("https://example.com/image.png")


def test_every_resolved_address_must_clear_the_list(monkeypatch):
    """A name that answers with one public and one private address must not
    pass on the strength of the public one — that is the whole DNS-rebinding
    shape, and gethostbyname (which returns only the first) would miss it."""
    _stub_dns(monkeypatch, {"mixed.test": ["93.184.216.34", "10.0.0.5"]})
    with pytest.raises(BlockedURLError, match="blocked network"):
        validate_url("http://mixed.test/x")


def test_unresolvable_hostname_is_refused(monkeypatch):
    _stub_dns(monkeypatch, {})
    with pytest.raises(BlockedURLError, match="could not be resolved"):
        validate_url("http://nope.invalid/x")


# ── IPv6 ───────────────────────────────────────────────────────────────────
def test_ipv6_loopback_and_unique_local_are_refused(monkeypatch):
    for address in ("::1", "fc00::1", "fe80::1%eth0"):
        _stub_dns(monkeypatch, {"six.test": [address]})
        with pytest.raises(BlockedURLError):
            validate_url("http://six.test/x")


def test_ipv6_literal_url_resolves_through_the_same_path(monkeypatch):
    _stub_dns(monkeypatch, {"::1": ["::1"]})
    with pytest.raises(BlockedURLError):
        validate_url("http://[::1]:8000/x")


# ── caller-supplied network list ───────────────────────────────────────────
def test_a_caller_may_fence_off_more(monkeypatch):
    _stub_dns(monkeypatch, {"public.test": ["93.184.216.34"]})
    validate_url("http://public.test/x")
    extra = list(DEFAULT_BLOCKED_NETWORKS) + [ipaddress.ip_network("93.184.216.0/24")]
    with pytest.raises(BlockedURLError):
        validate_url("http://public.test/x", extra)


def test_an_empty_list_blocks_nothing_but_still_checks_the_scheme(monkeypatch):
    """Passing [] is how a consumer disables the blocklist. It is a foot-gun
    and the docstring says so, but it must behave predictably: the address
    check goes away, the scheme and resolve checks do not."""
    _stub_dns(monkeypatch, {"local.test": ["127.0.0.1"]})
    validate_url("http://local.test/x", [])
    with pytest.raises(BlockedURLError):
        validate_url("file://local.test/x", [])
