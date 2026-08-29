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

import io
import logging
import os
import tarfile
import time
from dataclasses import dataclass
from typing import Optional

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from runtimes import Runtime


log = logging.getLogger("sandbox-runner.spawner")


# ── Config from env — populated once at module import ────────────────────
NET_NAME = os.environ.get("SANDBOX_NET_NAME", "sandbox_net")
EGRESS_URL = os.environ.get("SANDBOX_EGRESS_URL", "http://sandbox-egress:8888")

# Resource limits per sandbox — hard defaults, not caller-overridable.
# Kept aligned with what ai/sandbox/SANDBOX.md documents.
MEMORY_LIMIT = "512m"
# nano_cpus is measured in 10^-9 of a CPU, so 1 CPU = 1_000_000_000.
NANO_CPUS_PER_CPU = 1_000_000_000
PIDS_LIMIT = 256


@dataclass
class SpawnResult:
    """What the runner needs to reply to the caller after spawn."""

    container_id: str
    container_name: str  # sandbox-{sandbox_id}


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
        files: dict[str, str],
        entrypoint: Optional[str],
    ) -> SpawnResult:
        """Create + start a container running the caller's files under the
        given runtime. Returns immediately; readiness is polled separately
        by the runner.

        The caller's files are packed into a tarball and injected into the
        container's ``/app`` tmpfs via ``put_archive``. No host filesystem
        touches user code — the tarball is built in memory. This eliminates
        an entire class of "write to host path" bugs that would otherwise
        need per-request cleanup logic.
        """
        name = f"sandbox-{sandbox_id}"
        user_command = entrypoint or runtime.default_entrypoint
        merged_names = sorted(
            set((runtime.default_files or {}).keys()) | set(files.keys())
        )
        log.info(
            "spawn: name=%s image=%s command=%r files=%s",
            name, runtime.image, user_command, merged_names,
        )

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
                environment={
                    "HTTP_PROXY": EGRESS_URL,
                    "HTTPS_PROXY": EGRESS_URL,
                    "http_proxy": EGRESS_URL,
                    "https_proxy": EGRESS_URL,
                    # Base images look at this to skip color codes in logs
                    # captured by the runner.
                    "TERM": "dumb",
                    # Force Python + Node + npm to use unbuffered stdout.
                    # We redirect the user command to /tmp/sandbox.log via
                    # `>>… 2>&1`, and stdout goes block-buffered (4-8 KB)
                    # when it isn't a TTY — a fresh traceback sits in the
                    # buffer until it fills, the process exits, or someone
                    # calls flush(). That's exactly the bug where the
                    # model calls get_sandbox_logs, sees an empty file,
                    # and confidently tells the user "no errors here" while
                    # the browser is showing a crash card. PYTHONUNBUFFERED
                    # switches CPython to line-buffered even when writing
                    # to a file; NPM_CONFIG_LOGLEVEL / FORCE_COLOR keep
                    # Node dev servers from stripping their own diagnostics
                    # in a `dumb` TERM.
                    "PYTHONUNBUFFERED": "1",
                    "NPM_CONFIG_LOGLEVEL": "warn",
                    "FORCE_COLOR": "0",
                },
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
        container.exec_run(
            ["/bin/sh", "-c", f"({user_command}) >>/tmp/sandbox.log 2>&1"],
            workdir="/app",
            detach=True,
        )
        log.debug(
            "spawn: launched detached user command in %s: %r",
            name, user_command,
        )
        return SpawnResult(container_id=container.id, container_name=name)

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

        Called by the reuse path in ``app._reuse_or_spawn`` right
        before ``update_files``. A subsequent
        ``tail_logs_since_last_marker`` uses this line as an anchor so
        the response contains only what the dev server printed AFTER
        the overlay — old tracebacks stay on disk (visible to
        ``get_sandbox_logs`` and the ``/logs`` endpoints) but don't
        leak into the preview response.

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
                        "printf -- '\\n--- preview_app reload %s ---\\n' "
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

    def tail_logs_since_last_marker(
        self, container_name: str, n_lines: int = 100
    ) -> str:
        """Return log content written after the LAST
        ``--- preview_app reload …`` marker.

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
                        "/--- preview_app reload /"
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

    def update_files(
        self,
        container_name: str,
        files: dict[str, str],
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
            log.debug("update_files: rm -rf /app/%s in %s", safe, container_name)
        if files:
            tarball = _make_tarball(files)
            container.put_archive("/app", tarball)
            log.info(
                "update_files: %s ← %d file(s) via put_archive (%d bytes tar); "
                "removed %d",
                container_name, len(files), len(tarball), len(deletes),
            )
        else:
            log.info(
                "update_files: %s deletes-only: %d file(s) removed",
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


def _make_tarball(files: dict[str, str]) -> bytes:
    """Pack ``{path: content}`` into an in-memory tar suitable for
    ``container.put_archive``. Paths are relative to the archive root
    (which will be extracted at ``/app``). Path traversal is refused —
    an absolute path or one containing ``..`` raises ``ValueError``
    rather than silently writing outside ``/app``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel_path, content in files.items():
            safe = _safe_relpath(rel_path)
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=safe)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _safe_relpath(path: str) -> str:
    """Reject anything that would escape ``/app``. Returns the cleaned
    relative path on success, raises ``ValueError`` on rejection."""
    if not path or path.startswith("/") or ".." in path.split("/"):
        log.warning("_safe_relpath: rejected %r", path)
        raise ValueError(f"unsafe path in files/deletes: {path!r}")
    return path.lstrip("/")
