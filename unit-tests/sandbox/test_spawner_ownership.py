"""Ownership of the files sandbox-runner injects into ``/app``, and the
shell script ``exec`` runs.

Regression for 2026-10-15: ``exec`` in a session failed with
``/bin/sh: 1: cannot create test.js: Permission denied``. Docker's
``put_archive`` extraction applies each tar header's uid/gid, and
``tarfile.TarInfo`` defaults to 0:0, so every injected file (and every
parent directory Docker implied for it) landed root-owned while the
sandbox and ``exec`` run as 1000:1000.

No Docker is needed: the tar builders are tested directly, and the
put_archive call sites (spawn, write_files, edit_file) run
against the fake Docker client in ``sandbox_fakes.py``, which records
the tarball it is handed.

Run from the repo root:

    uv run --no-sync pytest unit-tests/sandbox -q
"""

from __future__ import annotations

import base64
import posixpath
import shutil
import subprocess

import pytest

from sandbox_fakes import (
    FakeContainer,
    install,
    assert_all_owned as _assert_all_owned,
    assert_parents_listed_first as _assert_parents_listed_first,
    spawner_with as _spawner_with,
    tar_members as _members,
    tar_read as _read,
)

import spawner  # noqa: E402  (sandbox_fakes put ai/sandbox/runner on sys.path)
from runtimes import get_runtime  # noqa: E402


# ── tar builders ──────────────────────────────────────────────────────────


def test_make_tarball_owns_files_and_every_parent_dir():
    tarball = spawner._make_tarball({
        "index.js": "console.log(1)\n",
        "src/App.jsx": "export default 1\n",
        "src/components/deep/Button.jsx": "x",
        "public/logo.png": {"encoding": "base64", "content": base64.b64encode(b"\x89PNG\x00").decode()},
    })
    members = _assert_all_owned(tarball)
    _assert_parents_listed_first(members)

    dirs = {m.name for m in members if m.isdir()}
    assert dirs == {"src", "src/components", "src/components/deep", "public"}
    files = {m.name for m in members if m.isfile()}
    assert files == {"index.js", "src/App.jsx", "src/components/deep/Button.jsx", "public/logo.png"}
    assert _read(tarball, "public/logo.png") == b"\x89PNG\x00"
    assert _read(tarball, "src/App.jsx") == b"export default 1\n"


def test_make_tarball_top_level_only_has_no_dir_entries():
    # /app itself is never listed: Docker skips a `.` entry whose target
    # exists, so it would do nothing (the base images own /app instead).
    members = _assert_all_owned(spawner._make_tarball({"a.txt": "a", "b.txt": "b"}))
    assert [m.name for m in members] == ["a.txt", "b.txt"]
    assert not any(posixpath.normpath(m.name) == "." for m in members)


def test_make_tarball_normalises_dot_and_double_slash_parents():
    members = _assert_all_owned(spawner._make_tarball({"./x/y.txt": "1", "p//q/r.txt": "2"}))
    _assert_parents_listed_first(members)
    assert {m.name for m in members if m.isdir()} == {"x", "p", "p/q"}


def test_make_tarball_dedupes_shared_parents():
    members = _members(spawner._make_tarball({"a/b/1": "1", "a/b/2": "2", "a/3": "3"}))
    dir_names = [m.name for m in members if m.isdir()]
    assert dir_names == ["a", "a/b"]


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../x", ""])
def test_make_tarball_still_refuses_traversal(bad):
    with pytest.raises(ValueError):
        spawner._make_tarball({bad: "x"})


def test_build_app_tarball_bytes_input():
    tarball = spawner._build_app_tarball({"lib/util.py": b"print(1)\n"})
    members = _assert_all_owned(tarball)
    assert [m.name for m in members] == ["lib", "lib/util.py"]


def test_parent_dirs_order_is_parents_first():
    assert spawner._parent_dirs(["z/y/x/f", "a/f", "b"]) == ["a", "z", "z/y", "z/y/x"]


