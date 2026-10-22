"""common.net.locality — a host must resolve ONLY to this box's networks.

The guard ``common.signals`` runs before it sends conversation text to the
sidecar. DNS is stubbed (a resolver function, or a fake ``getaddrinfo``),
so nothing here touches the network; the refusal paths matter most: a
public address, a mix of private and public, a name that does not resolve.

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_net_locality.py -q
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from common.net import LocalityVerdict, check_locality, locality_problem, resolve_host, resolves_locally


def answers(*addresses):
    return lambda host: list(addresses)


@pytest.mark.parametrize("addresses", [
    ["172.18.0.5"], ["10.1.2.3"], ["192.168.5.240"], ["127.0.0.1"], ["::1"], ["fe80::1%eth0"],
    ["fd00::7"], ["169.254.1.1"], ["100.64.0.9"], ["172.18.0.5", "172.18.0.6"],
])
def test_private_addresses_are_local(addresses):
    assert locality_problem("signals", answers(*addresses)) is None
    assert resolves_locally("signals", answers(*addresses)) is True


@pytest.mark.parametrize("addresses", [["93.184.216.34"], ["172.18.0.5", "8.8.8.8"], ["2606:4700::1111"]])
def test_any_public_address_is_refused_without_naming_it(addresses):
    problem = locality_problem("signals.example.com", answers(*addresses))
    assert problem == "host 'signals.example.com' resolves to a public address"
    assert not any(a in problem for a in addresses)
    assert resolves_locally("signals.example.com", answers(*addresses)) is False


def test_resolution_failures():
    def gone(host):
        raise socket.gaierror(-2, "Name or service not known")

    assert locality_problem("signals", gone) == "host 'signals' could not be resolved (gaierror)"
    assert locality_problem("signals", answers()) == "host 'signals' resolved to no address"
    assert locality_problem("signals", answers("not-an-ip")) == "host 'signals' resolved to an unusable address"
    assert locality_problem("", answers("10.0.0.1")) == "no host"
    assert locality_problem(None, answers("10.0.0.1")) == "no host"  # type: ignore[arg-type]


def test_check_locality_says_whether_the_host_resolved():
    # A caller caching verdicts keeps an unresolved one only briefly: a
    # container that is restarting does not resolve, and then does.
    def gone(host):
        raise socket.gaierror(-2, "Name or service not known")

    assert check_locality("signals", answers("172.18.0.5")) == LocalityVerdict(None, True)
    assert check_locality("signals", gone) == LocalityVerdict("host 'signals' could not be resolved (gaierror)",
                                                              False)
    assert check_locality("signals", answers()) == LocalityVerdict("host 'signals' resolved to no address", False)
    for addresses in (["93.184.216.34"], ["not-an-ip"]):
        verdict = check_locality("signals", answers(*addresses))
        assert verdict.resolved and verdict.problem == locality_problem("signals", answers(*addresses))
    assert check_locality("", answers("10.0.0.1")) == LocalityVerdict("no host", True)


def test_a_longer_network_list_can_narrow_it():
    only_docker = [ipaddress.ip_network("172.16.0.0/12")]
    assert locality_problem("x", answers("172.18.0.5"), networks=only_docker) is None
    assert locality_problem("x", answers("10.0.0.5"), networks=only_docker) is not None


def test_the_default_resolver_uses_getaddrinfo(monkeypatch):
    def fake(host, port, *args, **kwargs):
        if host != "signals":
            raise socket.gaierror(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.9", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.5", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert resolve_host("signals") == ["172.18.0.5", "172.18.0.9"]
    assert locality_problem("signals") is None
    assert locality_problem("elsewhere") == "host 'elsewhere' could not be resolved (gaierror)"
