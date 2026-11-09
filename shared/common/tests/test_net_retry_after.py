"""Tests for common.net.retry_after_s — a Retry-After header as a sleep.

Both RFC 9110 spellings (delay-seconds and an HTTP-date), the fallbacks for a
missing or unreadable header, a date in the past, and the cap — which also
bounds the default. Stdlib only; the clock is pinned with ``now``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from common.net import retry_after_s

NOW = datetime(2026, 11, 6, 15, 4, 5, tzinfo=timezone.utc)


def _http_date(when: datetime) -> str:
    return when.strftime("%a, %d %b %Y %H:%M:%S GMT")


@pytest.mark.parametrize("value, expected", [
    ("14", 14.0),
    ("0", 0.0),
    (" 7 ", 7.0),
    ("1.5", 1.5),
    ("60", 60.0),
])
def test_delay_seconds(value, expected):
    assert retry_after_s(value, cap=600) == expected


def test_an_http_date_is_that_instant_minus_now():
    assert retry_after_s(_http_date(NOW + timedelta(seconds=30)), now=NOW) == 30.0
    # The obsolete RFC 850 and asctime spellings HTTP still allows.
    assert retry_after_s("Friday, 06-Nov-26 15:04:35 GMT", now=NOW) == 30.0
    assert retry_after_s("Fri Nov  6 15:04:35 2026", now=NOW) == 30.0
    # A naive `now` is read as UTC.
    assert retry_after_s(_http_date(NOW + timedelta(seconds=9)), now=NOW.replace(tzinfo=None)) == 9.0


def test_a_date_in_the_past_is_no_wait():
    assert retry_after_s(_http_date(NOW - timedelta(minutes=5)), now=NOW) == 0.0


@pytest.mark.parametrize("value", [None, "", "   ", "soon", "-5", "+5", "1e3", "inf", "nan",
                                   "14 s", "Fri, 99 Nov 2026 25:61:00 GMT"])
def test_missing_or_unreadable_is_the_default(value):
    assert retry_after_s(value, default=10) == 10.0
    assert retry_after_s(value, default=3.5) == 3.5


def test_the_cap_bounds_every_answer_the_default_included():
    assert retry_after_s("3600", cap=60) == 60.0
    assert retry_after_s(_http_date(NOW + timedelta(hours=2)), cap=60, now=NOW) == 60.0
    assert retry_after_s(None, default=90, cap=60) == 60.0
    assert retry_after_s("14", cap=0) == 0.0
    # The defaults: 10 s when absent, at most 300 s.
    assert retry_after_s(None) == 10.0
    assert retry_after_s("100000") == 300.0


def test_a_negative_default_or_cap_never_gives_a_negative_wait():
    assert retry_after_s(None, default=-4) == 0.0
    assert retry_after_s("5", cap=-1) == 0.0
