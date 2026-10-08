"""Model usage records: one row per vision-model request, linked to its job.

The Prometheus counters ``llm.client`` keeps are process-wide and labelled by
call KIND only — they answer "where does the model's time go", never "what did
job abc123 cost". This module answers the second question, durably:

    job_id_var / job_type_var  which job made the call. Set once per job by
                               ``jobs.queue.ClassifierQueue.handle_job``.
    unit_var                   which unit of that job: ``{"criterion", "item",
                               "document"}``. Set by ``analysis.scheduler`` —
                               item and document in ``run_item`` BEFORE the
                               ``references: "auto"`` selection call (which
                               serves every criterion on the item, so it
                               carries the item and no criterion), and the
                               criterion too in ``_evaluate`` /
                               ``_evaluate_document``.
    unit_scope()               the context manager the scheduler sets
                               ``unit_var`` with.
    UsageStore                 the ``llm_calls`` table, in the SAME
                               ``classifier-db`` Postgres as the jobs table.
                               asyncpg, through the one pool ``db.database``
                               owns, exactly like
                               ``references.store.ReferenceRegistry``.
    usage_store                the process-wide instance. Every reader looks
                               it up on THIS module at call time, so a test
                               can swap it for one of its own.
    record_call()              what ``llm.client._post`` calls once per request,
                               ok or error, after the request's ``LLM_CALLS``
                               slot is released. NEVER raises.

Why ContextVars rather than arguments: the call sites that reach ``_post`` —
the scoring call, the box loop's ask / refine / verify, the selection and the
describe call — are four modules deep below the scheduler, and threading a
job id through every one of them would touch every signature for a concern
none of them has. asyncio copies the current context into every child task
(``gather``, ``create_task``, ``to_thread``), and every scheduler unit is its
own task under ``gather``, so a value set once at the top of a job or a unit
reaches every call underneath it — and two concurrent units can never see each
other's. A call made outside a job (none today) is still recorded, with a
NULL ``job_id``.

**Retention: the rows outlive the job.** The artifact sweeper prunes job rows
and directories past JOB_TTL_HOURS and never touches ``llm_calls`` — a cost
record is worth more after the job is gone than while it is fresh. The only
thing that removes a job's rows is ``DELETE /jobs/{id}`` (``main`` wires
:meth:`UsageStore.delete_job` into the jobs router's ``on_delete`` hook).

**Accounting can never fail a model call.** ``record_call`` catches
everything — building the row, reaching the database, the insert — logs a
warning and counts it in ``classifier_llm_usage_write_errors_total``. The
write happens after the slot is released, so a slow database never holds a
model slot either.

The ``usage`` column (``JSONB``) keeps the response's ``usage`` object
verbatim, so whatever vLLM reports beyond the five token columns (multimodal
token counts, a future field) is not lost — and Trino / ``psql`` can read a
field of it without parsing text. ``started_at`` is a ``TIMESTAMPTZ``; the API
still hands it out as the ISO-8601 UTC string (microseconds, ``+00:00``) it
always did. Cached tokens are only reported when vLLM runs with
``--enable-prompt-tokens-details``; a token total is NULL (not 0) when no
call in it reported that number.

Process flow position: ``llm.client._post`` writes; ``api.usage``
(``GET /jobs/{id}/usage``, ``GET /usage``) and ``jobs.queue.handle_job`` (the
result's ``usage`` block) read; ``main`` creates the table and deletes a
job's rows with the job.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import asyncpg
import httpx

from config import VISION_LLM_API
from db import Database, create_schema, database
from logger import logger
from metrics import llm_usage_write_errors
from middleware import request_id_var

# ---------------------------------------------------------------------------
# Call context
# ---------------------------------------------------------------------------

job_id_var: ContextVar[Optional[str]] = ContextVar("classifier_llm_job_id", default=None)
job_type_var: ContextVar[Optional[str]] = ContextVar("classifier_llm_job_type", default=None)
# {"criterion": str | None, "item": int | None, "document": int | None}
unit_var: ContextVar[Optional[dict[str, Any]]] = ContextVar("classifier_llm_unit", default=None)


@contextmanager
def unit_scope(
    *,
    criterion: Optional[str] = None,
    item: Optional[int] = None,
    document: Optional[int] = None,
) -> Iterator[None]:
    """Attribute every model call inside the block to this unit.

    The whole unit is set, not merged into an outer one, so a caller says
    everything it knows: ``run_item`` sets item + document (its selection
    call has no criterion), ``_evaluate`` sets all three. Reset on exit, so
    nothing leaks into whatever the task runs next.
    """
    token = unit_var.set({"criterion": criterion, "item": item, "document": document})
    try:
        yield
    finally:
        unit_var.reset(token)


def now_iso() -> str:
    """UTC, always with microseconds — the format every timestamp this module
    hands out has, and the format ``api.usage`` normalises a ``since`` /
    ``until`` bound to."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _ts(value: Any) -> Optional[datetime]:
    """An ISO-8601 string (or a datetime) → an aware UTC datetime for a
    ``TIMESTAMPTZ`` parameter; a naive value is read as UTC. ``None`` passes
    through.

    Raises:
        ValueError: Not an ISO-8601 date or datetime.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    """A ``TIMESTAMPTZ`` as asyncpg returns it → :func:`now_iso`'s format."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return str(value)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

