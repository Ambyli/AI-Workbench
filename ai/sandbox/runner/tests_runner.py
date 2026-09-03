"""Behavioral-test execution against a live sandbox preview.

Bridges ``Spawner.spawn_tester`` (a synchronous Docker call) into the
async ``_reuse_or_spawn`` flow in ``app.py``, and encapsulates the
"is this a test file?" convention so both the missing-tests validator
and the runner see the same rule.

Contract:
  * Anything under a top-level ``tests/`` directory in ``files`` is a
    test file. No glob magic — the rule is "tests/**" and that's it.
    Simpler than per-runtime glob-matching and gives the model an
    obvious place to put things.
  * PREVIEW_URL is exported into the tester container's env as
    ``http://sandbox-{id}:80/`` — tests never hardcode a hostname.
  * On failure (non-zero exit OR timeout), a ``TestResult`` with
    ``ok=False`` is returned. Callers do not raise — the response
    should still ship, with the failing test output surfaced next to
    the preview URL so the model can iterate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional

from runtimes import Runtime


log = logging.getLogger("sandbox-runner.tests_runner")


# The prefix used to identify test files inside the caller's ``files``
# map. Kept as a module-level constant so any future change (e.g.
# supporting a top-level ``spec/`` alias) has one edit site and the
# missing-tests validator + the extractor stay in sync automatically.
TESTS_PREFIX = "tests/"


@dataclass
class TestResult:
    """Outcome of running the model-supplied tests against the live
    preview URL.

    Serialized (via ``asdict``) into the ``tests`` field of
    ``RunResponse``, which is what the MCP tool wrapper's
    ``_format_test_output`` renders back to the model.

    Attributes:
        ok: True iff exit_code == 0. Callers can rely on this for
            branching; ``exit_code`` is for the human-facing diagnostic.
        exit_code: Process exit code. -1 signals timeout (the caller
            didn't get a clean exit code).
        output: Combined stdout+stderr from the test runner. Verbatim,
            not summarized — the model iterates on this directly.
        runner: Which runner produced the output — matches
            ``Runtime.test_command`` roughly (``"pytest"`` / ``"jest"``
            / ``"sh"``). Set to a short label the MCP wrapper renders
            in the "Tests: passed (…)" one-liner.
        duration_s: Wall-clock seconds the test run took. Useful for
            operators debugging why a preview_app call felt slow.
        timed_out: True iff we hit the SANDBOX_TEST_TIMEOUT_SECONDS
            deadline instead of a clean exit. Rendered specially by
            the MCP wrapper (hint mentions the timeout, not a
            traceback).
    """

    ok: bool
    exit_code: int
    output: str
    runner: str
    duration_s: float
    timed_out: bool = False


def extract_test_files(files: dict[str, str]) -> dict[str, str]:
    """Return only the entries in ``files`` whose path starts with
    ``tests/``. Everything else is treated as app source.

    The empty case is meaningful — if nothing is under ``tests/`` the
    caller either forgot to include tests (400 territory, handled by
    ``app._validate_tests_present``) or is running on a deployment with
    ``SANDBOX_TESTS_REQUIRED=false`` (this returns empty, the runner
    skips test execution)."""
    out = {p: c for p, c in files.items() if p.startswith(TESTS_PREFIX)}
    log.debug(
        "extract_test_files: %d test file(s) of %d total",
        len(out), len(files),
    )
    return out


def _runner_label(test_command: Optional[str]) -> str:
    """Best-effort short label for the ``runner`` field of TestResult.

    Read from the first token of the runtime's ``test_command`` — that
    is stable (``pytest``, ``npx``, ``sh``) and cheap. Falls back to
    ``"unknown"`` so we never crash rendering.
    """
    if not test_command:
        return "unknown"
    head = test_command.strip().split(None, 1)[0] if test_command.strip() else ""
    # `npx --yes jest` should read as "jest", not "npx" — the user
    # cares about the runner, not the invocation wrapper.
    if head == "npx":
        parts = test_command.split()
        for p in parts:
            if p in ("jest", "vitest", "playwright"):
                return p
    return head or "unknown"


async def run_tests_in_companion(
    spawner,
    sandbox_id: str,
    runtime: Runtime,
    test_files: dict[str, str],
    timeout_s: float,
) -> TestResult:
    """Execute the runtime's tests against the live sandbox and return
    a ``TestResult``.

    Kept async so it composes with ``_reuse_or_spawn``'s coroutine
    body; the heavy lifting (docker exec) is wrapped in
    ``asyncio.to_thread`` so the event loop is never blocked while
    tests run.

    ``PREVIEW_URL`` is derived from ``sandbox_id`` alone — the tester
    reaches the sandbox on ``sandbox_net`` by Docker DNS, no public
    URL involved.
    """
    if not runtime.test_command:
        # Defensive: the runner shouldn't be calling us for a runtime
        # that has no test_command, but return a clean "skipped" result
        # instead of raising so the response path stays simple.
        log.debug(
            "run_tests_in_companion: runtime has no test_command, skipping"
        )
        return TestResult(
            ok=True,
            exit_code=0,
            output="(skipped — this runtime has no test_command declared)",
            runner="skipped",
            duration_s=0.0,
        )
    preview_url = f"http://sandbox-{sandbox_id}:80/"
    env = {"PREVIEW_URL": preview_url}
    label = _runner_label(runtime.test_command)
    log.info(
        "run_tests_in_companion: sandbox=%s runner=%s n_files=%d timeout=%.1fs",
        sandbox_id, label, len(test_files), timeout_s,
    )
    start = time.monotonic()
    try:
        exit_code, output = await asyncio.to_thread(
            spawner.spawn_tester,
            sandbox_id,
            test_files,
            runtime.test_command,
            env,
            timeout_s,
        )
    except Exception as exc:
        # Infrastructure failure (docker socket gone, image missing,
        # etc.). Do NOT propagate — tests are meant to be soft-fail,
        # and turning "tester image not built" into a 500 that hides
        # the preview URL is worse than reporting it as a test failure
        # the operator can read in the audit log.
        log.exception(
            "run_tests_in_companion: infrastructure error for sandbox=%s: %s",
            sandbox_id, exc,
        )
        duration = time.monotonic() - start
        return TestResult(
            ok=False,
            exit_code=-2,
            output=(
                f"[sandbox-tester] infrastructure error: {type(exc).__name__}: {exc}\n"
                "The tester container could not be spawned. Common causes:\n"
                "  * sandbox-tester:latest image not built (run "
                "`docker compose -f ai/sandbox/docker-compose.sandbox.yml --profile build build`).\n"
                "  * docker daemon unreachable.\n"
                "The preview URL is still valid — this is a test-harness failure, "
                "not an app failure."
            ),
            runner=label,
            duration_s=duration,
        )
    duration = time.monotonic() - start
    timed_out = exit_code == -1
    ok = exit_code == 0
    log.info(
        "run_tests_in_companion: sandbox=%s ok=%s exit=%s timed_out=%s duration=%.1fs",
        sandbox_id, ok, exit_code, timed_out, duration,
    )
    return TestResult(
        ok=ok,
        exit_code=exit_code,
        output=output,
        runner=label,
        duration_s=duration,
        timed_out=timed_out,
    )


def result_to_dict(result: Optional[TestResult]) -> Optional[dict]:
    """Serialize a ``TestResult`` for inclusion in ``RunResponse``.

    ``None`` in, ``None`` out — the reuse path may pass ``None`` when
    tests are disabled deployment-wide (``SANDBOX_TESTS_REQUIRED=false``
    AND no tests supplied), and pydantic wants the shape to survive."""
    if result is None:
        return None
    return asdict(result)
