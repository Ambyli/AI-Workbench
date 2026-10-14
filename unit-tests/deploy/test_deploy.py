"""Tests for scripts/deploy.py -- the zero-downtime `make up`.

A FakeDocker stands in for the docker CLI: it keeps a small model of
containers, images and the compose config, answers the read commands the
script issues, applies the mutating ones, and records every argv so the tests
can assert on the exact command sequence. No docker is needed.

    uv run --no-sync pytest unit-tests/deploy -q
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("deploy", _ROOT / "scripts" / "deploy.py")
deploy = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules[cls.__module__], so the
# module must be registered before it executes.
sys.modules["deploy"] = deploy
_spec.loader.exec_module(deploy)

PROJECT = "ai-test"
COMPOSE_FILE = "ai/test/docker-compose.test.yml"


# ── fixtures: service definitions as `docker compose config --format json` prints them ──


def swap_svc(**extra) -> dict:
    svc = {
        "image": "example/app:1",
        "restart": "unless-stopped",
        "labels": {"com.zeo.deploy": "swap"},
        "healthcheck": {
            "test": ["CMD", "true"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "20s",
        },
        "networks": {"shared": None},
    }
    svc.update(extra)
    return svc


def db_svc() -> dict:
    return {
        "image": "postgres:16",
        "restart": "always",
        "container_name": "test_db",
        "ports": [{"mode": "ingress", "target": 5432, "published": "5432", "protocol": "tcp"}],
        "healthcheck": {"test": ["CMD-SHELL", "pg_isready"], "interval": "1s", "retries": 10},
    }


def gpu_svc() -> dict:
    return {
        "image": "vllm/vllm-openai:v1",
        "restart": "unless-stopped",
        "deploy": {"resources": {"reservations": {"devices": [
            {"capabilities": ["gpu"], "driver": "nvidia", "count": -1}]}}},
    }


def oneshot_svc() -> dict:
    return {"image": "example/init:1", "restart": "no"}


# ── the fake docker ─────────────────────────────────────────────────────────


class FakeDocker:
    def __init__(self, services: dict, *, images: dict | None = None, hashes: dict | None = None):
        self.config = {"name": PROJECT, "services": services}
        self.images = dict(images if images is not None else {"example/app:1": "sha256:img1"})
        self.hashes = dict(hashes or {})
        self.containers: dict = {}
        self.calls: list = []
        self.created = 0
        # per service: list of health sequences handed to the next containers compose creates
        self.next_health: dict = {}
        self.next_exit: dict = {}
        self.compose_up_rc: dict = {}
        self.edge_stats = None  # str or callable -> str
        self.exec_result = None  # deploy.Result for non-stats `docker exec` (the reload check)

    # model helpers ----------------------------------------------------------

    def add(self, service, name, *, project=PROJECT, state="running", health="healthy",
            exit_code=0, labels=None, image="sha256:img1", stop_timeout=None, ports=()):
        self.created += 1
        cid = f"{self.created:02d}" + "c" * 62
        self.containers[cid] = {
            "name": name, "service": service, "project": project, "state": state,
            "health": [health] if isinstance(health, str) else list(health),
            "exit_code": exit_code, "labels": dict(labels or {}), "image": image,
            "created": f"2026-10-14T00:00:{self.created:02d}Z",
            "ip": f"172.18.0.{self.created + 10}", "stop_timeout": stop_timeout,
            "ports": list(ports),
        }
        return cid

    def by_name(self, name):
        return next((cid for cid, c in self.containers.items() if c["name"] == name), None)

    def names(self, service, project=PROJECT):
        return sorted(c["name"] for c in self.containers.values()
                      if c["service"] == service and c["project"] == project)

    def mutating(self):
        """Every recorded call except reads, as space-joined strings."""
        reads = {"ps", "inspect", "logs", "image", "exec"}
        out = []
        for argv in self.calls:
            if argv[0] == "compose":
                sub = self._compose_args(argv)
                if sub[0] == "config":
                    continue
                out.append("compose " + " ".join(sub))
            elif argv[0] not in reads:
                out.append(" ".join(argv))
        return out

    # the runner -------------------------------------------------------------

    def __call__(self, argv, capture):
        assert argv[0] == "docker"
        args = argv[1:]
        self.calls.append(args)
        cmd = args[0]
        if cmd == "compose":
            return self._compose(self._compose_args(args))
        handler = getattr(self, "_" + cmd)
        return handler(args[1:])

    @staticmethod
    def _compose_args(args):
        rest, i = [], 1
        while i < len(args):
            if args[i] in ("-f", "--env-file", "-p"):
                i += 2
                continue
            rest = args[i:]
            break
        return rest

    def _compose(self, sub):
        R = deploy.Result
        if sub[:3] == ["config", "--format", "json"]:
            return R(0, json.dumps(self.config))
        if sub[0] == "config" and sub[1].startswith("--hash="):
            svc = sub[1].split("=", 1)[1]
            return R(0, f"{svc} {self.hashes.get(svc, 'cfg-' + svc)}\n")
        if sub[0] in ("pull", "build"):
            svc = self.config["services"][sub[1]]
            ref = svc.get("image") or f"{PROJECT}-{sub[1]}"
            self.images[ref] = f"sha256:fetched-{sub[1]}"
            return R(0)
        if sub[0] == "up":
            svc_name = sub[-1]
            rc = self.compose_up_rc.get(svc_name, 0)
            if "--scale" in sub:
                want = int(sub[sub.index("--scale") + 1].split("=")[1])
                have = [c for c in self.containers.values()
                        if c["service"] == svc_name and c["project"] == PROJECT]
                for k in range(want - len(have)):
                    healths = self.next_health.get(svc_name) or [["starting", "healthy"]]
                    health = healths.pop(0) if len(healths) > 1 else healths[0]
                    self.add(svc_name, f"{PROJECT}-{svc_name}-{len(have) + k + 1}", health=health,
                             image=self.images.get(self.config["services"][svc_name].get("image"), "x"))
                return R(rc, "", "boom" if rc else "")
            mine = [cid for cid, c in self.containers.items()
                    if c["service"] == svc_name and c["project"] == PROJECT]
            if mine:
                for cid in mine:
                    self.containers[cid]["state"] = "running"
            else:
                spec = self.config["services"].get(svc_name, {})
                cid = self.add(svc_name, spec.get("container_name") or f"{PROJECT}-{svc_name}-1",
                               health="healthy" if spec.get("healthcheck") else "none")
                if svc_name in self.next_exit:
                    self.containers[cid]["exit_code"] = self.next_exit[svc_name]
            return R(rc)
        raise AssertionError(f"unexpected compose call {sub}")

    def _ps(self, args):
        running_only = args[0] == "-q"
        filters = {}
        for i, a in enumerate(args):
            if a == "--filter":
                key, value = args[i + 1].removeprefix("label=").split("=", 1)
                filters[key] = value
        ids = [
            cid for cid, c in self.containers.items()
            if c["project"] == filters.get(deploy.PROJECT_LABEL)
            and c["service"] == filters.get(deploy.SERVICE_LABEL)
            and (c["state"] == "running" or not running_only)
        ]
        return deploy.Result(0, "\n".join(ids) + ("\n" if ids else ""))

    def _inspect(self, ids):
        out = []
        for cid in ids:
            c = self.containers.get(cid)
            if c is None:
                return deploy.Result(1, "", f"Error: No such object: {cid}")
            health = c["health"][0]
            if len(c["health"]) > 1:
                c["health"].pop(0)
            state = {"Status": c["state"], "ExitCode": c["exit_code"]}
            if health != "none":
                state["Health"] = {"Status": health}
            out.append({
                "Id": cid, "Name": "/" + c["name"], "Created": c["created"], "State": state,
                "Image": c["image"],
                "Config": {"Labels": c["labels"], "StopTimeout": c["stop_timeout"]},
                "NetworkSettings": {
                    "Networks": {"ai_shared": {"IPAddress": c["ip"]}},
                    "Ports": {f"{p}/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(p)}] for p in c["ports"]},
                },
            })
        return deploy.Result(0, json.dumps(out))

    def _image(self, args):
        ref = args[-1]
        if ref in self.images:
            return deploy.Result(0, self.images[ref] + "\n")
        return deploy.Result(1, "", f"Error: No such image: {ref}")

    def _rename(self, args):
        cid, new = args
        if self.by_name(new):
            return deploy.Result(1, "", f"Conflict. The container name {new} is already in use")
        self.containers[cid]["name"] = new
        return deploy.Result(0)

    def _stop(self, args):
        self.containers[args[-1]]["state"] = "exited"
        return deploy.Result(0)

    def _rm(self, args):
        self.containers.pop(args[-1], None)
        return deploy.Result(0)

    def _logs(self, args):
        return deploy.Result(0, "line one\nline two\n")

    def _wait(self, args):
        c = self.containers[args[-1]]
        c["state"] = "exited"
        return deploy.Result(0, f"{c['exit_code']}\n")

    def _kill(self, args):
        return deploy.Result(0)

    def _exec(self, args):
        if "wget" not in args:
            return self.exec_result or deploy.Result(0, "Configuration file is valid\n")
        if self.edge_stats is None:
            return deploy.Result(1, "", "wget: can't connect")
        text = self.edge_stats() if callable(self.edge_stats) else self.edge_stats
        return deploy.Result(0, text)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(fake: FakeDocker, *services, dry_run=False, force=False, repo_root=None, edge_listens=None):
    out = io.StringIO()
    clock = Clock()
    deployer = deploy.Deployer(
        deploy.Docker(fake), [COMPOSE_FILE], PROJECT, ".env",
        repo_root=str(repo_root or _ROOT), dry_run=dry_run, force=force,
        out=out, sleep=clock.sleep, clock=clock,
        # Tests opt in to an edge-fronted service; the real haproxy.cfg is
        # covered by the repo-invariant tests at the bottom.
        edge_listens=edge_listens or {},
    )
    rc = deployer.run(list(services))
    return rc, out.getvalue()


def target_name(fake: FakeDocker, service: str, designated: str | None = None, mounts=None) -> str:
    svc = fake.config["services"][service]
    image = fake.images[svc.get("image") or f"{PROJECT}-{service}"]
    if mounts is None:
        mounts, _ = deploy.mount_digest(svc, str(_ROOT), fake.config)
    cfg = fake.hashes.get(service, "cfg-" + service)
    return f"{designated or service}-{deploy.deploy_hash(cfg, image, mounts)}"


# ── classification ──────────────────────────────────────────────────────────


def test_swap_label_with_everything_in_place_is_swap():
    assert deploy.classify("app", swap_svc()) == (deploy.SWAP, "")


@pytest.mark.parametrize("extra,needle", [
    ({"container_name": "app"}, "fixed container_name 'app'"),
    ({"ports": [{"target": 4000, "published": "4001"}]}, "publishes host port 4001"),
    ({"healthcheck": None}, "has no healthcheck"),
    ({"healthcheck": {"test": ["NONE"]}}, "has no healthcheck"),
    ({"healthcheck": {"test": ["CMD", "true"], "disable": True}}, "has no healthcheck"),
    ({"deploy": {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}}}, "reserves a GPU"),
    ({"network_mode": "host"}, "network_mode: host"),
    ({"pull_policy": "build"}, "pull_policy: build"),
    ({"scale": 2}, "scale is 2"),
])
def test_swap_label_refused_falls_back_to_recreate(extra, needle):
    cls, reason = deploy.classify("app", swap_svc(**extra))
    assert cls == deploy.RECREATE
    assert reason.startswith("labelled com.zeo.deploy=swap, but ")
    assert needle in reason


def test_unlabelled_service_is_recreate_with_detected_reasons():
    cls, reason = deploy.classify("db", db_svc())
    assert cls == deploy.RECREATE
    assert "fixed container_name 'test_db'" in reason
    assert "publishes host port 5432" in reason
    assert reason.endswith("not labelled com.zeo.deploy=swap (its compose comment says why)")

    cls, reason = deploy.classify("model", gpu_svc())
    assert cls == deploy.RECREATE and "reserves a GPU" in reason


@pytest.mark.parametrize("restart", [None, "", "no"])
def test_restart_no_or_unset_is_oneshot(restart):
    svc = oneshot_svc()
    svc["restart"] = restart
    assert deploy.classify("init", svc)[0] == deploy.ONESHOT


def test_labels_in_list_form_are_understood():
    assert deploy.classify("app", swap_svc(labels=["com.zeo.deploy=swap"]))[0] == deploy.SWAP


# ── ordering ────────────────────────────────────────────────────────────────


def test_dependency_order_pulls_in_transitive_deps_first():
    services = {
        "api": {"depends_on": {"app": {"condition": "service_healthy"}}},
        "app": {"depends_on": {"db": {"condition": "service_healthy"}, "init": {"condition": "service_completed_successfully"}}},
        "db": {},
        "init": {"depends_on": {"db": {"condition": "service_started"}}},
        "unrelated": {},
    }
    assert deploy.dependency_order(services, ["api"]) == ["db", "init", "app", "api"]
    assert deploy.dependency_order(services, ["db", "api"]) == ["db", "init", "app", "api"]


def test_dependency_cycle_is_an_error():
    services = {"a": {"depends_on": ["b"]}, "b": {"depends_on": ["a"]}}
    with pytest.raises(deploy.DeployError, match="dependency cycle"):
        deploy.dependency_order(services, ["a"])


# ── durations and timeouts ──────────────────────────────────────────────────


@pytest.mark.parametrize("value,seconds", [
    ("30s", 30), ("1m30s", 90), ("500ms", 0.5), ("1h0m0s", 3600), ("10m0s", 600), ("40", 40), (None, 7),
])
def test_parse_duration(value, seconds):
    assert deploy.parse_duration(value, 7) == pytest.approx(seconds)


def test_health_timeout_is_start_period_plus_interval_times_retries_plus_slack():
    assert deploy.health_timeout(swap_svc()) == 20 + 10 * 3 + deploy.HEALTH_SLACK_S


def test_health_timeout_label_overrides():
    labels = {"com.zeo.deploy": "swap", "com.zeo.deploy.timeout": "15m"}
    assert deploy.health_timeout(swap_svc(labels=labels)) == 900


# ── hash composition ────────────────────────────────────────────────────────


def test_deploy_hash_moves_with_each_input():
    base = deploy.deploy_hash("cfg", "sha256:a", "m")
    assert len(base) == 12
    assert deploy.deploy_hash("cfg", "sha256:b", "m") != base
    assert deploy.deploy_hash("cfg2", "sha256:a", "m") != base
    assert deploy.deploy_hash("cfg", "sha256:a", "m2") != base
    assert deploy.deploy_hash("cfg", "sha256:a", "m") == base


def test_image_id_change_produces_a_new_name():
    fake = FakeDocker({"app": swap_svc()})
    first = target_name(fake, "app")
    fake.add("app", first)
    rc, out = run(fake, "app")
    assert rc == 0 and "up to date" in out

    fake.images["example/app:1"] = "sha256:img2"  # `make build` rebuilt under the same tag
    second = target_name(fake, "app")
    assert second != first
    rc, out = run(fake, "app")
    assert rc == 0
    assert f"swap -> {second} (replacing {first})" in out
    assert fake.names("app") == [second]


def test_bind_mounted_file_content_changes_the_hash(tmp_path):
    cfg = tmp_path / "ai" / "app" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("a: 1\n")
    svc = swap_svc(volumes=[{"type": "bind", "source": str(cfg), "target": "/app/config.yaml"}])
    before, notes = deploy.mount_digest(svc, str(tmp_path))
    assert notes == []
    cfg.write_text("a: 2\n")
    after, _ = deploy.mount_digest(svc, str(tmp_path))
    assert after != before


def test_bind_mounted_directory_is_hashed_by_paths_and_content(tmp_path):
    d = tmp_path / "ai" / "app" / "conf"
    (d / "sub").mkdir(parents=True)
    (d / "a.txt").write_text("x")
    (d / "sub" / "b.txt").write_text("y")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "junk.pyc").write_bytes(b"\0")
    svc = swap_svc(volumes=[{"type": "bind", "source": str(d), "target": "/etc/app"}])
    first, _ = deploy.mount_digest(svc, str(tmp_path))

    (d / "__pycache__" / "junk.pyc").write_bytes(b"\1")  # ignored
    assert deploy.mount_digest(svc, str(tmp_path))[0] == first
    (d / "sub" / "b.txt").write_text("z")
    second, _ = deploy.mount_digest(svc, str(tmp_path))
    assert second != first
    (d / "sub" / "b.txt").rename(d / "sub" / "c.txt")
    assert deploy.mount_digest(svc, str(tmp_path))[0] != second


def test_mounts_outside_the_repo_and_docker_sock_are_not_hashed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("1")
    plain = swap_svc()
    svc = swap_svc(volumes=[
        {"type": "bind", "source": str(outside), "target": "/x"},
        {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
        {"type": "volume", "source": "named", "target": "/data"},
    ])
    assert deploy.mount_digest(svc, str(repo))[0] == deploy.mount_digest(plain, str(repo))[0]
    outside.write_text("2")
    assert deploy.mount_digest(svc, str(repo))[0] == deploy.mount_digest(plain, str(repo))[0]


def test_oversized_bind_mounted_directory_is_skipped_with_a_note(tmp_path, monkeypatch):
    d = tmp_path / "big"
    d.mkdir()
    for i in range(4):
        (d / f"f{i}").write_text(str(i))
    monkeypatch.setattr(deploy, "MOUNT_MAX_FILES", 3)
    svc = swap_svc(volumes=[{"type": "bind", "source": str(d), "target": "/big"}])
    digest, notes = deploy.mount_digest(svc, str(tmp_path))
    assert len(notes) == 1 and "big/ not hashed (more than 3 files)" in notes[0]
    assert digest == deploy.mount_digest(swap_svc(), str(tmp_path))[0]


def test_editing_a_bind_mounted_file_swaps_the_service(tmp_path):
    cfg = tmp_path / "litellm_config.yaml"
    cfg.write_text("model_list: []\n")
    svc = swap_svc(volumes=[{"type": "bind", "source": str(cfg), "target": "/app/config.yaml"}])
    fake = FakeDocker({"app": svc})
    first = target_name(fake, "app", mounts=deploy.mount_digest(svc, str(tmp_path))[0])
    fake.add("app", first)
    rc, out = run(fake, "app", repo_root=tmp_path)
    assert rc == 0 and "up to date" in out

    cfg.write_text("model_list: [x]\n")
    rc, out = run(fake, "app", repo_root=tmp_path)
    assert rc == 0
    assert "swap ->" in out
    assert fake.names("app") != [first]


# ── swap behaviour ──────────────────────────────────────────────────────────


def test_up_to_date_is_a_no_op():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", target_name(fake, "app"))
    rc, out = run(fake, "app")
    assert rc == 0
    assert f"[{PROJECT}] app: up to date (" in out
    assert fake.mutating() == []


def test_happy_path_command_sequence():
    fake = FakeDocker({"app": swap_svc()})
    old = fake.add("app", "app", stop_timeout=900)  # the pre-swap container_name container
    target = target_name(fake, "app")
    rc, out = run(fake, "app")
    assert rc == 0, out
    new = fake.by_name(target)
    assert new is not None and new != old
    assert fake.mutating() == [
        "compose up -d --no-deps --no-recreate --scale app=2 app",
        f"rename {new} {target}",
        f"stop {old}",
        f"rm -f {old}",
    ]
    assert fake.names("app") == [target]
    assert f"swap -> {target} (replacing app)" in out
    assert "retiring app: docker stop (grace 900s), then rm" in out
    assert f"done: {target} is serving" in out


def test_first_start_with_nothing_running():
    fake = FakeDocker({"app": swap_svc()})
    rc, out = run(fake, "app")
    assert rc == 0, out
    assert "(no copy running yet)" in out
    assert fake.mutating()[0] == "compose up -d --no-deps --no-recreate --scale app=1 app"
    assert fake.names("app") == [target_name(fake, "app")]


def test_designated_name_label_is_the_name_prefix():
    labels = {"com.zeo.deploy": "swap", "com.zeo.deploy.name": "supabase-auth"}
    fake = FakeDocker({"auth": swap_svc(labels=labels)})
    fake.add("auth", "supabase-auth")
    rc, _ = run(fake, "auth")
    assert rc == 0
    [name] = fake.names("auth")
    assert name.startswith("supabase-auth-") and len(name) == len("supabase-auth-") + 12


@pytest.mark.parametrize("bad", [["starting", "unhealthy"], ["starting"]])
def test_failed_health_wait_removes_new_copy_and_keeps_old(bad):
    fake = FakeDocker({"app": swap_svc(), "later": swap_svc()})
    fake.next_health["app"] = [bad]
    old = fake.add("app", "app")
    rc, out = run(fake, "app", "later")
    assert rc == 1
    assert fake.names("app") == ["app"]
    assert fake.containers[old]["state"] == "running"
    assert not any(c.startswith(f"stop {old}") for c in fake.mutating())
    assert "line two" in out  # the new copy's logs were printed
    assert "removed the new copy, app keeps serving" in out
    assert not fake.names("later")  # the deploy stopped at the first failure


def test_new_copy_that_exits_is_removed():
    fake = FakeDocker({"app": swap_svc()})
    old = fake.add("app", "app")

    original = fake._compose

    def compose(sub):
        result = original(sub)
        if sub[0] == "up" and "--scale" in sub:
            newest = max(fake.containers, key=lambda k: fake.containers[k]["created"])
            fake.containers[newest].update(state="exited", exit_code=3)
        return result

    fake._compose = compose
    rc, out = run(fake, "app")
    assert rc == 1
    assert "exited (code 3) before becoming healthy" in out
    assert list(fake.containers) == [old]


def test_compose_scale_failure_cleans_up():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", "app")
    fake.compose_up_rc["app"] = 1
    rc, out = run(fake, "app")
    assert rc == 1
    assert "docker compose up --scale app=2 failed: boom" in out
    assert fake.names("app") == ["app"]


def test_container_vanishing_between_ps_and_inspect_is_retried():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", target_name(fake, "app"))
    original, failed = fake._inspect, []

    def flaky(ids):
        if len(ids) == 1 and not failed:
            failed.append(ids)
            return deploy.Result(1, "", "Error: No such object")
        return original(ids)

    fake._inspect = flaky
    rc, out = run(fake, "app")
    assert rc == 0 and failed and "up to date" in out


def test_leftover_containers_are_reconciled():
    fake = FakeDocker({"app": swap_svc()})
    target = target_name(fake, "app")
    exited = fake.add("app", "ai-test-app-7", state="exited")
    old = fake.add("app", "app")
    stuck = fake.add("app", target, health="unhealthy")  # an earlier, failed attempt
    rc, out = run(fake, "app")
    assert rc == 0, out
    seq = fake.mutating()
    parked = f"{target}-old-{stuck[:8]}"
    assert seq[0] == f"rm -f {exited}"
    assert seq[1] == f"rename {stuck} {parked}"
    assert seq[2] == "compose up -d --no-deps --no-recreate --scale app=3 app"
    assert seq[-4:] == [f"stop {old}", f"rm -f {old}", f"stop {stuck}", f"rm -f {stuck}"]
    assert fake.names("app") == [target]


def test_healthy_target_beside_old_copies_only_retires_the_old_ones():
    fake = FakeDocker({"app": swap_svc()})
    old = fake.add("app", "app")
    current = fake.add("app", target_name(fake, "app"))
    rc, out = run(fake, "app")
    assert rc == 0
    assert fake.mutating() == [f"stop {old}", f"rm -f {old}"]
    assert list(fake.containers) == [current]


def test_force_swaps_even_when_up_to_date():
    fake = FakeDocker({"app": swap_svc()})
    target = target_name(fake, "app")
    current = fake.add("app", target)
    rc, out = run(fake, "app", force=True)
    assert rc == 0, out
    seq = fake.mutating()
    assert seq[0] == f"rename {current} {target}-old-{current[:8]}"
    assert seq[1] == "compose up -d --no-deps --no-recreate --scale app=2 app"
    assert seq[-2:] == [f"stop {current}", f"rm -f {current}"]
    assert fake.names("app") == [target]


def test_missing_image_is_pulled_or_built_before_hashing():
    fake = FakeDocker({"app": swap_svc(), "built": swap_svc(image=None, build={"context": "."})},
                      images={})
    rc, out = run(fake, "app", "built")
    assert rc == 0, out
    seq = fake.mutating()
    assert "compose pull app" in seq
    assert "compose build built" in seq
    assert fake.names("built") == [target_name(fake, "built")]


def test_dry_run_changes_nothing():
    fake = FakeDocker({"db": db_svc(), "app": swap_svc(depends_on={"db": {"condition": "service_healthy"}}),
                       "init": oneshot_svc()})
    fake.add("app", "app")
    fake.add("app", "ai-test-app-4", state="exited")
    rc, out = run(fake, "app", "init", dry_run=True)
    assert rc == 0
    assert fake.mutating() == []
    assert "DRY RUN" in out
    assert "order: db, app, init (pulled in by depends_on: db)" in out
    assert "db: recreate (downtime): fixed container_name 'test_db'" in out
    assert "would remove leftover container ai-test-app-4 (exited)" in out
    assert "then retire app" in out
    assert "init: oneshot" in out


# ── the edge ────────────────────────────────────────────────────────────────

STATS_HEADER = "# pxname,svname,status,check_status,addr,\n"


def stats(rows):
    return STATS_HEADER + "".join(f"{px},{sv},{st},L7OK,{addr},\n" for px, sv, st, addr in rows)


FRONTED = {"app": "4001"}


def test_waits_for_the_edge_to_mark_the_new_copy_up():
    fake = FakeDocker({"app": swap_svc()})
    old = fake.add("app", "app")
    fake.add("edge", "edge", project="ai-edge")
    new_ip = f"172.18.0.{fake.created + 1 + 10}"
    replies = iter([
        stats([("app", "FRONTEND", "OPEN", ""), ("app", "app1", "UP", "172.18.0.11:4000"),
               ("app", "app2", "DOWN", f"{new_ip}:4000"), ("app", "BACKEND", "UP", "")]),
        stats([("app", "app1", "UP", "172.18.0.11:4000"), ("app", "app2", "UP", f"{new_ip}:4000")]),
    ])
    fake.edge_stats = lambda: next(replies)
    rc, out = run(fake, "app", edge_listens=FRONTED)
    assert rc == 0, out
    assert "edge backend app/app2 is UP" in out
    execs = [c for c in fake.calls if c[0] == "exec"]
    assert len(execs) == 2
    assert execs[0][2:] == ["wget", "-qO-", "http://127.0.0.1:8404/stats;csv"]
    assert fake.mutating()[-2:] == [f"stop {old}", f"rm -f {old}"]
    assert "WARNING" not in out


def test_service_the_edge_does_not_front_never_asks_the_edge():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", "app")
    fake.add("edge", "edge", project="ai-edge")
    fake.edge_stats = stats([("litellm", "app1", "UP", "172.18.0.40:4000")])
    rc, out = run(fake, "app", edge_listens={"litellm": "4001"})
    assert rc == 0, out
    assert "edge" not in out
    assert not [c for c in fake.calls if c[0] == "exec"]


def test_edge_that_never_marks_the_copy_up_aborts():
    fake = FakeDocker({"app": swap_svc()})
    old = fake.add("app", "app")
    fake.add("edge", "edge", project="ai-edge")
    fake.edge_stats = stats([("app", "app1", "UP", "172.18.0.11:4000")])
    rc, out = run(fake, "app", edge_listens=FRONTED)
    assert rc == 1
    assert "the edge did not mark" in out
    assert list(fake.names("app")) == ["app"]
    assert fake.containers[old]["state"] == "running"


def test_unreadable_edge_stats_abort_instead_of_retiring_blind():
    fake = FakeDocker({"app": swap_svc()})
    old = fake.add("app", "app")
    fake.add("edge", "edge", project="ai-edge")
    fake.edge_stats = None  # wget fails
    rc, out = run(fake, "app", edge_listens=FRONTED)
    assert rc == 1
    assert "could not read the edge's stats" in out
    assert "removed the new copy, app keeps serving" in out
    assert fake.names("app") == ["app"]
    assert fake.containers[old]["state"] == "running"


def test_edge_running_an_older_config_without_the_backend_aborts():
    fake = FakeDocker({"app": swap_svc()})
    old = fake.add("app", "app")
    fake.add("edge", "edge", project="ai-edge")
    fake.edge_stats = stats([("litellm", "app1", "UP", "172.18.0.40:4000")])
    rc, out = run(fake, "app", edge_listens=FRONTED)
    assert rc == 1
    assert "the running edge has no backend 'app'" in out
    assert fake.names("app") == ["app"]
    assert fake.containers[old]["state"] == "running"


def test_cutover_swaps_with_a_warning_while_the_old_copy_still_holds_the_port():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", "app", ports=[4001])
    rc, out = run(fake, "app", edge_listens=FRONTED)
    assert rc == 0, out
    assert "app still publishes host port 4001 itself (the one-time cutover)" in out
    assert fake.names("app") == [target_name(fake, "app")]
    assert out.rstrip().splitlines()[-1].startswith(f"[{PROJECT}] WARNING: the edge is not running")
    assert "4001 (app)" in out and "make up edge" in out
    assert not [c for c in fake.calls if c[0] == "exec"]


def test_fresh_start_without_the_edge_swaps_with_a_warning():
    fake = FakeDocker({"app": swap_svc()})
    rc, out = run(fake, "app", edge_listens=FRONTED)
    assert rc == 0, out
    assert "LAN port 4001 has no listener until `make up edge`" in out
    assert "WARNING: the edge is not running, so these LAN ports have no listener: 4001 (app)" in out


def test_missing_edge_after_the_cutover_refuses_the_swap():
    fake = FakeDocker({"app": swap_svc(), "later": swap_svc()})
    old = fake.add("app", "app")  # no host port: the cutover already happened
    rc, out = run(fake, "app", "later", edge_listens=FRONTED)
    assert rc == 1
    assert "Run `make up edge` first, or FORCE=1" in out
    assert fake.mutating() == []
    assert fake.containers[old]["state"] == "running"
    assert not fake.names("later")
    assert "4001 (app)" not in out  # refused, so nothing was left without a listener


def test_missing_edge_after_the_cutover_swaps_with_force():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", "app")
    rc, out = run(fake, "app", force=True, edge_listens=FRONTED)
    assert rc == 0, out
    assert "swapping anyway (FORCE=1)" in out
    assert fake.names("app") == [target_name(fake, "app")]
    assert "4001 (app)" in out


def test_up_to_date_fronted_service_does_not_need_the_edge():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", target_name(fake, "app"))
    rc, out = run(fake, "app", edge_listens=FRONTED)
    assert rc == 0 and "up to date" in out and "WARNING" not in out


def test_dry_run_reports_a_missing_edge():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", "app", ports=[4001])
    rc, out = run(fake, "app", dry_run=True, edge_listens=FRONTED)
    assert rc == 0 and fake.mutating() == []
    assert "these LAN ports would have no listener: 4001 (app)" in out


def test_parse_edge_listens():
    text = """