# Token columns are INTEGER (a per-call count never nears 2^31) so a SUM over
# them is a BIGINT — a Python int — rather than NUMERIC, which asyncpg would
# hand back as Decimal.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id                BIGSERIAL PRIMARY KEY,
    job_id            TEXT,
    job_type          TEXT,
    request_id        TEXT,
    started_at        TIMESTAMPTZ NOT NULL,
    label             TEXT,
    kind              TEXT NOT NULL,
    criterion         TEXT,
    item              INTEGER,
    document          INTEGER,
    attempt           INTEGER,
    model_requested   TEXT,
    model_reported    TEXT,
    api_url           TEXT,
    max_tokens        INTEGER,
    outcome           TEXT NOT NULL,
    http_status       INTEGER,
    error             TEXT,
    finish_reason     TEXT,
    seconds           DOUBLE PRECISION NOT NULL,
    prompt_tokens     INTEGER,
    cached_tokens     INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens  INTEGER,
    total_tokens      INTEGER,
    usage             JSONB
);
CREATE INDEX IF NOT EXISTS llm_calls_job ON llm_calls (job_id);
CREATE INDEX IF NOT EXISTS llm_calls_started ON llm_calls (started_at);
"""

# Insert order; every key of a row built by build_row, ``id`` aside.
COLUMNS: tuple[str, ...] = (
    "job_id", "job_type", "request_id", "started_at", "label", "kind",
    "criterion", "item", "document", "attempt", "model_requested",
    "model_reported", "api_url", "max_tokens", "outcome", "http_status",
    "error", "finish_reason", "seconds", "prompt_tokens", "cached_tokens",
    "completion_tokens", "reasoning_tokens", "total_tokens", "usage",
)

# The token columns, in the order every totals block lists them.
TOKEN_COLUMNS: tuple[str, ...] = (
    "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "total_tokens",
)

# One aggregate expression list, shared by every grouping so the job view,
# the per-kind / per-criterion / per-day breakdowns and GET /usage can never
# disagree on what "calls" or "seconds" means. SUM over a token column is
# NULL when no call in the group reported it — deliberately not 0.
_AGGREGATES = (
    "COUNT(*) AS calls, "
    "COUNT(*) FILTER (WHERE outcome = 'error') AS errors, "
    "COALESCE(SUM(seconds), 0) AS seconds, "
    + ", ".join(f"SUM({c}) AS {c}" for c in TOKEN_COLUMNS)
)

# The UTC calendar day of a call — GET /usage's per-day grouping. Explicitly
# UTC: date_trunc / ::date on a TIMESTAMPTZ use the SESSION time zone, which
# is whatever the server or the role was configured with.
_DAY = "to_char(started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')"

# A short error string: the class and the message, never a traceback.
_ERROR_MAX_CHARS = 500


def _count(value: Any) -> Optional[int]:
    """A non-negative int token count, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _block(row: asyncpg.Record) -> dict[str, Any]:
    """One aggregate row → the totals dict every view returns."""
    out: dict[str, Any] = {
        "calls": int(row["calls"]),
        "errors": int(row["errors"]),
        "seconds": round(float(row["seconds"] or 0.0), 3),
    }
    for column in TOKEN_COLUMNS:
        value = row[column]
        out[column] = int(value) if value is not None else None
    return out


