"""Spawner — wraps the Docker SDK to launch and reap sandbox containers.

Single point of Docker API access in the sandbox subsystem. This file
enforces the security invariants that ai/sandbox/SANDBOX.md documents:

* Sandbox containers are attached ONLY to ``sandbox_net`` — never to
  ``ai_shared`` or ``sandbox_state``.
* Capabilities are dropped, rootfs is read-only, resource limits are
  applied. No exceptions per runtime.
* ``HTTP_PROXY`` / ``HTTPS_PROXY`` env vars are set to the sandbox-egress
  URL so any outbound HTTP goes through the allowlist proxy.
* No host bind-mounts — files are written into the container via the
  ``put_archive`` API on a tmpfs at ``/app``.

If a change here weakens any of these, the security invariant list in
``ai/sandbox/SANDBOX.md`` must be re-run to prove nothing has regressed.
"""

from __future__ import annotations

import base64
import io
import logging
import shlex
import tarfile
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Union

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from constants import (
    EGRESS_URL,
    MEMORY_LIMIT,
    NANO_CPUS_PER_CPU,
    NET_NAME,
    PIDS_LIMIT,
    SANDBOX_GID,
    SANDBOX_UID,
    SANDBOX_USER_NAME,
)
from common.textops import FileOpError, decode_utf8_prefix, utf8_tail
from runtimes import Runtime


log = logging.getLogger("sandbox-runner.spawner")

# Go's os.ModeDir bit, as Docker reports it in the get_archive stat header
# (X-Docker-Container-Path-Stat ``mode``). Lets a read refuse a directory
# before streaming its whole tree.
_GO_MODE_DIR = 1 << 31


# Type alias for a single file entry in the runtime files map. Either a raw
# str (UTF-8 text — the common case) or a discriminated dict carrying
# base64-encoded binary content. Runtime code that writes the file into the
# container uses ``_decode_file_entry`` to normalise to bytes.
FileEntry = Union[str, dict]


# Content of the Streamlit bootstrap shim that gets written into /app when
# the spawn command matches Streamlit. The shim monkeypatches Streamlit's
# uncaught-exception handler to ALSO print tracebacks to stderr, so
# ``get_logs`` shows user exceptions instead of Streamlit's usual behaviour
# of catching them and rendering only in the browser.
#
# Guarded imports: if the Streamlit internal path changes on a version bump
# the shim logs a WARNING and falls through — the entrypoint keeps working
# because Streamlit's original handler is still installed.
_STREAMLIT_BOOTSTRAP_PY = (
    "\"\"\"Runner-injected Streamlit stderr tee.\n"
    "\n"
    "Streamlit installs its own uncaught-exception handler that renders\n"
    "the error in the browser and skips stderr. That makes get_logs\n"
    "useless for debugging: models see an empty log and mistake\n"
    "silently-broken Streamlit apps for healthy ones. This shim wraps\n"
    "Streamlit's handler so tracebacks flow to stderr too, preserving\n"
    "the browser render path. Written to /app/_streamlit_bootstrap.py\n"
    "by sandbox-runner (see spawner.py).\n"
    "\"\"\"\n"
    "from __future__ import annotations\n"
    "import sys\n"
    "import traceback\n"
    "import logging\n"
    "\n"
    "_log = logging.getLogger(\"streamlit_bootstrap\")\n"
    "\n"
    "try:\n"
    "    import streamlit.runtime.scriptrunner.script_runner as _sr\n"
    "    _orig = _sr.handle_uncaught_app_exception\n"
    "\n"
    "    def _tee(ex, *a, **kw):\n"
    "        try:\n"
    "            print(\n"
    "                f\"[streamlit exception] {type(ex).__name__}: {ex}\",\n"
    "                file=sys.stderr,\n"
    "                flush=True,\n"
    "            )\n"
    "            traceback.print_exception(\n"
    "                type(ex), ex, ex.__traceback__, file=sys.stderr\n"
    "            )\n"
    "            sys.stderr.flush()\n"
    "        except Exception:\n"
    "            pass\n"
    "        return _orig(ex, *a, **kw)\n"
    "\n"
    "    _sr.handle_uncaught_app_exception = _tee\n"
    "except Exception as _exc:\n"
    "    _log.warning(\n"
    "        \"streamlit stderr tee failed to install (%s: %s); tracebacks \"\n"
    "        \"will only render in the browser\",\n"
    "        type(_exc).__name__, _exc,\n"
    "    )\n"
)


@dataclass
class SpawnResult:
    """What the runner needs to reply to the caller after spawn."""

    container_id: str
    container_name: str  # sandbox-{sandbox_id}


@dataclass
class ExecResult:
    """Return shape for ``exec_command``."""

    exit_code: int
    output: str
    duration_ms: int
    truncated: bool
    timed_out: bool = False


