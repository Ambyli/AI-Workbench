"""Read a ``Retry-After`` header — how long a 429 or a 503 asks a client to wait.

A service that refuses work for now — the classifier's full queue answers
``429``, the edge answers ``503`` while a recreate-class backend restarts —
says when to come back in ``Retry-After``, and every client that resubmits
has to turn that header into a sleep. RFC 9110 § 10.2.3 allows two spellings,
and a client that only handles the common one breaks on the other:

  * ``delay-seconds`` — a non-negative integer, ``Retry-After: 14``. A decimal
    (``1.5``) is accepted as well; nothing standard sends one, but nothing is
    gained by refusing it.
  * ``HTTP-date`` — ``Retry-After: Fri, 06 Nov 2026 15:04:05 GMT``: the wait
    is that instant minus now (``email.utils``' parser, which also reads the
    obsolete RFC 850 / asctime forms; a date with no zone is UTC, as HTTP
    dates are).

A header that is missing, empty or neither gives ``default``; a date already
past gives 0; the answer is never more than ``cap``, so a misbehaving server
cannot park a batch job for an hour, and never negative.

Stdlib only (the ``net`` package's pure part, like ``validate_url``).

Used by the in-repo batch tools that submit to the classifier
(``ai/classifier/bin/reference_box_assess.py``,
``unit-tests/classifier/regions_report.py``), which wait out a full queue
and resubmit.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

# delay-seconds, plus the decimal leniency described above. No sign, no
# exponent, no "inf" / "nan": float() would accept all of those.
_SECONDS = re.compile(r"\d+(?:\.\d+)?")


def retry_after_s(
    value: Optional[str],
    *,
    default: float = 10.0,
    cap: float = 300.0,
    now: Optional[datetime] = None,
) -> float:
    """Seconds to wait before retrying, from a ``Retry-After`` header value.

    Args:
        value: The header as received (``response.headers.get("Retry-After")``),
            or None when there was none.
        default: The wait when ``value`` is missing, empty or unreadable.
        cap: The most this ever returns (``default`` included).
        now: The current instant, for an HTTP-date (tests pin it); a naive
            one is read as UTC. Defaults to the clock.

    Returns:
        A float in ``[0, cap]``. Never raises for a bad header.
    """
    cap = max(0.0, float(cap))
    fallback = min(max(0.0, float(default)), cap)
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    if _SECONDS.fullmatch(text):
        return min(float(text), cap)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return fallback
    if when is None:  # Python < 3.10 returned None for an unparseable date
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return min(max(0.0, (when - now).total_seconds()), cap)