def _call(row: asyncpg.Record) -> dict[str, Any]:
    """One ``llm_calls`` row → the API's call entry (``started_at`` as an ISO
    string, ``usage`` decoded)."""
    out = {key: row[key] for key in row.keys() if key != "usage"}
    out["started_at"] = _iso(row["started_at"])
    usage = row["usage"]
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            usage = None
    out["usage"] = usage if isinstance(usage, dict) else None
    return out


def build_row(
    *,
    label: str,
    kind: str,
    attempt: int,
    prompt: Any,
    data: Any,
    error: Optional[BaseException],
    seconds: float,
    started_at: str,
    counts: dict[str, Optional[int]],
) -> dict[str, Any]:
    """The ``llm_calls`` row for one request, attributed from the context vars.

    Args:
        label / kind / attempt: As ``_post`` has them.
        prompt:     The request body (``model``, ``max_tokens`` are read).
        data:       The response body, or None when the request failed.
        error:      The exception the request raised, or None.
        seconds:    Time inside the slot — the model, not the wait for one.
        started_at: When the request went out (:func:`now_iso`).
        counts:     ``llm.client.usage_counts(data)``.

    ``request_id`` is the correlation id of the HTTP request that queued the
    job (``handle_job`` restores it into ``middleware.request_id_var``);
    NULL outside one. ``usage`` is the response's ``usage`` object as a dict
    (stored as JSONB), or None.
    """
    unit = unit_var.get() or {}
    body = prompt if isinstance(prompt, dict) else {}
    response = data if isinstance(data, dict) else {}
    usage = response.get("usage")

    finish_reason = None
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        finish_reason = reason if isinstance(reason, str) else None

    http_status = None
    error_text = None
    if error is not None:
        if isinstance(error, httpx.HTTPStatusError):
            http_status = error.response.status_code
        error_text = f"{type(error).__name__}: {error}"[:_ERROR_MAX_CHARS]

    request_id = request_id_var.get()
    max_tokens = body.get("max_tokens", body.get("max_completion_tokens"))
    model_reported = response.get("model")
    return {
        "job_id": job_id_var.get(),
        "job_type": job_type_var.get(),
        "request_id": request_id if request_id and request_id != "-" else None,
        "started_at": started_at,
        "label": label,
        "kind": kind,
        "criterion": unit.get("criterion"),
        "item": unit.get("item"),
        "document": unit.get("document"),
        "attempt": attempt,
        "model_requested": body.get("model") if isinstance(body.get("model"), str) else None,
        "model_reported": model_reported if isinstance(model_reported, str) else None,
        "api_url": VISION_LLM_API,
        "max_tokens": _count(max_tokens),
        "outcome": "error" if error is not None else "ok",
        "http_status": http_status,
        "error": error_text,
        "finish_reason": finish_reason,
        "seconds": round(float(seconds), 6),
        "prompt_tokens": counts.get("prompt"),
        "cached_tokens": counts.get("cached"),
        "completion_tokens": counts.get("completion"),
        "reasoning_tokens": counts.get("reasoning"),
        "total_tokens": _count(usage.get("total_tokens")) if isinstance(usage, dict) else None,
        "usage": usage if isinstance(usage, dict) else None,
    }