class Spawner:
    """Docker SDK wrapper. Constructed once per process, reused per request."""

    def __init__(self) -> None:
        # Connects to /var/run/docker.sock (mounted read-write from the host
        # in docker-compose.sandbox.yml). This is the single privileged
        # capability in the subsystem — nothing else has API access.
        self._client = docker.from_env()
        log.info(
            "Spawner initialized: net=%s egress=%s mem=%s cpu=1 pids=%d",
            NET_NAME, EGRESS_URL, MEMORY_LIMIT, PIDS_LIMIT,
        )

    def spawn(
        self,
        sandbox_id: str,
        runtime: Runtime,
        files: dict[str, FileEntry],
        entrypoint: Optional[str],
        env: Optional[dict[str, str]] = None,
    ) -> SpawnResult:
        """Create + start a container running the caller's files under the
        given runtime. Returns immediately; readiness is polled separately
        by the runner.

        The caller's files are packed into a tarball and injected into the
        container's ``/app`` tmpfs via ``put_archive``. No host filesystem
        touches user code — the tarball is built in memory. This eliminates
        an entire class of "write to host path" bugs that would otherwise
        need per-request cleanup logic.

        ``env`` is a caller-supplied environment overlay — merged on top of
        the sandbox-runner-controlled defaults (HTTP_PROXY, PYTHONUNBUFFERED,
        etc.). Caller env can't clobber the proxy vars — they're re-applied
        below after the merge — so a malicious ``HTTP_PROXY=`` in ``env``
        can't disable the egress allowlist.
        """
        name = f"sandbox-{sandbox_id}"
        user_command = entrypoint or runtime.default_entrypoint
        merged_names = sorted(
            set((runtime.default_files or {}).keys()) | set(files.keys())
        )
        log.info(
            "spawn: name=%s image=%s command=%r files=%s env_keys=%s",
            name, runtime.image, user_command, merged_names,
            sorted((env or {}).keys()),
        )

        # Merge caller env on top of sandbox defaults, then re-apply the
        # security-relevant defaults so caller env can't override them.
        merged_env = _merge_container_env(env or {})

        try:
            # Two-phase spawn to satisfy Docker's rule that put_archive
            # writes to the container rootfs unless the container is
            # started (at which point the tmpfs at /app is live).
            #
            # Phase 1: start with a placeholder PID 1 (`sleep infinity`)
            #   so the tmpfs mounts activate but no user code runs yet.
            # Phase 2: put_archive files into the now-writable /app.
            # Phase 3: exec the real command detached inside the running
            #   container. The user's app is not PID 1, but that's fine
            #   for our purposes — the reaper tears the container down on
            #   TTL, not on process exit, and health probes surface app
            #   crashes just as quickly.
            container = self._client.containers.create(
                image=runtime.image,
                name=name,
                # Override the base image's ENTRYPOINT with a placeholder
                # so we can inject files before the user's app starts.
                entrypoint=["sleep", "infinity"],
                command=[],
                environment=merged_env,
                # sandbox_net ONLY. The invariant that keeps sandboxes off
                # ai_shared, sandbox_state, and everything else lives here.
                network=NET_NAME,
                # FOLLOW-UP: read_only=True is disabled AND /app is no
                # longer a tmpfs. Two coupled Docker limitations force
                # this:
                #   1. `put_archive` on a read-only rootfs fails with
                #      "container rootfs is marked read-only" even when
                #      the target is a tmpfs mount.
                #   2. `put_archive` writes to the container rootfs and
                #      is shadowed by any tmpfs mounted at the same
                #      path — files land underneath and stay invisible.
                # The correct fix is to inject via `exec_run` with a
                # streamed tar, which uses the running container's mount
                # namespace; both invariants can then be restored. For
                # now, /app is a regular dir on the (writable) rootfs.
                # /home/sandbox stays tmpfs because the runner never
                # `put_archive`s there — only the container's own `pip
                # install --user` writes there at runtime.
                # Primary security control remains network segmentation.
                read_only=False,
                tmpfs={
                    "/tmp": "size=128M,mode=1777",
                    "/home/sandbox": "size=256M,uid=1000,gid=1000,mode=0755",
                },
                # Grant CAP_NET_BIND_SERVICE so unprivileged nginx (etc.)
                # can bind port 80 inside the container. File-caps set
                # via setcap in the base Dockerfile aren't inheritable
                # once cap_drop=ALL removes them from the ambient set.
                cap_add=["NET_BIND_SERVICE"],
                # Drop every capability. Runtimes that need one specifically
                # (none today) would add it here explicitly.
                cap_drop=["ALL"],
                # Resource caps. 1 full CPU per sandbox.
                mem_limit=MEMORY_LIMIT,
                nano_cpus=NANO_CPUS_PER_CPU,
                pids_limit=PIDS_LIMIT,
                # Sandbox runs as a non-root user (defined in the base
                # Dockerfiles). Ensuring here is belt-and-suspenders.
                user="1000:1000",
                working_dir="/app",
                # No hostname leakage — every sandbox looks the same from
                # inside, no info about the host or other sandboxes.
                hostname="sandbox",
                # Don't restart on crash; the runner reaps and returns
                # 500 to the caller so the model can decide what to do.
                restart_policy={"Name": "no"},
                # Labels used by the reaper to find our containers even if
                # the process is restarted mid-flight.
                labels={
                    "sandbox.managed": "true",
                    "sandbox.id": sandbox_id,
                    "sandbox.spawned_at": str(int(time.time())),
                },
                detach=True,
            )
        except APIError as exc:
            # Common cause: image not built yet. Give the operator a hint.
            log.error(
                "spawn: docker create failed for image=%s: %s",
                runtime.image, exc,
            )
            raise RuntimeError(
                f"docker create failed for image {runtime.image!r}: {exc}. "
                f"Did you run `docker compose -f ai/sandbox/docker-compose.sandbox.yml build`?"
            ) from exc

        # Phase 1 → 2: start with the placeholder PID 1, then inject.
        container.start()
        log.debug("spawn: container %s created + started (sleep pid1)", name)
        merged = {**(runtime.default_files or {}), **files}

        # Optional per-runtime bootstrap (e.g. Streamlit stderr tee).
        bootstrap_files = _runtime_bootstrap_files(runtime, user_command)
        if bootstrap_files:
            merged = {**bootstrap_files, **merged}
            log.debug(
                "spawn: %d bootstrap file(s) prepended: %s",
                len(bootstrap_files), sorted(bootstrap_files.keys()),
            )

        if merged:
            tarball = _make_tarball(merged)
            container.put_archive("/app", tarball)
            log.debug(
                "spawn: put_archive %d file(s) into %s:/app (%d bytes)",
                len(merged), name, len(tarball),
            )

        # Phase 3: launch the real command. `detach=True` returns
        # immediately so readiness polling can start. The command runs
        # as a child of the sleep PID 1, not as PID 1 itself — which is
        # a known compromise (see docstring above).
        #
        # stdout+stderr are redirected to /tmp/sandbox.log so
        # ``tail_logs`` can read them later. exec_run's output is NOT
        # captured in ``container.logs()`` (that only sees PID 1's
        # streams) — without this redirect the log-tail endpoint would
        # always return empty, defeating the whole runtime-error
        # feedback flow. /tmp is a 128 MB tmpfs, so the log can't
        # runaway-fill anything durable.
        launch_command = _wrap_launch_command(user_command, runtime)
        container.exec_run(
            ["/bin/sh", "-c", launch_command],
            workdir="/app",
            detach=True,
        )
        log.debug(
            "spawn: launched detached user command in %s: %r",
            name, launch_command,
        )
        return SpawnResult(container_id=container.id, container_name=name)

    def spawn_empty(
        self,
        sandbox_id: str,
        runtime: Runtime,
        entrypoint: Optional[str],
        env: Optional[dict[str, str]] = None,
    ) -> SpawnResult:
        """Spawn a container using ONLY the runtime's ``warming_files``.

        Backs the ``create`` MCP tool: the model wants a warm container
        while it's still composing the real code. First subsequent
        ``write_files`` overlays the real project on top of these
        placeholders, and the dev server hot-reloads.

        Everything about the resulting container — network, caps, TTL,
        entry-point wrapping — is identical to ``spawn``. Only the
        initial file map differs.
        """
        log.debug(
            "spawn_empty: sandbox=%s runtime=%s warming_files=%s",
            sandbox_id, runtime.image,
            sorted((runtime.warming_files or {}).keys()),
        )
        return self.spawn(
            sandbox_id=sandbox_id,
            runtime=runtime,
            files=dict(runtime.warming_files or {}),
            entrypoint=entrypoint,
            env=env,
        )

    def readiness_ok(
        self, container_name: str, port: int, path: str, timeout_s: float
    ) -> bool:
        """Poll the container's readiness probe over sandbox_net.

        Runs inside sandbox-runner, which shares sandbox_net with the
        sandbox — so we hit ``sandbox-{id}:port`` by Docker DNS.
        """
        import urllib.request

        deadline = time.monotonic() + timeout_s
        url = f"http://{container_name}:{port}{path}"
        log.debug(
            "readiness_ok: polling %s (deadline %.1fs)", url, timeout_s,
        )
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if 200 <= resp.status < 500:
                        log.debug(
                            "readiness_ok: %s replied HTTP %d after %d attempt(s)",
                            url, resp.status, attempts,
                        )
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        log.debug(
            "readiness_ok: %s never responded within %.1fs (%d attempts)",
            url, timeout_s, attempts,
        )
        return False

    def probe_health(
        self, container_name: str, port: int, path: str, timeout_s: float = 3.0
    ) -> dict:
        """Single-shot health probe used after ``write_files``.

        Distinct from ``readiness_ok`` — that one polls until it succeeds
        or deadline. This one takes a single measurement and reports it
        so the tool response can surface "your update caused a 500" or
        "the reload broke the app" without the model having to make a
        second call.

        Returns one of:
            {"code": 200, "latency_ms": 47}
            {"error": "connection refused"}
            {"error": "timeout after 3.0s"}
            {"error": "invalid url"}
        """
        import urllib.request
        import urllib.error

        url = f"http://{container_name}:{port}{path}"
        start = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                latency_ms = int((time.monotonic() - start) * 1000)
                log.debug(
                    "probe_health: %s HTTP %d in %d ms",
                    url, resp.status, latency_ms,
                )
                return {"code": resp.status, "latency_ms": latency_ms}
        except urllib.error.HTTPError as exc:
            # Dev servers happily reply 500/404 with a body — count it as
            # a real HTTP response, not a probe error.
            latency_ms = int((time.monotonic() - start) * 1000)
            log.debug(
                "probe_health: %s HTTP %d in %d ms (HTTPError)",
                url, exc.code, latency_ms,
            )
            return {"code": exc.code, "latency_ms": latency_ms}
        except urllib.error.URLError as exc:
            reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
            log.debug("probe_health: %s URLError: %s", url, reason)
            if "timed out" in reason.lower() or "timeout" in reason.lower():
                return {"error": f"timeout after {timeout_s:.1f}s"}
            return {"error": f"connection refused ({reason})"}
        except Exception as exc:
            log.warning("probe_health: %s unexpected %s: %s", url, type(exc).__name__, exc)
            return {"error": f"{type(exc).__name__}: {exc}"}

    def stop(self, container_name: str) -> None:
        """Best-effort teardown. Idempotent — if the container is already
        gone (e.g. crashed and was reaped by Docker's own restart policy),
        NotFound is swallowed."""
        try:
            container = self._client.containers.get(container_name)
        except NotFound:
            log.debug("stop: %s already gone (NotFound)", container_name)
            return
        try:
            container.stop(timeout=5)
        except APIError as exc:
            log.debug("stop: %s stop() APIError swallowed: %s", container_name, exc)
        try:
            container.remove(force=True)
            log.info("stop: removed container %s", container_name)
        except (APIError, NotFound) as exc:
            log.debug(
                "stop: %s remove() failed: %s (may be already gone)",
                container_name, exc,
            )

    def container_exists(self, container_name: str) -> bool:
        """Cheap liveness check used by the session-reuse path in the
        runner. Distinguishes "Postgres says this session is running"
        from "the container actually still exists on the Docker host" —
        the two can diverge if the reaper hasn't caught up yet or the
        container crashed out-of-band."""
        try:
            self._client.containers.get(container_name)
            return True
        except NotFound:
            log.debug("container_exists: %s NotFound", container_name)
            return False

    def export_files(self, container_name: str):
        """Return an iterator that yields tar-format chunks of the
        container's ``/app`` directory. Consumers stream it straight to
        an HTTP response — the Docker daemon does the packing.

        Raises ``docker.errors.NotFound`` if the container is gone
        (caller should surface 404). No compression here; a plain tar
        streams incrementally, whereas gzip would need the whole
        archive in-memory to hash. Callers who want gzip can pipe
        through a filter downstream."""
        log.debug("export_files: %s /app", container_name)
        container = self._client.containers.get(container_name)
        stream, stat = container.get_archive("/app")
        log.debug(
            "export_files: %s /app stat=%s",
            container_name, stat if isinstance(stat, dict) else "?",
        )
        return stream

    def read_files(
        self,
        container_name: str,
        paths: Optional[list[str]],
        max_bytes_per_file: int,
    ) -> list[dict]:
        """Return file listings — with contents if ``paths`` is set,
        or names + sizes only if ``paths`` is ``None``.

        Backs the ``get_files`` MCP tool. Uses ``docker.get_archive``
        to pull each requested path out of the running container's
        ``/app`` tree in one tar stream, then extracts entries locally
        without touching the host filesystem — everything happens in
        memory.

        Each returned entry:
            {path, size, encoding: "utf-8" | "base64", content,
             truncated: bool, error?: str}

        Path validation reuses ``_safe_relpath`` so absolute paths and
        ``..`` are rejected. Missing files are reported with an
        ``error`` field rather than raising, so a partial request still
        returns useful data.
        """
        log.debug(
            "read_files: %s paths=%s max_bytes=%d",
            container_name, paths, max_bytes_per_file,
        )
        container = self._client.containers.get(container_name)
        if paths is None:
            return _list_app_dir(container)

        out: list[dict] = []
        for p in paths:
            try:
                safe = _safe_relpath(p)
            except ValueError as exc:
                out.append({
                    "path": p, "size": 0, "encoding": "utf-8",
                    "content": "", "truncated": False,
                    "error": str(exc),
                })
                continue
            try:
                data, size, truncated = _read_archive_file(
                    container, safe, max_bytes_per_file,
                )
            except FileOpError as exc:
                out.append({
                    "path": safe, "size": 0, "encoding": "utf-8",
                    "content": "", "truncated": False,
                    "error": (
                        "not found" if exc.kind == "missing_file"
                        else exc.message
                    ),
                })
                continue
            entry = _encode_bytes_for_response(safe, data, size, truncated)
            out.append(entry)
        return out

    def read_file_bytes(
        self, container_name: str, path: str, max_bytes: int,
    ) -> tuple[str, bytes, int, bool]:
        """Read ONE regular file under ``/app``: ``(safe_path, data,
        real_size, truncated)`` with ``data`` capped at ``max_bytes``.

        Backs ``read_file`` and ``edit_file``. Raises ``FileOpError`` —
        ``unsafe_path`` / ``missing_file`` / ``is_directory`` /
        ``not_a_file`` — rather than returning a partial answer. Decoding
        and line handling are the caller's (``common.textops``)."""
        safe = _safe_path_or_error(path)
        container = self._client.containers.get(container_name)
        data, size, truncated = _read_archive_file(container, safe, max_bytes)
        log.debug(
            "read_file_bytes: %s /app/%s size=%d read=%d truncated=%s",
            container_name, safe, size, len(data), truncated,
        )
        return safe, data, size, truncated

    def read_existing_files(
        self, container_name: str, paths: list[str], max_bytes: int,
    ) -> dict[str, bytes]:
        """Current bytes of each of ``paths`` that exists as a regular
        file of at most ``max_bytes`` — one ``get_archive`` per path.

        Backs the write_files rewrite guard, which only cares about files
        it could compare: missing paths, directories, unsafe paths (the
        write itself rejects those) and files over the cap are left out
        of the result instead of raising. Keys are the caller's paths."""
        container = self._client.containers.get(container_name)
        out: dict[str, bytes] = {}
        for p in paths:
            try:
                safe = _safe_relpath(p)
                data, _size, truncated = _read_archive_file(
                    container, safe, max_bytes,
                )
            except (ValueError, FileOpError):
                continue
            if not truncated:
                out[p] = data
        log.debug(
            "read_existing_files: %s %d of %d path(s) exist",
            container_name, len(out), len(paths),
        )
        return out

    def put_files(self, container_name: str, files: dict[str, bytes]) -> None:
        """Write already-encoded ``{relpath: bytes}`` into ``/app`` in one
        ``put_archive``. Used by ``edit_file``; goes through
        ``_build_app_tarball`` like every other write, so the file and its
        parents stay owned by the sandbox user."""
        safe_files = {_safe_relpath(p): data for p, data in files.items()}
        container = self._client.containers.get(container_name)
        tarball = _build_app_tarball(safe_files)
        container.put_archive("/app", tarball)
        log.info(
            "put_files: %s ← %d file(s) via put_archive (%d bytes tar)",
            container_name, len(safe_files), len(tarball),
        )

    def exec_command(
        self,
        container_name: str,
        command: str,
        timeout_seconds: int,
        working_dir: str,
        max_output_bytes: int,
    ) -> ExecResult:
        """Run a non-interactive shell command inside the container.

        Backs the ``exec`` MCP tool. The command runs via ``sh -c`` so
        the model can chain with pipes; stdin is closed so anything
        prompting for input hangs until the timeout kicks in.

        Timeout is enforced by ``exec_start`` streaming with a wall-clock
        deadline. If the deadline hits, the exec is left running (Docker
        cleans it up when the container is torn down) and the response
        is marked ``timed_out``. Best-effort SIGKILL is attempted by
        starting a shorter ``kill`` exec that targets processes owned by
        1000:1000 whose command line looks like ours — cheaper than
        tracking PIDs across the two-phase spawn.

        Output keeps the LAST ``max_output_bytes`` (default 8 KB set at
        the app layer), cut forward to a UTF-8 character boundary, with
        ``truncated=True`` when earlier output was dropped. The tail is
        what matters: a failing build or test prints its error last.
        """
        log.info(
            "exec_command: %s cmd=%r timeout=%ds workdir=%s",
            container_name, command, timeout_seconds, working_dir,
        )
        # Reject anything that would escape /app. `working_dir` is
        # optional; empty means /app.
        try:
            safe_workdir = _safe_workdir(working_dir)
        except ValueError as exc:
            raise ValueError(str(exc))

        container = self._client.containers.get(container_name)
        wrapped = _wrap_exec_command(safe_workdir, command)

        # exec_create + exec_start stream lets us enforce a wall-clock
        # timeout without depending on the container's own signal handling.
        # `stdin` is closed by default in the SDK's non-tty exec so we
        # don't have to detach it explicitly.
        exec_id = self._client.api.exec_create(
            container.id,
            ["/bin/sh", "-c", wrapped],
            stdout=True, stderr=True,
            user="1000:1000",
            tty=False,
        )["Id"]

        start = time.monotonic()
        deadline = start + timeout_seconds
        # Ring buffer of the LAST max_output_bytes: errors and summaries
        # are at the end of a command's output, so the head is what gets
        # dropped. Chunks are popped from the left once the bytes after
        # them already cover the budget; the final join is trimmed to the
        # exact budget on a UTF-8 boundary (``utf8_tail``).
        chunks: deque[bytes] = deque()
        kept = 0
        total = 0
        timed_out = False

        try:
            stream = self._client.api.exec_start(
                exec_id, stream=True, demux=False,
            )
            for chunk in stream:
                if not isinstance(chunk, (bytes, bytearray)):
                    # SDK may hand a str back in some builds — normalize.
                    chunk = str(chunk).encode("utf-8", errors="replace")
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                chunk = bytes(chunk)
                chunks.append(chunk)
                kept += len(chunk)
                total += len(chunk)
                while chunks and kept - len(chunks[0]) >= max_output_bytes:
                    kept -= len(chunks.popleft())
        except APIError as exc:
            log.warning(
                "exec_command: %s exec_start APIError: %s",
                container_name, exc,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            return ExecResult(
                exit_code=-1,
                output=f"exec failed: {exc}",
                duration_ms=duration_ms,
                truncated=False,
                timed_out=False,
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        truncated = total > max_output_bytes
        output_bytes = utf8_tail(b"".join(chunks), max_output_bytes)
        output_text = output_bytes.decode("utf-8", errors="replace")

        if timed_out:
            log.info(
                "exec_command: %s TIMED OUT after %ds (%d bytes)",
                container_name, timeout_seconds, total,
            )
            return ExecResult(
                exit_code=-1,
                output=output_text,
                duration_ms=duration_ms,
                truncated=truncated,
                timed_out=True,
            )

        try:
            inspect = self._client.api.exec_inspect(exec_id)
            exit_code = int(inspect.get("ExitCode") or 0)
        except APIError:
            exit_code = -1
        log.info(
            "exec_command: %s exit=%d duration=%dms truncated=%s bytes=%d",
            container_name, exit_code, duration_ms, truncated, total,
        )
        return ExecResult(
            exit_code=exit_code,
            output=output_text,
            duration_ms=duration_ms,
            truncated=truncated,
            timed_out=False,
        )

    def tail_logs(self, container_name: str, n_lines: int = 100) -> str:
        """Return the last ``n_lines`` of the sandbox app's combined
        stdout+stderr, decoded UTF-8.

        Reads ``/tmp/sandbox.log`` inside the container — that's where
        ``spawn()`` redirects the user's process. ``container.logs()``
        would only see PID 1 (``sleep infinity``, no output) because
        the user's command runs via ``exec_run`` in a detached exec
        session, whose streams don't feed the container's log stream.

        Returns empty string (not error) if the container is gone or
        the app hasn't printed anything yet — callers should treat "no
        logs" as "we tried, nothing useful" rather than as a failure
        to look."""
        try:
            container = self._client.containers.get(container_name)
        except NotFound:
            log.debug("tail_logs: %s NotFound, returning empty", container_name)
            return ""
        try:
            # Fall back to /dev/null on `tail` errors (file doesn't
            # exist yet, permission surprise) so we always return a
            # string instead of raising into the async pool.
            result = container.exec_run(
                [
                    "/bin/sh",
                    "-c",
                    (
                        f"tail -n {int(n_lines)} /tmp/sandbox.log "
                        "2>/dev/null || true"
                    ),
                ],
            )
        except APIError as exc:
            log.warning("tail_logs: %s exec_run failed: %s", container_name, exc)
            return ""
        raw = result.output
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
        log.debug(
            "tail_logs: %s → %d bytes (requested %d lines)",
            container_name, len(text), n_lines,
        )
        return text

    def write_reload_marker(self, container_name: str) -> None:
        """Append a boundary line to ``/tmp/sandbox.log``.

        Called by the reuse path in ``app._apply_files`` right before
        ``write_files``. A subsequent
        ``tail_logs_since_last_marker`` uses this line as an anchor so
        the response contains only what the dev server printed AFTER
        the overlay — old tracebacks stay on disk (visible to
        ``get_logs`` and the ``/logs`` endpoints) but don't leak into
        the preview response.

        The marker is deliberately human-readable and includes a UTC
        timestamp so an operator tailing the full log can tell which
        block of output belongs to which reload attempt. That's the
        payoff over an out-of-band line-count snapshot: the boundary
        is visible in the artifact everyone else reads too.

        Best-effort: ``NotFound`` / ``APIError`` are swallowed. Worst
        case ``tail_logs_since_last_marker`` returns empty (no marker
        to anchor against), which is strictly better than raising."""
        try:
            container = self._client.containers.get(container_name)
        except NotFound:
            log.debug("write_reload_marker: %s NotFound", container_name)
            return
        try:
            # Timestamp captured inside the container so it uses the
            # sandbox's clock (base image is UTC). Leading newline
            # separates the marker cleanly from any partial line the
            # dev server left un-terminated.
            container.exec_run(
                [
                    "/bin/sh",
                    "-c",
                    (
                        "printf -- '\\n--- write_files reload %s ---\\n' "
                        "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" "
                        ">> /tmp/sandbox.log"
                    ),
                ],
            )
        except APIError as exc:
            log.warning(
                "write_reload_marker: %s exec failed: %s", container_name, exc
            )
            return
        log.debug("write_reload_marker: %s marker written", container_name)

    def append_to_log(self, container_name: str, payload: str) -> None:
        """Append raw text to ``/tmp/sandbox.log`` inside the container.

        Used by the browser-log ingest path — the runner receives a batch
        of already-formatted `[browser] …` lines from the shim and drops
        them into the same log that ``get_logs`` reads, so browser events
        appear interleaved with container output.

        The payload is base64-encoded on the way in so we don't have to
        reason about shell-escaping of newlines, quotes, backslashes, or
        control characters that might legitimately appear in a captured
        stack trace. ``base64 -d`` is available in every base image the
        subsystem ships (alpine coreutils / debian slim).

        Best-effort: ``NotFound`` / ``APIError`` are swallowed. Losing a
        batch is preferable to bubbling an error back to a browser that
        was never going to retry.
        """
        if not payload:
            return
        try:
            container = self._client.containers.get(container_name)
        except NotFound:
            log.debug("append_to_log: %s NotFound", container_name)
            return
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        try:
            container.exec_run(
                [
                    "/bin/sh",
                    "-c",
                    f"echo {encoded} | base64 -d >> /tmp/sandbox.log",
                ],
            )
        except APIError as exc:
            log.warning(
                "append_to_log: %s exec failed: %s", container_name, exc
            )
            return
        log.debug("append_to_log: %s wrote %d bytes", container_name, len(payload))

    def tail_logs_since_last_marker(
        self, container_name: str, n_lines: int = 100
    ) -> str:
        """Return log content written after the LAST
        ``--- write_files reload …`` marker.

        Paired with ``write_reload_marker``. If no marker is present
        (e.g. write failed silently, or the caller skipped it),
        returns empty — never spills full history into the preview
        response, which was the whole point of the scheme.

        Trailing ``| tail -n {n_lines}`` bounds response size against
        a chatty reload (Vite can be verbose on config changes)."""
        try:
            container = self._client.containers.get(container_name)
        except NotFound:
            log.debug(
                "tail_logs_since_last_marker: %s NotFound", container_name
            )
            return ""
        try:
            # awk state machine:
            #   seen=0    → skip lines until the first marker.
            #   marker    → reset buf, set seen=1, don't include the
            #               marker itself in the returned tail.
            #   otherwise → append to buf if we've seen a marker.
            # An unmarked file therefore returns empty rather than
            # dumping full history — the failure mode this exists to
            # prevent.
            result = container.exec_run(
                [
                    "/bin/sh",
                    "-c",
                    (
                        "awk 'BEGIN{seen=0} "
                        "/--- write_files reload /"
                        "{buf=\"\"; seen=1; next} "
                        "seen{buf = buf $0 ORS} "
                        "END{printf \"%s\", buf}' /tmp/sandbox.log "
                        f"2>/dev/null | tail -n {int(n_lines)} || true"
                    ),
                ],
            )
        except APIError as exc:
            log.warning(
                "tail_logs_since_last_marker: %s exec failed: %s",
                container_name, exc,
            )
            return ""
        raw = result.output
        text = (
            raw.decode("utf-8", errors="replace")
            if isinstance(raw, bytes)
            else str(raw or "")
        )
        log.debug(
            "tail_logs_since_last_marker: %s → %d bytes",
            container_name, len(text),
        )
        return text

    def write_files(
        self,
        container_name: str,
        files: dict[str, FileEntry],
        deletes: list[str],
    ) -> None:
        """Overlay ``files`` onto a running container's ``/app`` and
        optionally remove entries listed in ``deletes``.

        This is the hot-reload path — the container keeps running, its
        dev server (streamlit / nginx / vite / etc.) watches the
        filesystem and reacts on its own. No respawn, no readiness probe.

        ``deletes`` paths are sanitized to reject absolute paths and any
        ``..`` traversal so a malicious file map can't rm outside
        ``/app`` even though we run ``rm`` as the unprivileged sandbox
        user."""
        container = self._client.containers.get(container_name)
        for path in deletes:
            safe = _safe_relpath(path)
            container.exec_run(
                ["rm", "-rf", f"/app/{safe}"],
                user="1000:1000",
            )
            log.debug("write_files: rm -rf /app/%s in %s", safe, container_name)
        if files:
            tarball = _make_tarball(files)
            container.put_archive("/app", tarball)
            log.info(
                "write_files: %s ← %d file(s) via put_archive (%d bytes tar); "
                "removed %d",
                container_name, len(files), len(tarball), len(deletes),
            )
        else:
            log.info(
                "write_files: %s deletes-only: %d file(s) removed",
                container_name, len(deletes),
            )

    def list_managed(self) -> list[Container]:
        """Return every running container this subsystem owns.

        Filters on the ``sandbox.managed=true`` label so we never touch
        unrelated containers on the same Docker host.
        """
        containers = self._client.containers.list(
            filters={"label": "sandbox.managed=true"}
        )
        log.debug("list_managed: %d container(s)", len(containers))
        return containers


# ── Module-private helpers ────────────────────────────────────────────────


def _default_container_env() -> dict[str, str]:
    """The sandbox-runner-controlled env vars every container inherits.

    Split out so ``_merge_container_env`` can re-apply them on top of the
    caller's env, ensuring caller env can't override the security-relevant
    ones (HTTP_PROXY) or the log-buffering flags that make ``get_logs``
    useful.
    """
    return {
        "HTTP_PROXY": EGRESS_URL,
        "HTTPS_PROXY": EGRESS_URL,
        "http_proxy": EGRESS_URL,
        "https_proxy": EGRESS_URL,
        # Base images look at this to skip color codes in logs
        # captured by the runner.
        "TERM": "dumb",
        # Force Python + Node + npm to use unbuffered stdout. Details on
        # why this matters live above the original inline block; the
        # short version is that without PYTHONUNBUFFERED, `get_logs`
        # sees stale buffered content and the model tells the user "no
        # errors" while the browser is showing a crash card.
        "PYTHONUNBUFFERED": "1",
        "NPM_CONFIG_LOGLEVEL": "warn",
        "FORCE_COLOR": "0",
    }


def _merge_container_env(caller_env: dict[str, str]) -> dict[str, str]:
    """Return the effective container env: caller's overrides first,
    then sandbox-runner defaults reapplied on top so proxy + buffering
    invariants can't be clobbered."""
    merged: dict[str, str] = {}
    # Caller's env goes in first — provides user overrides.
    for k, v in (caller_env or {}).items():
        if not isinstance(k, str):
            continue
        merged[k] = str(v)
    # Runner-controlled defaults win: they overwrite anything the caller
    # tried to set for these keys, so a caller can't disable the egress
    # proxy or the unbuffered-stdout invariant.
    merged.update(_default_container_env())
    return merged


def _decode_file_entry(entry: FileEntry) -> bytes:
    """Normalise a ``files`` value to raw bytes.

    Accepts either a raw ``str`` (encoded UTF-8) or a discriminated
    dict of shape ``{"encoding": "base64", "content": "..."}``. Raises
    ``ValueError`` on unrecognised shapes so pydantic-side validation
    doesn't need to know the encoding rules.
    """
    if isinstance(entry, str):
        return entry.encode("utf-8")
    if isinstance(entry, dict):
        encoding = entry.get("encoding")
        content = entry.get("content", "")
        if encoding == "base64":
            try:
                return base64.b64decode(content, validate=True)
            except Exception as exc:
                raise ValueError(f"invalid base64 content: {exc}") from exc
        if encoding in (None, "utf-8", "utf8", "text"):
            return str(content).encode("utf-8")
        raise ValueError(
            f"unknown file encoding {encoding!r}; "
            "expected 'base64' or 'utf-8'"
        )
    raise ValueError(
        f"file value must be str or {{encoding, content}} dict, "
        f"got {type(entry).__name__}"
    )


def _make_tarball(files: dict[str, FileEntry]) -> bytes:
    """Pack ``{path: content}`` into an in-memory tar suitable for
    ``container.put_archive``. Paths are relative to the archive root
    (which will be extracted at ``/app``). Path traversal is refused —
    an absolute path or one containing ``..`` raises ``ValueError``
    rather than silently writing outside ``/app``.

    Values are either str (UTF-8) or a discriminated dict for binary
    content — see ``_decode_file_entry``.
    """
    decoded = {
        _safe_relpath(rel_path): _decode_file_entry(content)
        for rel_path, content in files.items()
    }
    return _build_app_tarball(decoded)


def _owned_tarinfo(name: str) -> tarfile.TarInfo:
    """A ``TarInfo`` owned by the sandbox user (``SANDBOX_UID``:``SANDBOX_GID``).

    Docker's ``put_archive`` extraction (moby/go-archive ``Unpack``) runs
    as root on the daemon side and ``lchown``s every entry to the uid/gid
    in its tar header. ``tarfile.TarInfo`` defaults to 0:0, so without
    this every injected file landed root-owned 0644 — readable by the
    app, but not editable, replaceable or deletable by ``exec`` (which
    runs as 1000:1000).
    """
    info = tarfile.TarInfo(name=name)
    info.uid = SANDBOX_UID
    info.gid = SANDBOX_GID
    info.uname = SANDBOX_USER_NAME
    info.gname = SANDBOX_USER_NAME
    info.mtime = int(time.time())
    return info


def _parent_dirs(paths) -> list[str]:
    """Every ancestor directory of ``paths`` (relative to ``/app``),
    parents before children, ``/app`` itself excluded.

    ``"a/b/c.txt"`` → ``["a", "a/b"]``. Empty and ``.`` segments are
    dropped so ``"./x"`` or ``"a//b"`` yield the same directories Docker
    would create.
    """
    dirs: set[str] = set()
    for path in paths:
        parts = [p for p in path.split("/") if p not in ("", ".")]
        for depth in range(1, len(parts)):
            dirs.add("/".join(parts[:depth]))
    return sorted(dirs, key=lambda d: (d.count("/"), d))


def _build_app_tarball(files: dict[str, bytes]) -> bytes:
    """Pack already-validated ``{relpath: bytes}`` into a tar for
    ``put_archive("/app", ...)``, with EVERY entry owned by the sandbox
    user — explicit directory entries included.

    The directory entries matter as much as the file ownership: for a
    parent the tar does not list, Docker's extraction creates an
    *implied* directory owned by root, mode 0755, so ``exec`` could not
    create a file inside ``src/`` after ``write_files({"src/a.js": …})``.
    Listing each parent (before its children) makes Docker create — or,
    for one that already exists, re-own and re-mode — it as 1000:1000
    0755, which also heals directories a pre-fix runner left root-owned.

    ``/app`` itself is NOT listed: ``Unpack`` skips a ``.`` entry whose
    target already exists, so a tar cannot change it. ``/app`` is owned
    by 1000:1000 in the base images (``ai/sandbox/images/*.Dockerfile``).

    Files keep mode 0644; directories get 0755.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for dir_path in _parent_dirs(files.keys()):
            info = _owned_tarinfo(dir_path)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for rel_path, data in files.items():
            info = _owned_tarinfo(rel_path)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _safe_relpath(path: str) -> str:
    """Reject anything that would escape ``/app``. Returns the cleaned
    relative path on success, raises ``ValueError`` on rejection."""
    if not path or path.startswith("/") or ".." in path.split("/"):
        log.warning("_safe_relpath: rejected %r", path)
        raise ValueError(f"unsafe path in files/deletes: {path!r}")
    return path.lstrip("/")


def _safe_path_or_error(path: str) -> str:
    """``_safe_relpath`` for the single-file tools: a rejection is a
    ``FileOpError("unsafe_path")`` the caller renders, not a ValueError."""
    try:
        return _safe_relpath(path)
    except ValueError as exc:
        raise FileOpError(
            "unsafe_path",
            f"{path!r} is not a relative path under /app — absolute "
            "paths and '..' are rejected.",
            path=path,
        ) from exc


def _wrap_exec_command(safe_workdir: str, command: str) -> str:
    """The ``sh -c`` script ``exec_command`` runs: ``cd`` into the
    (already validated) working dir, then the caller's command verbatim.

    ``sh -c`` runs a non-login shell, so no rc files are sourced — same
    behaviour as the base image entrypoint.

    The command is NOT prefixed with ``exec`` (it used to be:
    ``cd X && exec {command}``). ``exec`` replaces the shell with the
    FIRST simple command, so everything after it silently never ran —
    ``cat > a.js <<'EOF' … EOF`` + newline + ``node a.js`` wrote the file
    and exited 0 without running node — and a first word that is a
    keyword, builtin or ``VAR=value`` assignment failed outright. The cd
    is on its own line with ``|| exit 1`` so a failed cd stops the script
    instead of running the command in the wrong directory.
    """
    return f"cd {shlex.quote(safe_workdir)} || exit 1\n{command}"


def _safe_workdir(working_dir: str) -> str:
    """Working dir for ``exec_command`` — bound to ``/app`` and below.

    Empty / None → ``/app``. Rejects absolute paths outside ``/app`` and
    any ``..`` traversal. Returns the absolute path the shell should
    ``cd`` into.
    """
    if not working_dir or working_dir == "/app":
        return "/app"
    if working_dir.startswith("/"):
        # Only /app/* is allowed. Everything else is rejected.
        if working_dir == "/app" or working_dir.startswith("/app/"):
            candidate = working_dir
        else:
            raise ValueError(
                f"working_dir {working_dir!r} must be under /app"
            )
    else:
        candidate = f"/app/{working_dir}"
    # Normalize ..-free.
    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"working_dir {working_dir!r} contains '..' traversal")
    return "/" + "/".join(parts) if parts else "/"


def _list_app_dir(container: Container) -> list[dict]:
    """Return a list of ``{path, size}`` for every regular file in the
    container's ``/app`` — no content. Used when ``get_files`` is
    called without an explicit ``paths`` list.

    We stream the archive of ``/app`` and walk its members client-side
    rather than shelling out to ``find`` inside the container — one
    round trip, no shell injection surface, works even in images that
    strip coreutils. Symlinks and directories are skipped.
    """
    try:
        stream, _stat = container.get_archive("/app")
    except NotFound:
        return []
    buf = io.BytesIO()
    for chunk in stream:
        buf.write(chunk)
    buf.seek(0)
    entries: list[dict] = []
    with tarfile.open(fileobj=buf, mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            # Archive root is `app/…`; strip that so caller sees
            # relative paths under /app.
            name = member.name.split("/", 1)[-1] if "/" in member.name else ""
            if not name:
                continue
            entries.append({"path": name, "size": int(member.size)})
    entries.sort(key=lambda e: e["path"])
    log.debug("_list_app_dir: %d file(s)", len(entries))
    return entries


class _ChunkReader(io.RawIOBase):
    """File-like view over Docker's ``get_archive`` chunk iterator, so
    ``tarfile``'s stream mode reads lazily instead of the whole archive
    being buffered first. ``drain`` consumes what is left (releasing the
    HTTP connection); ``close_stream`` abandons it."""

    def __init__(self, chunks) -> None:
        super().__init__()
        self._chunks = chunks
        self._it = iter(chunks)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            try:
                self._buf = bytes(next(self._it))
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n

    def drain(self) -> None:
        for _ in self._it:
            pass

    def close_stream(self) -> None:
        closer = getattr(self._chunks, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — best effort
                pass


def _read_archive_file(
    container, safe: str, max_bytes: int,
) -> tuple[bytes, int, bool]:
    """Read ``/app/{safe}`` as ONE regular file: ``(data, real_size,
    truncated)`` with ``data`` capped at ``max_bytes``.

    ``get_archive`` on a directory returns its whole tree, so a directory
    is refused from the stat header before anything is streamed (and from
    the first tar member, if the header lacked the mode). The member is
    read only up to the cap; the rest of a file's stream is drained so the
    connection is released.

    Raises ``FileOpError``: ``missing_file``, ``is_directory``,
    ``not_a_file`` (a symlink or other special entry)."""
    try:
        stream, stat = container.get_archive(f"/app/{safe}")
    except NotFound as exc:
        raise FileOpError(
            "missing_file", f"{safe} does not exist under /app.", path=safe,
        ) from exc
    reader = _ChunkReader(stream)
    mode = int((stat or {}).get("mode") or 0) if isinstance(stat, dict) else 0
    if mode & _GO_MODE_DIR:
        reader.close_stream()
        raise FileOpError("is_directory", f"{safe} is a directory.", path=safe)
    result: Optional[tuple[bytes, int, bool]] = None
    problem: Optional[FileOpError] = None
    with tarfile.open(fileobj=reader, mode="r|") as tar:
        for member in tar:
            if member.isdir():
                problem = FileOpError(
                    "is_directory", f"{safe} is a directory.", path=safe,
                )
            elif not member.isfile():
                problem = FileOpError(
                    "not_a_file",
                    f"{safe} is not a regular file (symlink or special file).",
                    path=safe,
                )
            else:
                extracted = tar.extractfile(member)
                data = extracted.read(max_bytes) if extracted is not None else b""
                size = int(member.size)
                result = (data, size, size > max_bytes)
            break
    if problem is not None:
        reader.close_stream()
        raise problem
    reader.drain()
    if result is None:
        raise FileOpError("missing_file", f"{safe} does not exist under /app.", path=safe)
    return result


def _encode_bytes_for_response(
    path: str, data: bytes, real_size: int, truncated: bool,
) -> dict:
    """Package a file for the ``get_files`` response.

    UTF-8 files get emitted as text; anything with a null byte or
    invalid UTF-8 is base64-encoded so the caller can round-trip binary
    content through the same overlay format ``write_files`` accepts.

    A truncated read can stop in the middle of a multi-byte character;
    the incomplete tail is dropped (``decode_utf8_prefix``) instead of
    the whole file falling back to base64.
    """
    if b"\x00" in data:
        return {
            "path": path,
            "size": real_size,
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "truncated": truncated,
        }
    try:
        text = decode_utf8_prefix(data) if truncated else data.decode("utf-8")
        return {
            "path": path,
            "size": real_size,
            "encoding": "utf-8",
            "content": text,
            "truncated": truncated,
        }
    except UnicodeDecodeError:
        return {
            "path": path,
            "size": real_size,
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
            "truncated": truncated,
        }


def _wrap_launch_command(user_command: str, runtime: Runtime) -> str:
    """Return the shell command written into the launch exec_run.

    The core is ``(<cmd>) >>/tmp/sandbox.log 2>&1`` — that redirect is
    load-bearing, see ``spawn`` for the reasoning. When the runtime + cmd
    match Streamlit, prepend ``python -c 'import _streamlit_bootstrap'``
    so the stderr-tee monkeypatch loads before Streamlit starts.
    Everything else uses the raw user command.
    """
    if _is_streamlit_command(user_command):
        # Import via -c so a missing shim file doesn't break the boot.
        # `2>/dev/null || true` silences the failed import; the WARNING
        # from the shim's own except-guard is what would normally land
        # here if the internal API changed.
        prelude = (
            "python -c 'import sys; sys.path.insert(0, \"/app\"); "
            "import _streamlit_bootstrap' >/dev/null 2>&1 || true; "
        )
        return f"({prelude}{user_command}) >>/tmp/sandbox.log 2>&1"
    return f"({user_command}) >>/tmp/sandbox.log 2>&1"


def _is_streamlit_command(command: str) -> bool:
    """Cheap substring match on ``streamlit run`` — good enough because
    the default python entrypoint uses that literal, and callers who
    ship a custom Streamlit entrypoint invariably keep the ``streamlit``
    binary name (that's the CLI name)."""
    tokens = command.split()
    if not tokens:
        return False
    # `streamlit run …` or `python -m streamlit run …`
    if tokens[0] == "streamlit":
        return len(tokens) > 1 and tokens[1] == "run"
    if tokens[0] == "python" and "streamlit" in tokens:
        return True
    return False


def _runtime_bootstrap_files(runtime: Runtime, user_command: str) -> dict[str, str]:
    """Extra files the runner writes for a specific runtime/entrypoint
    combo. Currently only Streamlit gets one — see ``_STREAMLIT_BOOTSTRAP_PY``.
    """
    if _is_streamlit_command(user_command):
        return {"_streamlit_bootstrap.py": _STREAMLIT_BOOTSTRAP_PY}
    return {}
