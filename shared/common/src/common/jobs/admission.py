"""
Bounded admission — the refusal both persistent registries raise when a
``register(..., max_open=N)`` would take the queue past N unfinished jobs.

A registry that is also a work queue (``claim_next``) has no natural end to
its line: every submit is a new row, and a burst waits out the whole backlog
behind it. ``PostgresRegistry.register`` and ``SqliteRegistry.register`` take
an optional ``max_open``: the count of OPEN jobs — every row whose phase is
not terminal (``completed`` / ``failed`` / ``cancelled``), so waiting AND
running, and a consumer's own phases (the classifier's ``staging``, the
interceptor's custom ones) too — and the INSERT run as one atomic step, and a
count already at ``max_open`` raises :class:`QueueFull` with no row written.
``count_open()`` is the same count without the atomicity, for a cheap
pre-check before a caller does expensive work for a submit that would be
refused anyway.

``QueueFull`` is framework-free on purpose (``common`` has no web-framework
dependency): the service maps it to its own answer — the classifier's is a
429 with a ``Retry-After`` it estimates from its recent job durations.

Stdlib only, so ``common.jobs`` can export it without either backend's driver.
"""

from __future__ import annotations

from typing import Optional


class QueueFull(Exception):
    """A ``register(..., max_open=...)`` refused: ``open_jobs`` unfinished
    jobs already exist, at or past ``limit``. Nothing was written."""

    def __init__(self, open_jobs: int, limit: int) -> None:
        self.open_jobs = int(open_jobs)
        self.limit = int(limit)
        super().__init__(
            f"queue full: {self.open_jobs} job(s) waiting or running, the limit is {self.limit}"
        )


def check_max_open(max_open: Optional[int]) -> Optional[int]:
    """A ``register``'s ``max_open`` as given, or None for an unbounded one.

    Refuses a non-integer (a bool included — ``True`` would read as a limit
    of 1) and anything below 1: a limit of 0 would refuse every submit, which
    is never what a caller meant — "no limit" is ``None``. The same checks for
    both backends.
    """
    if max_open is None:
        return None
    if isinstance(max_open, bool) or not isinstance(max_open, int):
        raise TypeError(f"max_open takes an int (or None for no limit), not {max_open!r}")
    if max_open < 1:
        raise ValueError(
            f"max_open must be at least 1, not {max_open}: None means no limit"
        )
    return max_open