global
    maxconn 10
frontend stats
    bind 127.0.0.1:8404
listen litellm   # comment
    bind :4001
    server-template app 3 litellm:4000 check
listen superset
    bind 0.0.0.0:8016
"""
    assert deploy.parse_edge_listens(text) == {"litellm": "4001", "superset": "8016"}


def test_parse_edge_listens_keeps_the_first_bind_and_ignores_internal_frontends():
    text = """
.if streq("${X-}","")
    .alert "x"
.endif
frontend metrics
    bind :8405 name internal
listen litellm
    bind :4001 name lan
    bind :14000 name internal
    acl lan_bind so_name -m str lan
    .if !streq("${N-8}",0)
    stick-table type string len 64 size 10k expire 10m
    .endif
    server-template app 3 litellm:4000 check
listen classifier
    bind :8005 name lan
"""
    assert deploy.parse_edge_listens(text) == {"litellm": "4001", "classifier": "8005"}


def recreate_api_svc() -> dict:
    """A recreate-class HTTP API the edge fronts (classifier, sandbox-runner, detector)."""
    return {
        "image": "example/api:1", "restart": "unless-stopped", "container_name": "api",
        "healthcheck": {"test": ["CMD", "true"], "interval": "10s", "retries": 3},
        "networks": {"shared": None},
    }


def test_fronted_recreate_class_service_never_asks_the_edge():
    """check_edge_before_swap / wait_edge live in the swap path only: a
    recreate-class backend of the edge deploys exactly as before, even with
    the edge down."""
    fake = FakeDocker({"api": recreate_api_svc()}, images={"example/api:1": "sha256:a"})
    rc, out = run(fake, "api", edge_listens={"api": "8005"})
    assert rc == 0, out
    assert "api: recreate (downtime)" in out
    assert "edge" not in out
    assert fake.mutating() == ["compose up -d --no-deps api"]
    asked_edge = [c for c in fake.calls if c[0] == "ps" and f"label={deploy.PROJECT_LABEL}={deploy.EDGE_PROJECT}" in c]
    assert not asked_edge and not [c for c in fake.calls if c[0] == "exec"]


def test_parse_edge_stats():
    rows = deploy.parse_edge_stats(stats([("a", "FRONTEND", "OPEN", ""), ("a", "app1", "UP", "1.2.3.4:80"),
                                          ("b", "app1", "UP", "5.6.7.8:80")]), "a")
    assert [(r["svname"], r["addr"]) for r in rows] == [("app1", "1.2.3.4:80")]
    assert deploy.parse_edge_stats("", "a") == []


# ── recreate and oneshot ────────────────────────────────────────────────────


def test_recreate_class_runs_plain_up_and_says_why():
    fake = FakeDocker({"model": gpu_svc()}, images={"vllm/vllm-openai:v1": "sha256:v"})
    rc, out = run(fake, "model")
    assert rc == 0
    assert "model: recreate (downtime): reserves a GPU; not labelled com.zeo.deploy=swap (its compose comment says why)" in out
    assert fake.mutating() == ["compose up -d --no-deps model"]


def test_recreate_class_reports_unchanged_when_compose_will_not_touch_it():
    fake = FakeDocker({"model": gpu_svc()}, images={"vllm/vllm-openai:v1": "sha256:v"})
    fake.add("model", "model", labels={deploy.CONFIG_HASH_LABEL: "cfg-model"}, image="sha256:v")
    rc, out = run(fake, "model")
    assert rc == 0
    assert "model: recreate-class, unchanged -- compose leaves it running" in out


def test_recreate_waits_for_healthy_dependency():
    fake = FakeDocker({"db": db_svc(), "app": {
        "image": "example/app:1", "restart": "unless-stopped",
        "depends_on": {"db": {"condition": "service_healthy", "required": True}}}})
    fake.add("db", "test_db", health=["starting", "starting", "healthy"])
    rc, out = run(fake, "app")
    assert rc == 0, out
    assert fake.mutating() == ["compose up -d --no-deps db", "compose up -d --no-deps app"]


def test_unhealthy_dependency_times_out():
    fake = FakeDocker({"db": db_svc(), "app": {
        "image": "example/app:1", "restart": "unless-stopped",
        "depends_on": {"db": {"condition": "service_healthy"}}}})
    fake.add("db", "test_db", health="starting")
    rc, out = run(fake, "app")
    assert rc == 1
    assert "timed out after" in out and "waiting for db to be healthy (needed by app)" in out


def test_oneshot_success_then_dependents_run():
    fake = FakeDocker({"init": oneshot_svc(), "app": swap_svc(
        depends_on={"init": {"condition": "service_completed_successfully"}})})
    rc, out = run(fake, "app")
    assert rc == 0, out
    seq = fake.mutating()
    assert seq[0] == "compose up -d --no-deps init"
    assert seq[1].startswith("wait ")
    assert "exited 0" in out


def test_oneshot_failure_stops_the_deploy():
    fake = FakeDocker({"init": oneshot_svc(), "app": swap_svc(
        depends_on={"init": {"condition": "service_completed_successfully"}})})
    fake.next_exit["init"] = 2
    rc, out = run(fake, "app")
    assert rc == 1
    assert "init: FAILED:" in out and "exited with code 2" in out
    assert fake.names("app") == []


def test_profile_gated_service_is_handed_to_compose():
    fake = FakeDocker({"app": swap_svc()})
    rc, out = run(fake, "dashboard")
    assert rc == 0
    assert "dashboard: recreate (downtime): not in the stack's default config" in out
    assert fake.mutating() == ["compose up -d --no-deps dashboard"]


def edge_like_svc() -> dict:
    return {
        "image": "haproxy:3.4.6-alpine", "restart": "unless-stopped", "container_name": "edge",
        "ports": [{"target": 4001, "published": "4001", "protocol": "tcp"}],
        "labels": {"com.zeo.deploy.wait": "healthy"},
        "healthcheck": {"test": ["CMD", "true"], "interval": "10s", "retries": 3, "start_period": "5s"},
    }


def test_recreate_with_wait_label_waits_for_healthy():
    fake = FakeDocker({"edge": edge_like_svc()}, images={"haproxy:3.4.6-alpine": "sha256:h"})
    fake.add("edge", "edge", health=["starting", "starting", "healthy"])
    rc, out = run(fake, "edge")
    assert rc == 0, out
    assert "edge: recreate (downtime)" in out and "edge is healthy" in out


@pytest.mark.parametrize("health", ["unhealthy", "starting"])
def test_recreate_with_wait_label_fails_loudly_when_never_healthy(health):
    fake = FakeDocker({"edge": edge_like_svc()}, images={"haproxy:3.4.6-alpine": "sha256:h"})
    fake.add("edge", "edge", health=health)
    rc, out = run(fake, "edge")
    assert rc == 1
    assert f"edge: FAILED: edge is {health} after `up -d`" in out
    assert "line two" in out  # its logs were printed


def reloadable_edge_svc() -> dict:
    svc = edge_like_svc()
    svc["labels"] = {
        "com.zeo.deploy.wait": "healthy",
        "com.zeo.deploy.reload": "USR2",
        "com.zeo.deploy.reload.check": "haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg",
    }
    return svc


def unchanged_edge(fake):
    return fake.add("edge", "edge", labels={deploy.CONFIG_HASH_LABEL: "cfg-edge"}, image="sha256:h")


def test_unchanged_reloadable_service_is_checked_then_signalled():
    fake = FakeDocker({"edge": reloadable_edge_svc()}, images={"haproxy:3.4.6-alpine": "sha256:h"})
    cid = unchanged_edge(fake)
    rc, out = run(fake, "edge")
    assert rc == 0, out
    execs = [c for c in fake.calls if c[0] == "exec"]
    assert execs == [["exec", cid, "haproxy", "-c", "-f", "/usr/local/etc/haproxy/haproxy.cfg"]]
    assert fake.mutating() == [f"kill -s USR2 {cid}"]  # no compose up: nothing to recreate
    assert "reloading in place with USR2" in out
    assert "edge is healthy" in out


def test_failed_reload_check_stops_before_the_signal():
    fake = FakeDocker({"edge": reloadable_edge_svc(), "after": edge_like_svc()},
                      images={"haproxy:3.4.6-alpine": "sha256:h"})
    unchanged_edge(fake)
    fake.exec_result = deploy.Result(1, "", "[ALERT] parsing [haproxy.cfg:93] : unknown keyword 'sever'\n")
    rc, out = run(fake, "edge", "after")
    assert rc == 1
    assert "unknown keyword 'sever'" in out
    assert "failed in edge; not reloading, the running config is untouched" in out
    assert fake.mutating() == []
    assert not fake.names("after")


def test_changed_reloadable_service_is_recreated_not_signalled():
    fake = FakeDocker({"edge": reloadable_edge_svc()}, images={"haproxy:3.4.6-alpine": "sha256:h"})
    fake.add("edge", "edge", labels={deploy.CONFIG_HASH_LABEL: "an-older-config"}, image="sha256:h")
    rc, out = run(fake, "edge")
    assert rc == 0, out
    assert fake.mutating() == ["compose up -d --no-deps edge"]
    assert not [c for c in fake.calls if c[0] == "exec"]
    assert "edge: recreate (downtime)" in out


def test_dry_run_describes_the_reload():
    fake = FakeDocker({"edge": reloadable_edge_svc()}, images={"haproxy:3.4.6-alpine": "sha256:h"})
    unchanged_edge(fake)
    rc, out = run(fake, "edge", dry_run=True)
    assert rc == 0
    assert fake.mutating() == [] and not [c for c in fake.calls if c[0] == "exec"]
    assert "would run `haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg` in the running container, then send USR2" in out


def test_recreate_without_wait_label_does_not_wait():
    svc = edge_like_svc()
    svc["labels"] = {}
    fake = FakeDocker({"edge": svc}, images={"haproxy:3.4.6-alpine": "sha256:h"})
    fake.add("edge", "edge", health="unhealthy")
    rc, out = run(fake, "edge")
    assert rc == 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_main_wires_flags_through_to_compose():
    fake = FakeDocker({"app": swap_svc()})
    fake.add("app", target_name(fake, "app"))
    out = io.StringIO()
    rc = deploy.main(["-f", COMPOSE_FILE, "--env-file", ".env", "-p", PROJECT, "app"],
                     runner=fake, out=out, sleep=lambda s: None)
    assert rc == 0
    assert fake.calls[0] == ["compose", "-f", COMPOSE_FILE, "--env-file", ".env", "-p", PROJECT,
                             "config", "--format", "json"]
    assert "up to date" in out.getvalue()


def test_main_reports_a_broken_config():
    def runner(argv, capture):
        return deploy.Result(1, "", "required variable SUPABASE_JWT_SECRET is missing a value")

    out = io.StringIO()
    rc = deploy.main(["-f", COMPOSE_FILE, "-p", PROJECT], runner=runner, out=out)
    assert rc == 1
    assert "FAILED: docker compose config failed: required variable" in out.getvalue()


# ── repo invariants: compose files, edge config and Makefile agree ──────────

def _yaml():
    # Only these invariant tests need PyYAML; the deploy tests above must not
    # be skipped with them.
    return pytest.importorskip("yaml")


def _compose_files():
    return sorted((_ROOT / "ai").glob("*/docker-compose.*.yml"))


def _swap_services():
    """{service key: (stack, service dict, compose dict)} for every swap-labelled service."""
    found = {}
    for path in _compose_files():
        doc = _yaml().safe_load(path.read_text(encoding="utf-8"))
        for key, svc in (doc.get("services") or {}).items():
            if deploy.service_labels(svc).get(deploy.SWAP_LABEL) == deploy.SWAP:
                found[key] = (path.parent.name, svc, doc)
    return found


def _all_services():
    """{service key: [(stack, service dict, compose dict)]} across every compose file."""
    found = {}
    for path in _compose_files():
        doc = _yaml().safe_load(path.read_text(encoding="utf-8"))
        for key, svc in (doc.get("services") or {}).items():
            found.setdefault(key, []).append((path.parent.name, svc, doc))
    return found


def _on_ai_shared(svc: dict, doc: dict) -> bool:
    shared = {k for k, v in (doc.get("networks") or {}).items() if (v or {}).get("name") == "ai_shared"}
    return bool(shared & set(svc.get("networks") or {}))


EDGE_CFG_PATH = _ROOT / "ai" / "edge" / "haproxy.cfg"
EDGE_COMPOSE_PATH = _ROOT / "ai" / "edge" / "docker-compose.edge.yml"
# The recreate-class HTTP APIs the edge fronts only to turn their restart into
# a 503 (EDGE.md § The edge).
EDGE_RECREATE_BACKENDS = {"classifier", "sandbox-runner", "detector"}


def _haproxy_sections():
    """[{kind, name, binds: [(addr, port, bind name)], lines, server: (host, port)}].

    Section headers start in column 0; everything indented (including an
    indented `.if` block) belongs to the section above it. Comments are
    stripped, which is safe because no directive in haproxy.cfg carries a `#`.
    """
    out, cur = [], None
    for raw in EDGE_CFG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not raw.startswith((" ", "\t")):
            words = line.split()
            cur = {"kind": words[0], "name": words[1] if len(words) > 1 else "", "binds": [],
                   "lines": [], "server": None}
            out.append(cur)
            continue
        cur["lines"].append(line)
        words = line.split()
        if words[0] == "bind":
            addr, port = words[1].rsplit(":", 1)
            name = words[words.index("name") + 1] if "name" in words else None
            cur["binds"].append((addr, int(port), name))
        elif words[0] == "server-template":
            host, port = words[3].rsplit(":", 1)
            cur["server"] = (host, int(port))
    return out


def _section(kind: str, name: str) -> dict:
    (found,) = [s for s in _haproxy_sections() if s["kind"] == kind and s["name"] == name]
    return found


def _haproxy_listens():
    """{listen name: (FIRST bind port, upstream host, upstream port)} -- what deploy.py sees."""
    return {s["name"]: (s["binds"][0][1], *s["server"]) for s in _haproxy_sections() if s["kind"] == "listen"}


def _edge_compose_service():
    return _yaml().safe_load(EDGE_COMPOSE_PATH.read_text(encoding="utf-8"))["services"]["edge"]


def _edge_published_container_ports():
    return {int(p.rsplit(":", 1)[1]) for p in _edge_compose_service()["ports"]}


def _env_example() -> str:
    return (_ROOT / ".env.example").read_text(encoding="utf-8")


def test_every_swap_labelled_service_really_swaps():
    swaps = _swap_services()
    assert {"litellm", "oauth2-proxy", "cloudflared", "searxng", "supabase-api"} <= set(swaps)
    for key, (stack, svc, _doc) in swaps.items():
        assert deploy.classify(key, svc) == (deploy.SWAP, ""), f"{stack}/{key}"


def test_every_edge_backend_is_on_ai_shared_and_swap_or_portless_recreate():
    services = _all_services()
    listens = _haproxy_listens()
    assert listens, "no listen sections parsed"
    recreate = set()
    for name, (bind, host, port) in listens.items():
        assert len(services.get(name, [])) == 1, f"listen {name} is not exactly one compose service"
        assert host == name, f"listen {name} forwards to {host}"
        stack, svc, doc = services[name][0]
        assert _on_ai_shared(svc, doc), f"{stack}/{name} is not on ai_shared, so the edge cannot reach it"
        cls, _reason = deploy.classify(name, svc)
        if cls == deploy.RECREATE:
            # Fronted only for the friendly 503: the edge owns its host port.
            assert not svc.get("ports"), f"{stack}/{name} is fronted by the edge but still publishes ports"
            assert deploy.has_healthcheck(svc), f"{stack}/{name} has no healthcheck for the edge to copy"
            recreate.add(name)
        else:
            assert cls == deploy.SWAP, f"{stack}/{name} is {cls}"
        assert bind and port
    assert recreate == EDGE_RECREATE_BACKENDS


def test_edge_publishes_exactly_its_lan_listeners():
    ports = _edge_compose_service()["ports"]
    assert _edge_published_container_ports() == {bind for bind, _h, _p in _haproxy_listens().values()}
    env_example = _env_example()
    for p in ports:
        var = p.split("${", 1)[1].split("}", 1)[0].split(":-", 1)[0]
        assert f"\n{var}=" in env_example, f"{var} missing from .env.example"
        row = next(line for line in env_example.splitlines() if line.startswith(f"{var}="))
        assert "edge.yml" in row, f"PORT REGISTRY row for {var} must name edge.yml"
    assert "\nEDGE_HAPROXY_VERSION=" in env_example


def test_lan_and_internal_binds_are_named_and_internal_ones_never_published():
    published = _edge_published_container_ports()
    internal = set()
    for s in _haproxy_sections():
        if s["kind"] == "listen":
            first, *rest = s["binds"]
            assert first[2] == "lan", f"listen {s['name']}: the first bind must be `name lan`"
            for _addr, port, name in rest:
                assert name == "internal", f"listen {s['name']}: extra bind :{port} must be `name internal`"
                internal.add(port)
        elif s["kind"] == "frontend":
            for addr, port, name in s["binds"]:
                assert addr == "127.0.0.1" or name == "internal", f"frontend {s['name']} :{port}"
                internal.add(port)
    assert {8404, 8405, 8407, 14000} <= internal
    assert not internal & published, f"internal listeners published: {internal & published}"


def test_lan_allow_list_applies_to_every_lan_bind_and_no_internal_one():
    acl = 'acl lan_allowed src "${EDGE_ALLOWED_CIDRS[*]}"'
    listens = 0
    for s in _haproxy_sections():
        body = s["lines"]
        if s["kind"] == "frontend":
            assert not [l for l in body if "lan_allowed" in l or "EDGE_ALLOWED_CIDRS" in l], s["name"]
            continue
        if s["kind"] != "listen":
            continue
        listens += 1
        assert acl in body, f"listen {s['name']} has no LAN allow-list"
        rejects = [l for l in body if l.startswith("http-request return status 403") and "lan_allowed" in l]
        assert len(rejects) == 1, f"listen {s['name']}: expected exactly one 403 rule"
        # First http-request rule, ahead of tracking and header rewrites.
        assert next(l for l in body if l.startswith("http-request ")) == rejects[0], s["name"]
        if len(s["binds"]) > 1:
            # An internal bind shares the section: the reject must be limited to the LAN bind.
            assert "acl lan_bind so_name -m str lan" in body, s["name"]
            assert rejects[0].endswith("if lan_bind !lan_allowed"), s["name"]
        else:
            assert rejects[0].endswith("unless lan_allowed"), s["name"]
    assert listens == len(_haproxy_listens())


def test_per_key_limit_is_lan_only_and_never_stores_the_raw_key():
    litellm = _section("listen", "litellm")
    tracks = [l for l in litellm["lines"] if "track-sc" in l]
    limits = [l for l in litellm["lines"] if l.startswith("http-request return status 429")]
    assert len(tracks) == 3 and len(limits) == 1
    for l in tracks + limits:
        assert " if lan_bind " in l, l
    assert [l.split()[2] for l in tracks] == [
        "req.hdr(authorization),sha2(256),hex", "req.hdr(x-api-key),sha2(256),hex", "src"]
    assert "sc0_trackers gt" in limits[0] and "hdr Retry-After" in limits[0]
    assert '.if !streq("${EDGE_LITELLM_MAX_CONCURRENT_PER_KEY-8}",0)' in litellm["lines"]
    for s in _haproxy_sections():
        if s["name"] != "litellm":
            assert not [l for l in s["lines"] if "track-sc" in l or "stick-table" in l], s["name"]


def test_every_listen_answers_503_with_retry_after():
    sections = _haproxy_sections()
    (defaults,) = [s for s in sections if s["kind"] == "defaults"]
    default_503 = [l for l in defaults["lines"] if l.startswith("http-error status 503")]
    assert len(default_503) == 1 and "hdr Retry-After" in default_503[0] and "%[be_name]" in default_503[0]
    for s in sections:
        for l in s["lines"]:
            if l.startswith("http-error status 503"):
                assert "hdr Retry-After" in l, s["name"]
                if s["kind"] == "listen":
                    assert f"{s['name']} has no healthy copy" in l, f"listen {s['name']}: 503 must name the service"


def test_edge_env_vars_are_passed_through_and_documented():
    cfg = "\n".join(l.split("#", 1)[0] for l in EDGE_CFG_PATH.read_text(encoding="utf-8").splitlines())
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", cfg))
    assert used == {"EDGE_ALLOWED_CIDRS", "EDGE_LITELLM_MAX_CONCURRENT_PER_KEY"}
    env = _edge_compose_service().get("environment") or {}
    assert set(env) == used, "every variable haproxy.cfg reads must be in the edge compose environment"
    env_example = _env_example()
    claude_md = (_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for var in used:
        assert f"\n{var}=" in env_example, f"{var} missing from .env.example"
        assert f"`{var}`" in claude_md, f"{var} missing from the CLAUDE.md config table"
    # Space-separated (HAProxy's "${VAR[*]}" expansion), never commas -- and
    # docker-proxy's bridge-gateway source must stay allowed.
    cidrs = re.search(r'^EDGE_ALLOWED_CIDRS="([^"]*)"$', env_example, re.M).group(1)
    for value in (cidrs, env["EDGE_ALLOWED_CIDRS"]):
        assert "," not in value and "172.16.0.0/12" in value.replace("}", " ").split()


def test_semantic_router_reaches_litellm_through_the_edge_internal_bind():
    cfg = _yaml().safe_load((_ROOT / "ai" / "semantic-router" / "config.yaml").read_text(encoding="utf-8"))
    urls = {ref["base_url"] for m in cfg["providers"]["models"] for ref in m["backend_refs"]}
    assert urls == {"http://edge:14000/v1"}
    assert ("", 14000, "internal") in _section("listen", "litellm")["binds"]
    assert 14000 not in _edge_published_container_ports()
    render = (_ROOT / "ai" / "semantic-router" / "envoy.yaml").read_text(encoding="utf-8")
    body = render[render.index("\nadmin:"):]
    assert "address: litellm" not in body
    assert body.count("address: edge") == len(cfg["providers"]["models"])


def test_status_page_is_read_only_and_behind_oauth2_proxy():
    status = _section("frontend", "status")
    assert status["binds"] == [("", 8407, "internal")]
    assert "stats uri /edge/" in status["lines"]
    assert not [l for l in status["lines"] if l.startswith("stats admin")]
    upstreams = re.search(r"^OAUTH2_PROXY_UPSTREAMS=(.*)$", _env_example(), re.M).group(1).split(",")
    assert "http://edge:8407/edge/" in upstreams and "http://n8n:5678/n8n/" in upstreams


def test_prometheus_is_on_ai_shared_and_every_target_is_reachable_there():
    services = _all_services()
    _stack, prom, doc = services["prometheus"][0]
    assert _on_ai_shared(prom, doc), "prometheus must be on ai_shared to reach its targets"
    assert deploy.classify("prometheus", prom)[0] == deploy.RECREATE  # TSDB lock
    scrape = _yaml().safe_load((_ROOT / "ai" / "litellm" / "prometheus.yml").read_text(encoding="utf-8"))
    targets = [t for job in scrape["scrape_configs"] for sc in job["static_configs"] for t in sc["targets"]]
    assert "edge:8405" in targets
    for target in targets:
        host = target.rsplit(":", 1)[0]
        rows = services.get(host, [])
        assert rows, f"scrape target {target} is not a compose service key"
        assert any(_on_ai_shared(svc, d) for _s, svc, d in rows), f"{target} is not on ai_shared"
    metrics = _section("frontend", "metrics")
    assert metrics["binds"] == [("", 8405, "internal")]
    assert "http-request use-service prometheus-exporter if { path /metrics }" in metrics["lines"]


def test_deploy_reads_the_same_edge_listeners_as_haproxy_cfg():
    listens = deploy.load_edge_listens(str(_ROOT))
    assert listens == {name: str(bind) for name, (bind, _h, _p) in _haproxy_listens().items()}
    assert listens["litellm"] == "4001"  # the LAN bind, not the internal 14000
    assert not {"stats", "metrics", "status"} & set(listens)


def test_edge_is_recreated_and_waited_for():
    edge = _yaml().safe_load((_ROOT / "ai" / "edge" / "docker-compose.edge.yml").read_text(encoding="utf-8"))
    svc = edge["services"]["edge"]
    assert deploy.classify("edge", svc)[0] == deploy.RECREATE
    assert deploy.service_labels(svc).get(deploy.WAIT_LABEL) == "healthy"
    assert deploy.service_labels(svc).get(deploy.RELOAD_LABEL) == "USR2"
    check = deploy.service_labels(svc).get(deploy.RELOAD_CHECK_LABEL, "")
    assert check.startswith("haproxy -c -f ") and check.endswith("/haproxy.cfg")
    # The check must read the file the running edge loads: the mounted directory.
    mount = next(v for v in svc["volumes"] if v.startswith("./"))
    assert check.split()[-1].startswith(mount.split(":")[1])
    assert "command" not in svc  # the image entrypoint adds -W (master-worker), which USR2 needs


def test_edge_is_the_last_stack_in_the_makefile():
    calls = [line for line in (_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
             if line.startswith("$(eval $(call service,")]
    assert calls[-1] == "$(eval $(call service,edge,edge))"
