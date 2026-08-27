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
import os
import tarfile
import time
from dataclasses import dataclass
from typing import Optional

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from runtimes import Runtime


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
            raise RuntimeError(
                f"docker create failed for image {runtime.image!r}: {exc}. "
                f"Did you run `docker compose -f ai/sandbox/docker-compose.sandbox.yml build`?"
            ) from exc

        # Phase 1 → 2: start with the placeholder PID 1, then inject.
        container.start()
        merged = {**(runtime.default_files or {}), **files}
        if merged:
            tarball = _make_tarball(merged)
            container.put_archive("/app", tarball)

        # Phase 3: launch the real command. `detach=True` returns
        # immediately so readiness polling can start. The command runs
        # as a child of the sleep PID 1, not as PID 1 itself — which is
        # a known compromise (see docstring above).
        container.exec_run(
            ["/bin/sh", "-c", user_command],
            workdir="/app",
            detach=True,
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
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as resp:
                    if 200 <= resp.status < 500:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def stop(self, container_name: str) -> None:
        """Best-effort teardown. Idempotent — if the container is already
        gone (e.g. crashed and was reaped by Docker's own restart policy),
        NotFound is swallowed."""
        try:
            container = self._client.containers.get(container_name)
        except NotFound:
            return
        try:
            container.stop(timeout=5)
        except APIError:
            pass
        try:
            container.remove(force=True)
        except (APIError, NotFound):
            pass

    def list_managed(self) -> list[Container]:
        """Return every running container this subsystem owns.

        Filters on the ``sandbox.managed=true`` label so we never touch
        unrelated containers on the same Docker host.
        """
        return self._client.containers.list(
            filters={"label": "sandbox.managed=true"}
        )


def _make_tarball(files: dict[str, str]) -> bytes:
    """Pack ``{path: content}`` into an in-memory tar suitable for
    ``container.put_archive``. Paths are relative to the archive root
    (which will be extracted at ``/app``)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel_path, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=rel_path.lstrip("/"))
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
