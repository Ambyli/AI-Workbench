"""Shared fakes for the sandbox-runner tests — a Docker client that keeps
``/app`` in a dict, and a harness that points ``operations`` at it.

Not a conftest on purpose: each test module imports what it needs
(``from sandbox_fakes import ...``), so nothing is injected implicitly.
pytest puts this directory on ``sys.path`` when it collects the test
modules beside it (rootdir import mode), and importing this module also
puts ``ai/sandbox/runner`` there.
"""

from __future__ import annotations

import io
import os
import posixpath
import sys
import tarfile
from types import SimpleNamespace
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER_DIR = os.path.join(_HERE, "..", "..", "ai", "sandbox", "runner")
if RUNNER_DIR not in sys.path:
    sys.path.insert(0, RUNNER_DIR)

import spawner  # noqa: E402

UID = 1000
GID = 1000
_GO_MODE_DIR = 1 << 31


# ── tar helpers ───────────────────────────────────────────────────────────


def tar_members(tarball: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r") as tar:
        return tar.getmembers()


def tar_read(tarball: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r") as tar:
        return tar.extractfile(name).read()


def assert_all_owned(tarball: bytes) -> list[tarfile.TarInfo]:
    """Every entry 1000:1000 / sandbox, dirs 0755, files 0644."""
    members = tar_members(tarball)
    assert members, "empty tarball"
    for m in members:
        assert (m.uid, m.gid) == (UID, GID), f"{m.name} is {m.uid}:{m.gid}"
        assert m.uname == "sandbox" and m.gname == "sandbox", m.name
        if m.isdir():
            assert m.mode == 0o755, f"{m.name} dir mode {oct(m.mode)}"
        else:
            assert m.isfile(), f"{m.name} unexpected type {m.type!r}"
            assert m.mode == 0o644, f"{m.name} file mode {oct(m.mode)}"
    return members


def assert_parents_listed_first(members: list[tarfile.TarInfo]) -> None:
    """Every member's ancestors appear as directory entries BEFORE it —
    otherwise Docker would imply them, root-owned."""
    seen_dirs: set[str] = set()
    for m in members:
        name = posixpath.normpath(m.name)
        parent = posixpath.dirname(name)
        while parent not in ("", "."):
            assert parent in seen_dirs, f"{m.name}: parent {parent!r} not listed before it"
            parent = posixpath.dirname(parent)
        if m.isdir():
            seen_dirs.add(name)


# ── fake Docker client ────────────────────────────────────────────────────


class FakeContainer:
    """A container whose ``/app`` is ``files`` (relpath → bytes) plus
    ``dirs``. ``put_archive`` really applies the tarball, so a test can
    read back what a write produced; every call is recorded in order in
    ``events`` (("exec", argv) / ("put", path))."""

    def __init__(self, files: Optional[dict[str, bytes]] = None, dirs=()):
        self.id = "cid-123"
        self.files: dict[str, bytes] = {
            k: (v.encode("utf-8") if isinstance(v, str) else v)
            for k, v in (files or {}).items()
        }
        self.dirs: set[str] = set(dirs)
        self.put_calls: list[tuple[str, bytes]] = []
        self.exec_calls: list[tuple[tuple, dict]] = []
        self.events: list[tuple[str, object]] = []
        self.get_archive_calls: list[str] = []

    def start(self):
        pass

    def put_archive(self, path, data):
        data = bytes(data)
        self.put_calls.append((path, data))
        self.events.append(("put", path))
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            for m in tar.getmembers():
                if m.isdir():
                    self.dirs.add(m.name)
                elif m.isfile():
                    self.files[m.name] = tar.extractfile(m).read()
        return True

    def exec_run(self, *args, **kwargs):
        self.exec_calls.append((args, kwargs))
        argv = args[0] if args else kwargs.get("cmd")
        self.events.append(("exec", argv))
        return SimpleNamespace(output=b"", exit_code=0)

    def get_archive(self, path):
        self.get_archive_calls.append(path)
        rel = path[len("/app/"):]
        buf = io.BytesIO()
        if rel in self.dirs:
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(posixpath.basename(rel))
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            return iter([buf.getvalue()]), {"name": rel, "mode": _GO_MODE_DIR | 0o755}
        if rel not in self.files:
            raise spawner.NotFound("no such file")
        data = self.files[rel]
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(posixpath.basename(rel))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        raw = buf.getvalue()
        # Hand it over in small chunks, like Docker's streamed response.
        chunks = [raw[i:i + 512] for i in range(0, len(raw), 512)]
        return iter(chunks), {"name": posixpath.basename(rel), "size": len(data), "mode": 0o644}


class FakeContainers:
    def __init__(self, container):
        self._c = container
        self.create_kwargs: dict | None = None

    def get(self, _name):
        return self._c

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return self._c


class FakeAPI:
    """``exec_create`` / ``exec_start`` / ``exec_inspect``. ``chunks`` is
    what the exec stream yields."""

    def __init__(self, chunks=(b"ok\n",), exit_code=0):
        self.exec_create_args = None
        self.chunks = list(chunks)
        self.exit_code = exit_code

    def exec_create(self, container_id, cmd, **kwargs):
        self.exec_create_args = (container_id, cmd, kwargs)
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream=True, demux=False):
        return iter(self.chunks)

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.exit_code}


class FakeClient:
    def __init__(self, container, api: Optional[FakeAPI] = None):
        self.containers = FakeContainers(container)
        self.api = api or FakeAPI()


def spawner_with(container, api: Optional[FakeAPI] = None) -> spawner.Spawner:
    sp = spawner.Spawner.__new__(spawner.Spawner)  # skip docker.from_env()
    sp._client = FakeClient(container, api)
    return sp


# ── operations harness ────────────────────────────────────────────────────


class FakeRegistry:
    """The two registry calls the file operations make."""

    def __init__(self, session_id: str = "sess1"):
        self.session_id = session_id
        self.metadata_updates: list[tuple[str, dict]] = []

    async def update_metadata(self, sandbox_id, fields):
        self.metadata_updates.append((sandbox_id, fields))

    async def get(self, sandbox_id):
        return SimpleNamespace(metadata={"session_id": self.session_id})


SESSION = {
    "sandbox_id": "abc123abc123",
    "container_name": "sandbox-abc123abc123",
    "url": "https://example.test/sandboxes/abc123abc123/",
    "expires_at": "2026-10-15T12:00:00+00:00",
    "runtime": "node",
    "entrypoint": None,
    "ttl_seconds": 900,
    "env": {},
    "chat_id": None,
}


def install(monkeypatch, container, *, alive: bool = True):
    """Point ``operations`` / ``state`` at ``container``: a running session
    ``sess1``, no settle delay, a healthy probe. Returns
    ``(operations, spawner_instance, registry)``."""
    import operations
    import state

    sp = spawner_with(container)
    monkeypatch.setattr(sp, "probe_health", lambda *a, **k: {"code": 200, "latency_ms": 1})
    monkeypatch.setattr(sp, "container_exists", lambda name: alive)
    registry = FakeRegistry()
    monkeypatch.setattr(state, "registry", registry)
    monkeypatch.setattr(state, "spawner", sp)
    monkeypatch.setattr(state, "slot_sem", object())

    async def _find(session_id):
        return dict(SESSION) if session_id == "sess1" else None

    monkeypatch.setattr(operations, "_find_running_session", _find)
    monkeypatch.setattr(operations, "UPDATE_SETTLE_S", 0)
    return operations, sp, registry