class _Where:
    """A WHERE clause built from ``$n`` placeholders, numbered as clauses are
    added — asyncpg has no ``?``, and every reader here composes its filter
    piece by piece."""

    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.args: list[Any] = []

    def add(self, template: str, value: Any) -> "_Where":
        """``template`` holds one ``{}`` for this value's placeholder."""
        self.args.append(value)
        self.clauses.append(template.format(f"${len(self.args)}"))
        return self

    @property
    def sql(self) -> str:
        return " AND ".join(self.clauses) if self.clauses else "TRUE"


class UsageStore:
    """The ``llm_calls`` table.

    The table is created by :meth:`init` (``main``'s lifespan) and, failing
    that, lazily by the first write or read — so a test or a script that
    calls the model without the app's lifespan still records, instead of
    logging an "undefined table" warning per call.

    Args:
        db: The classifier's :class:`db.Database` — the one pool shared with
            the jobs table and ``reference_examples``.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._ready = False

    async def init(self) -> None:
        """Create the table and its indexes. Idempotent."""
        async with self.db.acquire() as conn:
            await create_schema(conn, "classifier.llm_calls", _SCHEMA)
        self._ready = True

    async def _ensure(self) -> None:
        if not self._ready:
            await self.init()

    # ── Writes ────────────────────────────────────────────────────────────
    async def record(self, row: dict[str, Any]) -> bool:
        """Insert one call. NEVER raises: a failure is logged, counted in
        ``classifier_llm_usage_write_errors_total`` and reported as False."""
        try:
            await self._insert(row)
            return True
        except Exception as exc:  # noqa: BLE001 — accounting must never fail a call
            llm_usage_write_errors.inc()
            logger.warning(
                "usage: could not record llm call job=%s label=%s: %s",
                row.get("job_id") if isinstance(row, dict) else None,
                row.get("label") if isinstance(row, dict) else None,
                exc,
            )
            return False

    async def _insert(self, row: dict[str, Any]) -> None:
        await self._ensure()
        values: list[Any] = []
        marks: list[str] = []
        for n, column in enumerate(COLUMNS, start=1):
            value = row.get(column)
            if column == "started_at":
                value = _ts(value)
            elif column == "usage":
                value = json.dumps(value) if value is not None else None
                marks.append(f"${n}::jsonb")
                values.append(value)
                continue
            marks.append(f"${n}")
            values.append(value)
        async with self.db.acquire() as conn:
            await conn.execute(
                f"INSERT INTO llm_calls ({', '.join(COLUMNS)}) VALUES ({', '.join(marks)})",
                *values,
            )

    async def delete_job(self, job_id: str) -> int:
        """Remove every call of one job; the number removed."""
        await self._ensure()
        async with self.db.acquire() as conn:
            status = await conn.execute("DELETE FROM llm_calls WHERE job_id = $1", job_id)
        try:
            return int(status.rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    # ── Reads ─────────────────────────────────────────────────────────────
    async def calls(self, job_id: str) -> list[dict[str, Any]]:
        """Every call of one job, in the order they were made."""
        await self._ensure()
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM llm_calls WHERE job_id = $1 ORDER BY started_at, id", job_id,
            )
        return [_call(row) for row in rows]

    async def _aggregate(
        self, where: _Where, group_by: Optional[str] = None,
    ) -> list[tuple[Any, dict[str, Any]]]:
        """``[(group key, totals block)]`` for the rows matching ``where``.

        A grouped list is ordered by its key with NULL FIRST — the order the
        SQLite version produced, so ``by_criterion`` still lists the calls
        that belong to no criterion (selection, describe) first.
        """
        await self._ensure()
        select = f"{group_by} AS grp, " if group_by else "NULL AS grp, "
        sql = f"SELECT {select}{_AGGREGATES} FROM llm_calls WHERE {where.sql}"
        if group_by:
            sql += f" GROUP BY {group_by} ORDER BY {group_by} NULLS FIRST"
        async with self.db.acquire() as conn:
            rows = await conn.fetch(sql, *where.args)
        return [(row["grp"], _block(row)) for row in rows]

    async def _models(self, where: _Where) -> list[str]:
        """The distinct models that answered (reported name, else requested)."""
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT COALESCE(model_reported, model_requested) AS model "
                f"FROM llm_calls WHERE {where.sql} "
                "AND COALESCE(model_reported, model_requested) IS NOT NULL ORDER BY 1",
                *where.args,
            )
        return [row["model"] for row in rows]

    async def totals(self, job_id: str) -> dict[str, Any]:
        """One job's totals, plus the same block per kind, outcome and criterion.

        Returns ``{"totals", "by_kind", "by_outcome", "by_criterion"}``.
        ``totals`` also carries ``models`` and ``first_call_at`` /
        ``last_call_at``. ``by_criterion`` is a LIST of blocks each with its
        ``criterion`` — the per-item selection and the describe call belong
        to no criterion, and ``null`` cannot be a JSON object key.
        """
        where = _Where().add("job_id = {}", job_id)
        ((_, totals),) = await self._aggregate(where)
        totals["models"] = await self._models(where)
        async with self.db.acquire() as conn:
            span = await conn.fetchrow(
                "SELECT MIN(started_at) AS first, MAX(started_at) AS last "
                "FROM llm_calls WHERE job_id = $1",
                job_id,
            )
        totals["first_call_at"], totals["last_call_at"] = _iso(span["first"]), _iso(span["last"])
        return {
            "totals": totals,
            "by_kind": dict(await self._aggregate(where, "kind")),
            "by_outcome": dict(await self._aggregate(where, "outcome")),
            "by_criterion": [
                {"criterion": name, **block}
                for name, block in await self._aggregate(where, "criterion")
            ],
        }

    async def summary(
        self,
        *,
        since: Optional[str | datetime] = None,
        until: Optional[str | datetime] = None,
        job_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Totals across jobs, grouped by kind and by UTC day.

        Args:
            since:    Inclusive lower bound on ``started_at`` (an ISO string,
                      as :func:`now_iso` writes it, or a datetime).
            until:    Exclusive upper bound.
            job_type: Only calls made by jobs of this type.
        """
        where = _Where()
        if since is not None:
            where.add("started_at >= {}", _ts(since))
        if until is not None:
            where.add("started_at < {}", _ts(until))
        if job_type is not None:
            where.add("job_type = {}", job_type)
        ((_, totals),) = await self._aggregate(where)
        totals["models"] = await self._models(where)
        async with self.db.acquire() as conn:
            jobs = await conn.fetchval(
                f"SELECT COUNT(DISTINCT job_id) FROM llm_calls WHERE {where.sql}", *where.args,
            )
        totals["jobs"] = int(jobs or 0)
        return {
            "totals": totals,
            "by_kind": dict(await self._aggregate(where, "kind")),
            "by_day": [
                {"day": day, **block}
                for day, block in await self._aggregate(where, _DAY)
            ],
        }

    async def job_usage(self, job_id: str) -> dict[str, Any]:
        """The compact block ``handle_job`` puts in a job's result as ``usage``."""
        totals = (await self.totals(job_id))["totals"]
        return {**totals, "usage_url": f"/jobs/{job_id}/usage"}


# ---------------------------------------------------------------------------
# Process-wide instance and the one write path
# ---------------------------------------------------------------------------

usage_store = UsageStore(database)


async def record_call(**fields: Any) -> None:
    """Build the row for one request and write it. NEVER raises.

    Called by ``llm.client._post`` once per request with :func:`build_row`'s
    arguments. The store is looked up on this module at call time, so a test
    that replaces ``llm.usage.usage_store`` captures every call.
    """
    try:
        row = build_row(**fields)
    except Exception as exc:  # noqa: BLE001 — accounting must never fail a call
        llm_usage_write_errors.inc()
        logger.warning("usage: could not build the llm call row (%s): %s",
                       fields.get("label"), exc)
        return
    await usage_store.record(row)