# ── call sites, against a fake Docker client ──────────────────────────────


def test_spawn_injects_owned_tarball_and_keeps_non_root_user():
    c = FakeContainer()
    sp = _spawner_with(c)
    sp.spawn("abc123", get_runtime("node"), {"src/index.js": "1", "package.json": "{}"}, None)

    assert [p for p, _ in c.put_calls] == ["/app"]
    members = _assert_all_owned(c.put_calls[0][1])
    _assert_parents_listed_first(members)
    assert "src" in {m.name for m in members if m.isdir()}
    # Security invariants untouched by this change.
    kw = sp._client.containers.create_kwargs
    assert kw["user"] == "1000:1000"
    assert kw["cap_drop"] == ["ALL"]
    assert kw["working_dir"] == "/app"


def test_write_files_injects_owned_tarball():
    c = FakeContainer()
    sp = _spawner_with(c)
    sp.write_files("sandbox-abc", {"a/b/c.txt": "hi", "top.txt": "t"}, deletes=["old.txt"])

    assert len(c.put_calls) == 1
    members = _assert_all_owned(c.put_calls[0][1])
    _assert_parents_listed_first(members)
    # Deletes still run as the sandbox user, never root.
    assert c.exec_calls[0][1]["user"] == "1000:1000"


def test_edit_file_rewrites_with_owned_tarball(monkeypatch):
    import asyncio

    c = FakeContainer({"src/app.js": b"line1\nline2\nline3\n"})
    ops, _sp, _registry = install(monkeypatch, c)
    result = asyncio.run(ops._do_edit_file("sess1", "src/app.js", "line2", "LINE2"))
    assert result["replacements"] == 1

    [(path, tarball)] = c.put_calls
    assert path == "/app"
    members = _assert_all_owned(tarball)
    _assert_parents_listed_first(members)
    assert _read(tarball, "src/app.js") == b"line1\nLINE2\nline3\n"


# ── exec wrapping ─────────────────────────────────────────────────────────


def test_exec_command_runs_script_as_sandbox_user_without_exec_prefix():
    c = FakeContainer()
    sp = _spawner_with(c)
    cmd = "cat > test.js <<'EOF'\nconsole.log(1)\nEOF\nnode test.js"
    res = sp.exec_command("sandbox-abc", cmd, 5, "", 8192)
    assert res.exit_code == 0

    _cid, argv, kwargs = sp._client.api.exec_create_args
    assert kwargs["user"] == "1000:1000"
    assert argv[:2] == ["/bin/sh", "-c"]
    assert argv[2] == "cd /app || exit 1\n" + cmd
    assert "exec " not in argv[2].split("\n", 1)[0]


def test_wrap_exec_command_quotes_workdir():
    assert spawner._wrap_exec_command("/app/my dir", "ls").startswith("cd '/app/my dir' || exit 1\n")


_SH = shutil.which("sh")


@pytest.mark.skipif(_SH is None, reason="no POSIX sh on PATH")
def test_wrapped_script_runs_every_line(tmp_path):
    """The old `cd X && exec {command}` stopped after the first simple
    command; the heredoc write + follow-up command must both run."""
    workdir = tmp_path.as_posix()
    script = spawner._wrap_exec_command(
        workdir,
        "cat > test.txt <<'EOF'\nhello\nEOF\nFOO=bar; echo \"$FOO\" >> test.txt\nfor i in 1 2; do echo $i >> test.txt; done",
    )
    proc = subprocess.run([_SH, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "test.txt").read_text().split() == ["hello", "bar", "1", "2"]


@pytest.mark.skipif(_SH is None, reason="no POSIX sh on PATH")
def test_wrapped_script_stops_when_cd_fails(tmp_path):
    marker = tmp_path / "ran"
    script = spawner._wrap_exec_command(
        (tmp_path / "missing").as_posix(), f"touch '{marker.as_posix()}'",
    )
    proc = subprocess.run([_SH, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1
    assert not marker.exists()
