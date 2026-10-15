#!/usr/bin/env python3
"""Zero-downtime `make up`: swap a compose service side by side.

`docker compose up -d` stops the old container before the new one starts, so
every caller sees errors for the whole restart. For services labelled
`com.zeo.deploy: "swap"` this script instead:

  1. starts a NEW container from the current config next to the old one
     (`up -d --no-deps --no-recreate --scale S=<running+1> S`) and renames it
     `<name>-<hash>`,
  2. waits for its healthcheck to report healthy (and, when the `ai/edge`
     HAProxy front has a backend named after the service, for HAProxy to
     mark it UP -- a fronted service also checks, before step 1, that the
     edge is running at all; see check_edge_before_swap),
  3. stops the old copy -- `docker stop` honours `stop_grace_period`, so
     in-flight requests finish -- and removes it.

Every other service keeps today's behaviour and the script says why:

  swap      labelled `com.zeo.deploy: swap`, no container_name, no published
            ports, no GPU, a healthcheck. Anything else falls back to recreate.
  oneshot   `restart` is `no` or unset: `up -d --no-deps`, then `docker wait`;
            a non-zero exit stops the deploy.
  recreate  everything else: `up -d --no-deps S`, exactly as before.

The hash is sha256(compose config hash + image ID + content of every
bind-mounted file/directory inside the repo), first 12 hex characters. Compose's
own config hash ignores bind-mount *content*, so without the third part an edit
to e.g. ai/litellm/litellm_config.yaml would never deploy.

Usage (normally through `make up <stack> [service...]`):

    python3 scripts/deploy.py -f ai/litellm/docker-compose.litellm.yml \\
        --env-file .env -p ai-litellm [--dry-run] [--force] [service...]

No services means every service in the stack's default config. Requested
services are deployed together with their transitive `depends_on`, in
dependency order. Stdlib only (Python 3.10+); docker is the only dependency.
See ai/edge/EDGE.md for the operator guide.
"""

from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

SWAP_LABEL = "com.zeo.deploy"
NAME_LABEL = "com.zeo.deploy.name"
TIMEOUT_LABEL = "com.zeo.deploy.timeout"
# On a recreate-class service: "healthy" makes `make up` wait for its
# healthcheck after `up -d` and fail if it never passes (the edge uses it, so a
# full `make up` ends by proving the edge is serving).
WAIT_LABEL = "com.zeo.deploy.wait"
# On a recreate-class service whose config is bind-mounted (compose cannot see
# the edit): when compose would leave it running, `make up` runs the check
# command inside it and, if that passes, sends this signal so it re-reads its
# config in place. The edge: `haproxy -c ...` then USR2 (hitless reload).
RELOAD_LABEL = "com.zeo.deploy.reload"
RELOAD_CHECK_LABEL = "com.zeo.deploy.reload.check"

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
ONEOFF_LABEL = "com.docker.compose.oneoff"
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"

SWAP, RECREATE, ONESHOT = "swap", "recreate", "oneshot"

EDGE_PROJECT = "ai-edge"
EDGE_SERVICE = "edge"
EDGE_CFG = os.path.join("ai", "edge", "haproxy.cfg")
EDGE_STATS_URL = "http://127.0.0.1:8404/stats;csv"
EDGE_TIMEOUT_S = 60.0

POLL_S = 2.0
HEALTH_SLACK_S = 60.0
LOG_TAIL_LINES = 100

MOUNT_MAX_FILES = 2000
MOUNT_MAX_BYTES = 50 * 1024 * 1024
MOUNT_SKIP_DIRS = {".git", "__pycache__"}
NEVER_HASH = {"/var/run/docker.sock"}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DeployError(Exception):
    """A step failed; the deploy stops and exits non-zero."""


# ── docker invocation ───────────────────────────────────────────────────────


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def why(self) -> str:
        text = (self.stderr or self.stdout or "").strip()
        return text.splitlines()[-1] if text else f"exit code {self.returncode}"


# A runner takes the full argv (docker binary first) and whether to capture
# output. capture=False lets compose stream its progress to the terminal.
Runner = Callable[[list, bool], Result]


def subprocess_runner(argv: list, capture: bool) -> Result:
    pipe = subprocess.PIPE if capture else None
    try:
        proc = subprocess.run(
            argv,
            stdout=pipe,
            stderr=pipe,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        return Result(127, "", str(exc))
    return Result(proc.returncode, proc.stdout or "", proc.stderr or "")


class Docker:
    def __init__(self, runner: Runner = subprocess_runner, binary: str = "docker"):
        self.runner = runner
        self.binary = binary

    def __call__(self, *args: str, capture: bool = True) -> Result:
        return self.runner([self.binary, *args], capture)


@dataclass
class Container:
    id: str
    name: str
    state: str
    health: str
    exit_code: int = 0
    created: str = ""
    ips: tuple = ()
    stop_timeout: Optional[int] = None
    labels: dict = field(default_factory=dict)
    image: str = ""
    published: tuple = ()  # host ports actually bound, e.g. ("4001",)

    @property
    def running(self) -> bool:
        return self.state == "running"


def container_from_inspect(d: dict) -> Container:
    state = d.get("State") or {}
    settings = d.get("NetworkSettings") or {}
    networks = settings.get("Networks") or {}
    config = d.get("Config") or {}
    bindings = [b for bs in (settings.get("Ports") or {}).values() for b in (bs or [])]
    return Container(
        id=d.get("Id", ""),
        name=(d.get("Name") or "").lstrip("/"),
        state=state.get("Status", ""),
        health=((state.get("Health") or {}).get("Status")) or "none",
        exit_code=int(state.get("ExitCode") or 0),
        created=d.get("Created", ""),
        ips=tuple(sorted(n["IPAddress"] for n in networks.values() if n and n.get("IPAddress"))),
        stop_timeout=config.get("StopTimeout"),
        labels=config.get("Labels") or {},
        image=d.get("Image", ""),
        published=tuple(sorted({b["HostPort"] for b in bindings if b.get("HostPort")})),
    )


# ── config helpers ──────────────────────────────────────────────────────────

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_S = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value, default: float = 0.0) -> float:
    """Seconds from a compose duration: "1m30s", "500ms", "40s", 30 (s)."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        # compose-go serialises durations as Go strings; a bare number in the
        # JSON would be a time.Duration, i.e. nanoseconds.
        return float(value) / 1e9 if value > 1e6 else float(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise DeployError(f"cannot parse duration {value!r}")
    return sum(float(n) * _UNIT_S[u] for n, u in parts)


def service_labels(svc: dict) -> dict:
    labels = svc.get("labels") or {}
    if isinstance(labels, list):
        return dict(item.split("=", 1) if "=" in item else (item, "") for item in labels)
    return {str(k): "" if v is None else str(v) for k, v in labels.items()}


def published_ports(svc: dict) -> list:
    out = []
    for p in svc.get("ports") or []:
        if isinstance(p, dict):
            host = p.get("host_ip")
            pub = p.get("published") or "(random)"
            out.append(f"{host}:{pub}" if host else str(pub))
        else:
            out.append(str(p))
    return out


def tcp_port_in_use(port: str) -> bool:
    """True when something on this host already listens on TCP `port`.

    Binding 0.0.0.0 without SO_REUSEADDR fails while any address of the port
    is taken, which is exactly what Docker's own bind would hit. Run on the
    Docker host (deploy.py always is); any error other than EADDRINUSE --
    e.g. EACCES for a port below 1024 as a non-root user -- is "don't know",
    so it never blocks a deploy.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", int(port)))
        except OSError as exc:
            return exc.errno == errno.EADDRINUSE
    return False


def host_ports(svc: dict) -> list:
    """Published host port numbers (as strings), for `docker ps --filter publish=`."""
    out = []
    for p in svc.get("ports") or []:
        if isinstance(p, dict):
            pub = str(p.get("published") or "")
        else:  # short syntax "[ip:]host:container"
            parts = str(p).split(":")
            pub = parts[-2] if len(parts) >= 2 else ""
        if pub.isdigit() and pub not in out:
            out.append(pub)
    return out


def has_gpu(svc: dict) -> bool:
    if svc.get("gpus") or svc.get("runtime") == "nvidia":
        return True
    devices = (((svc.get("deploy") or {}).get("resources") or {}).get("reservations") or {}).get("devices") or []
    for dev in devices:
        caps = [c for group in (dev.get("capabilities") or []) for c in (group if isinstance(group, list) else [group])]
        if "gpu" in caps or dev.get("driver") == "nvidia":
            return True
    return any("nvidia" in str(d) for d in svc.get("devices") or [])


def has_healthcheck(svc: dict) -> bool:
    hc = svc.get("healthcheck")
    if not hc or hc.get("disable"):
        return False
    test = hc.get("test")
    if not test:
        return False
    first = test[0] if isinstance(test, list) else str(test)
    return first.upper() != "NONE"


def dependencies(svc: dict) -> list:
    """[(name, condition, required)] from a normalised depends_on."""
    deps = svc.get("depends_on") or {}
    if isinstance(deps, list):
        return [(d, "service_started", True) for d in deps]
    return [
        (name, (spec or {}).get("condition", "service_started"), (spec or {}).get("required", True))
        for name, spec in deps.items()
    ]


def health_timeout(svc: dict) -> float:
    """start_period + interval x retries + slack, or the com.zeo.deploy.timeout label."""
    override = service_labels(svc).get(TIMEOUT_LABEL)
    if override:
        return parse_duration(override)
    hc = svc.get("healthcheck") or {}
    interval = parse_duration(hc.get("interval"), 30.0)
    retries = int(hc.get("retries") or 3)
    start = parse_duration(hc.get("start_period"), 0.0)
    return start + interval * retries + HEALTH_SLACK_S


def classify(name: str, svc: dict) -> tuple:
    """(class, reason). The reason is empty for swap."""
    restart = svc.get("restart") or "no"
    if restart == "no":
        return ONESHOT, "restart is 'no' or unset"

    problems = []
    if svc.get("container_name"):
        problems.append(f"fixed container_name '{svc['container_name']}'")
    ports = published_ports(svc)
    if ports:
        problems.append("publishes host port " + ", ".join(ports))
    if has_gpu(svc):
        problems.append("reserves a GPU")
    mode = svc.get("network_mode") or ""
    if mode == "host" or mode.startswith(("container:", "service:")):
        problems.append(f"network_mode: {mode}")

    if service_labels(svc).get(SWAP_LABEL) != SWAP:
        problems.append(f"not labelled {SWAP_LABEL}=swap (its compose comment says why)")
        return RECREATE, "; ".join(problems)

    if not has_healthcheck(svc):
        problems.append("has no healthcheck")
    scale = svc.get("scale") or (svc.get("deploy") or {}).get("replicas")
    if scale not in (None, 1):
        problems.append(f"scale is {scale}")
    if svc.get("pull_policy") in ("always", "build"):
        problems.append(f"pull_policy: {svc['pull_policy']} changes the image during up")
    if problems:
        return RECREATE, f"labelled {SWAP_LABEL}=swap, but " + "; ".join(problems)
    return SWAP, ""


def dependency_order(services: dict, requested: list) -> list:
    """Requested services plus transitive depends_on, dependencies first."""
    order, state = [], {}

    def visit(name: str, path: list) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            raise DeployError("dependency cycle: " + " -> ".join(path + [name]))
        state[name] = "visiting"
        for dep, _cond, _req in dependencies(services.get(name) or {}):
            if dep in services:
                visit(dep, path + [name])
        state[name] = "done"
        order.append(name)

    for name in requested:
        visit(name, [])
    return order


# ── bind-mount content ──────────────────────────────────────────────────────


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


def _hash_file(path: str, h) -> None:
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)


def _dir_digest(path: str):
    """(hexdigest, None) or (None, why-skipped) for a bind-mounted directory."""
    files, total = [], 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d not in MOUNT_SKIP_DIRS)
        for fname in sorted(filenames):
            if fname.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fname)
            if not os.path.isfile(full):
                continue
            files.append(full)
            total += os.path.getsize(full)
            if len(files) > MOUNT_MAX_FILES:
                return None, f"more than {MOUNT_MAX_FILES} files"
            if total > MOUNT_MAX_BYTES:
                return None, f"more than {MOUNT_MAX_BYTES // (1024 * 1024)} MB"
    h = hashlib.sha256()
    for full in files:
        rel = os.path.relpath(full, path).replace(os.sep, "/")
        h.update(f"{rel}\0".encode())
        _hash_file(full, h)
        h.update(b"\0")
    return h.hexdigest(), None


def mount_digest(svc: dict, repo_root: str, config: Optional[dict] = None):
    """(hexdigest, notes) over every bind-mounted file/dir inside the repo."""
    root = os.path.realpath(repo_root)
    sources = []
    for vol in svc.get("volumes") or []:
        if isinstance(vol, dict) and vol.get("type") == "bind":
            sources.append(vol.get("source"))
    for kind in ("configs", "secrets"):
        top = (config or {}).get(kind) or {}
        for ref in svc.get(kind) or []:
            key = ref.get("source") if isinstance(ref, dict) else ref
            spec = top.get(key) or {}
            if spec.get("file"):
                sources.append(spec["file"])

    h, notes = hashlib.sha256(), []
    for src in sources:
        if not src or src in NEVER_HASH:
            continue
        real = os.path.realpath(src)
        if not _inside(real, root):
            continue
        rel = os.path.relpath(real, root).replace(os.sep, "/")
        if os.path.isfile(real):
            h.update(f"file {rel}\n".encode())
            _hash_file(real, h)
            h.update(b"\n")
        elif os.path.isdir(real):
            digest, why = _dir_digest(real)
            if digest is None:
                notes.append(f"bind mount {rel}/ not hashed ({why}); a change in it will not trigger a swap -- use FORCE=1")
                continue
            h.update(f"dir {rel} {digest}\n".encode())
        else:
            h.update(f"missing {rel}\n".encode())
    return h.hexdigest(), notes


def deploy_hash(config_hash: str, image_id: str, mounts: str) -> str:
    raw = f"config {config_hash}\nimage {image_id}\nmounts {mounts}\n"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def parse_edge_listens(text: str) -> dict:
    """{service key: listener port} for every `listen` section of haproxy.cfg.

    The section name is the compose service key it fronts (see the header of
    ai/edge/haproxy.cfg); its FIRST `bind` port is the default LAN port. A
    later bind is internal and never published (litellm's `:14000 name
    internal`, the semantic router's path to LiteLLM), so it is ignored, as
    are `frontend` sections (stats, metrics, status page).
    """
    out, current = {}, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        words = line.split()
        if not raw[:1].isspace():
            current = words[1] if words[0] == "listen" and len(words) > 1 else None
            if current:
                out[current] = ""
        elif current and words[0] == "bind" and len(words) > 1 and not out[current]:
            out[current] = words[1].rsplit(":", 1)[-1]
    return out


def load_edge_listens(repo_root: str) -> dict:
    try:
        with open(os.path.join(repo_root, EDGE_CFG), encoding="utf-8") as fh:
            return parse_edge_listens(fh.read())
    except OSError:
        return {}


def parse_edge_stats(text: str, backend: str) -> list:
    """Server rows of one backend from HAProxy's `stats;csv` output."""
    lines = text.splitlines()
    if not lines:
        return []
    lines[0] = lines[0].lstrip("# ")
    rows = csv.DictReader(io.StringIO("\n".join(lines)))
    return [
        r for r in rows
        if r.get("pxname") == backend and r.get("svname") not in ("FRONTEND", "BACKEND")
    ]


# ── the deployer ────────────────────────────────────────────────────────────


class Deployer:
    def __init__(
        self,
        docker: Docker,
        files: list,
        project: str,
        env_file: Optional[str] = None,
        *,
        repo_root: str = REPO_ROOT,
        dry_run: bool = False,
        force: bool = False,
        out=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        edge_listens: Optional[dict] = None,
        probe_port: Optional[Callable[[str], bool]] = None,
    ):
        self.docker = docker
        self.probe_port = probe_port or tcp_port_in_use
        self.files = list(files)
        self.project = project
        self.env_file = env_file
        self.repo_root = repo_root
        self.dry_run = dry_run
        self.force = force
        self.out = out or sys.stdout
        self.sleep = sleep
        self.clock = clock
        self.config: dict = {}
        # (container, target name) of a copy renamed aside during a swap; see abort().
        self.parked: Optional[tuple] = None
        self.services: dict = {}
        # {service key: LAN port} the edge fronts, from ai/edge/haproxy.cfg.
        self.edge_listens = load_edge_listens(repo_root) if edge_listens is None else dict(edge_listens)
        # [(service, port)] swapped while the edge was not running: those LAN
        # ports have no listener until `make up edge`. Reported at the end.
        self.edge_missing: list = []

    # output -----------------------------------------------------------------

    def say(self, service: str, message: str) -> None:
        print(f"[{self.project}] {service}: {message}", file=self.out, flush=True)

    def detail(self, message: str) -> None:
        print(f"    {message}", file=self.out, flush=True)

    # docker helpers ---------------------------------------------------------

    def compose(self, *args: str, capture: bool = True) -> Result:
        base = ["compose"]
        for f in self.files:
            base += ["-f", f]
        if self.env_file:
            base += ["--env-file", self.env_file]
        base += ["-p", self.project]
        return self.docker(*base, *args, capture=capture)

    def must(self, result: Result, what: str) -> Result:
        if not result.ok:
            raise DeployError(f"{what} failed: {result.why()}")
        return result

    def load_config(self) -> None:
        r = self.must(self.compose("config", "--format", "json"), "docker compose config")
        try:
            self.config = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            raise DeployError(f"docker compose config printed invalid JSON: {exc}") from exc
        self.services = self.config.get("services") or {}

    def containers(self, service: str, project: Optional[str] = None, running_only: bool = False) -> list:
        args = ["ps", "-q" if running_only else "-aq", "--no-trunc",
                "--filter", f"label={PROJECT_LABEL}={project or self.project}",
                "--filter", f"label={SERVICE_LABEL}={service}",
                "--filter", f"label={ONEOFF_LABEL}=False"]
        for _ in range(2):
            r = self.must(self.docker(*args), "docker ps")
            ids = r.stdout.split()
            if not ids:
                return []
            r = self.docker("inspect", *ids)
            if r.ok:
                found = [container_from_inspect(d) for d in json.loads(r.stdout or "[]")]
                return sorted(found, key=lambda c: c.created)
            # a container vanished between ps and inspect; list again once
        raise DeployError(f"docker inspect failed: {r.why()}")

    def refresh(self, c: Container) -> Optional[Container]:
        r = self.docker("inspect", c.id)
        if not r.ok:
            return None
        data = json.loads(r.stdout or "[]")
        return container_from_inspect(data[0]) if data else None

    def config_hash(self, service: str) -> str:
        r = self.must(self.compose("config", f"--hash={service}"), f"docker compose config --hash={service}")
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == service:
                return parts[1]
        raise DeployError(f"docker compose config --hash={service} printed no hash for {service}")

    def image_ref(self, service: str, svc: dict) -> str:
        return svc.get("image") or f"{self.project}-{service}"

    def image_id(self, service: str, svc: dict, fetch: bool) -> Optional[str]:
        ref = self.image_ref(service, svc)
        r = self.docker("image", "inspect", "--format", "{{.Id}}", ref)
        if r.ok and r.stdout.strip():
            return r.stdout.strip()
        if not fetch:
            return None
        action = "build" if svc.get("build") else "pull"
        if self.dry_run:
            self.detail(f"image {ref} is not present locally; would run docker compose {action} {service}")
            return "missing"
        self.detail(f"image {ref} is not present locally; running docker compose {action} {service}")
        self.must(self.compose(action, service, capture=False), f"docker compose {action} {service}")
        r = self.docker("image", "inspect", "--format", "{{.Id}}", ref)
        if not (r.ok and r.stdout.strip()):
            raise DeployError(f"image {ref} is still missing after docker compose {action}")
        return r.stdout.strip()

    def print_logs(self, c: Container) -> None:
        r = self.docker("logs", "--tail", str(LOG_TAIL_LINES), c.id)
        self.detail(f"last {LOG_TAIL_LINES} log lines of {c.name}:")
        for line in (r.stdout + r.stderr).splitlines()[-LOG_TAIL_LINES:]:
            print(f"      | {line}", file=self.out, flush=True)

    def remove(self, c: Container) -> None:
        r = self.docker("rm", "-f", c.id)
        if not r.ok:
            self.detail(f"could not remove {c.name}: {r.why()}")

    # orchestration ----------------------------------------------------------

    def run(self, requested: list) -> int:
        try:
            self.load_config()
            targets = list(requested) or list(self.services)
            order = dependency_order(self.services, targets)
        except DeployError as exc:
            print(f"[{self.project}] FAILED: {exc}", file=self.out, flush=True)
            return 1
        added = [s for s in order if s not in targets]
        prefix = "DRY RUN -- nothing will change. " if self.dry_run else ""
        note = f" (pulled in by depends_on: {', '.join(added)})" if added else ""
        print(f"[{self.project}] {prefix}order: {', '.join(order)}{note}", file=self.out, flush=True)

        rc = 0
        for name in order:
            try:
                self.deploy_one(name)
            except DeployError as exc:
                self.say(name, f"FAILED: {exc}")
                rc = 1
                break
        self.report_edge_missing()
        return rc

    def report_edge_missing(self) -> None:
        if not self.edge_missing:
            return
        ports = ", ".join(f"{port} ({svc})" for svc, port in self.edge_missing)
        verb = "would have" if self.dry_run else "have"
        print(
            f"[{self.project}] WARNING: the edge is not running, so these LAN ports {verb} no listener: "
            f"{ports}. Run `make up edge` (ai/edge/EDGE.md § One-time cutover).",
            file=self.out, flush=True,
        )

    def deploy_one(self, name: str) -> None:
        svc = self.services.get(name)
        if svc is None:
            self.say(name, "recreate (downtime): not in the stack's default config (profile-gated?); handing it to compose as-is")
            self.compose_up(name)
            return
        cls, reason = classify(name, svc)
        if not self.dry_run:
            self.wait_for_dependencies(name, svc)
        if cls == ONESHOT:
            self.oneshot(name, svc)
        elif cls == SWAP:
            self.swap(name, svc)
        else:
            self.recreate(name, svc, reason)

    def compose_up(self, name: str, force: bool = False) -> None:
        args = ["up", "-d", "--no-deps"] + (["--force-recreate"] if force else []) + [name]
        if self.dry_run:
            self.detail(f"would run: docker compose {' '.join(args)}")
            return
        self.must(self.compose(*args, capture=False), f"docker compose up {name}")

    def wait_until(self, check: Callable[[], bool], timeout: float, what: str) -> None:
        deadline = self.clock() + timeout
        while not check():
            if self.clock() >= deadline:
                raise DeployError(f"timed out after {int(timeout)}s waiting for {what}")
            self.sleep(POLL_S)

    def wait_for_dependencies(self, name: str, svc: dict) -> None:
        # `up --no-deps` skips compose's own depends_on gating, so do it here.
        for dep, condition, _required in dependencies(svc):
            if dep not in self.services:
                continue
            if condition == "service_healthy":
                self.wait_until(
                    lambda dep=dep: any(c.running and c.health == "healthy" for c in self.containers(dep)),
                    health_timeout(self.services[dep]),
                    f"{dep} to be healthy (needed by {name})",
                )
            elif condition == "service_completed_successfully":
                done = [c for c in self.containers(dep) if c.state == "exited"]
                if not done or done[-1].exit_code != 0:
                    raise DeployError(f"{dep} has not completed successfully (needed by {name})")
            else:
                self.wait_until(
                    lambda dep=dep: any(c.running for c in self.containers(dep)),
                    HEALTH_SLACK_S,
                    f"{dep} to be running (needed by {name})",
                )

    # recreate -----------------------------------------------------------------

    def recreate_unchanged(self, name: str, svc: dict) -> bool:
        """True when compose will leave the running container alone."""
        try:
            expected = self.config_hash(name)
            running = [c for c in self.containers(name) if c.running]
            image = self.image_id(name, svc, fetch=False)
        except DeployError:
            return False
        return bool(running) and all(
            c.labels.get(CONFIG_HASH_LABEL) == expected and (image is None or c.image == image)
            for c in running
        )

    def unbound_ports(self, name: str, svc: dict) -> list:
        """Host ports the config publishes that a running container of this service lacks.

        Docker can leave a container running with NO port bindings: create
        it, fail its first start with "port is already allocated", free the
        port, start it again. Its config hash still matches, so compose (and a
        reload) would leave it alone forever -- serving nothing on those ports.
        """
        wanted = host_ports(svc)
        if not wanted:
            return []
        missing = []
        for c in self.containers(name):
            if c.running:
                missing += [p for p in wanted if p not in c.published and p not in missing]
        return missing

    def recreate(self, name: str, svc: dict, reason: str) -> None:
        labels = service_labels(svc)
        unchanged = self.recreate_unchanged(name, svc)
        unbound = self.unbound_ports(name, svc) if unchanged else []
        if unbound:
            self.say(name, f"recreate (downtime): its running container is missing host port binding(s) "
                           f"{', '.join(unbound)} (started after a failed port allocation?); forcing a recreate")
            self.check_ports_free(name, svc)
            self.compose_up(name, force=True)
            if labels.get(WAIT_LABEL) == "healthy":
                self.wait_recreated_healthy(name, svc)
            return
        signal = labels.get(RELOAD_LABEL)
        if unchanged and signal:
            self.say(name, f"recreate-class, unchanged -- reloading in place with {signal} ({reason})")
            self.reload_in_place(name, signal, labels.get(RELOAD_CHECK_LABEL))
        else:
            if unchanged:
                self.say(name, f"recreate-class, unchanged -- compose leaves it running ({reason})")
            else:
                self.say(name, f"recreate (downtime): {reason}")
                self.check_ports_free(name, svc)
            self.compose_up(name)
        if labels.get(WAIT_LABEL) == "healthy":
            self.wait_recreated_healthy(name, svc)

    def check_ports_free(self, name: str, svc: dict) -> None:
        """Fail before `up` when another service's container holds one of our host ports.

        Otherwise compose creates the container, fails to start it with
        "port is already allocated", and leaves it half-made -- the usual case
        being the edge before the one-time cutover, while an old pre-edge
        container (litellm, ...) still publishes the port. Its own container
        holding the port is fine: compose stops it before starting the new one.
        """
        holders, foreign = {}, []
        for port in host_ports(svc):
            r = self.docker("ps", "-q", "--no-trunc", "--filter", f"publish={port}")
            ids = r.stdout.split() if r.ok else []
            containers = []
            if ids:
                r = self.docker("inspect", *ids)
                if r.ok:
                    containers = [container_from_inspect(d) for d in json.loads(r.stdout or "[]")]
            if not containers:
                # No container claims it, but a host process might: a leftover
                # docker-proxy, a host Postgres, an SSH tunnel. Docker then fails
                # the start with "address already in use" after creating the
                # container -- and the old one is already gone.
                if self.probe_port(port):
                    foreign.append(port)
                continue
            for c in containers:
                if (c.labels.get(PROJECT_LABEL), c.labels.get(SERVICE_LABEL)) == (self.project, name):
                    continue
                holders.setdefault(port, []).append(c)
        if not holders and not foreign:
            return
        lines, stacks = [], []
        for port in foreign:
            lines.append(f"host port {port} is in use by a process outside Docker "
                         f"(find it with: sudo ss -ltnp 'sport = :{port}')")
        for port, cs in holders.items():
            for c in cs:
                project = c.labels.get(PROJECT_LABEL) or ""
                lines.append(f"host port {port} is held by {c.name}" + (f" (project {project})" if project else ""))
                if project.startswith("ai-") and project[3:] not in stacks:
                    stacks.append(project[3:])
        if stacks:
            hint = "; deploy those first so they release it: " + ", ".join(f"`make up {s}`" for s in stacks)
        elif holders:
            hint = "; stop that container first"
        else:
            hint = "; free it (a leftover docker-proxy can be killed by its pid), then retry"
        if name == EDGE_SERVICE and self.project == EDGE_PROJECT:
            hint += " (ai/edge/EDGE.md § One-time cutover)"
        message = "; ".join(lines) + hint
        if self.dry_run:
            self.detail(f"would fail: {message}")
            return
        raise DeployError(message)

    def reload_in_place(self, name: str, signal: str, check: Optional[str]) -> None:
        """Validate the bind-mounted config inside the running container, then signal it.

        Compose cannot see a change to a bind-mounted file, so a recreate-class
        service that re-reads its config on a signal (the edge: HAProxy
        master-worker, USR2) is reloaded on every `make up`. It is hitless, so
        reloading an unchanged config costs nothing. A failed check stops the
        deploy before the signal, leaving the running config in place.
        """
        if self.dry_run:
            if check:
                self.detail(f"would run `{check}` in the running container, then send {signal} if it passes")
            else:
                self.detail(f"would send {signal} to the running container")
            return
        running = [c for c in self.containers(name) if c.running]
        if not running:
            raise DeployError("no running container to reload")
        c = running[-1]
        if check:
            r = self.docker("exec", c.id, *shlex.split(check))
            if not r.ok:
                for line in (r.stdout + r.stderr).splitlines()[-LOG_TAIL_LINES:]:
                    print(f"      | {line}", file=self.out, flush=True)
                raise DeployError(f"`{check}` failed in {c.name}; not reloading, the running config is untouched")
            self.detail(f"`{check}` passed")
        self.must(self.docker("kill", "-s", signal, c.id), f"docker kill -s {signal} {c.name}")
        self.detail(f"sent {signal} to {c.name}")

    def wait_recreated_healthy(self, name: str, svc: dict) -> None:
        timeout = health_timeout(svc)
        if self.dry_run:
            self.detail(f"would wait up to {int(timeout)}s for it to report healthy ({WAIT_LABEL}=healthy)")
            return
        self.detail(f"waiting up to {int(timeout)}s for it to report healthy")
        deadline = self.clock() + timeout
        while True:
            found = self.containers(name)
            c = found[-1] if found else None
            if c is not None and c.running and c.health == "healthy":
                self.detail(f"{c.name} is healthy")
                return
            failed = c is None or not c.running or c.health == "unhealthy"
            if failed or self.clock() >= deadline:
                if c is not None:
                    self.print_logs(c)
                what = "is not running" if c is None or not c.running else f"is {c.health}"
                raise DeployError(f"{c.name if c else name} {what} after `up -d`")
            self.sleep(POLL_S)

    # oneshot ------------------------------------------------------------------

    def oneshot(self, name: str, svc: dict) -> None:
        self.say(name, "oneshot")
        if self.dry_run:
            self.detail(f"would run: docker compose up -d --no-deps {name}, then docker wait for exit 0")
            return
        self.compose_up(name)
        found = self.containers(name)
        if not found:
            raise DeployError("compose started no container")
        c = found[-1]
        r = self.must(self.docker("wait", c.id), f"docker wait {c.name}")
        try:
            code = int(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise DeployError(f"docker wait printed {r.stdout.strip()!r}, not an exit code") from None
        if code != 0:
            self.print_logs(c)
            raise DeployError(f"{c.name} exited with code {code}")
        self.detail(f"{c.name} exited 0")

    # swap ---------------------------------------------------------------------

    def swap(self, name: str, svc: dict) -> None:
        designated = service_labels(svc).get(NAME_LABEL) or name
        cfg = self.config_hash(name)
        image = self.image_id(name, svc, fetch=True)
        mounts, notes = mount_digest(svc, self.repo_root, self.config)
        target = f"{designated}-{deploy_hash(cfg, image, mounts)}"

        found = self.containers(name)
        stale = [c for c in found if not c.running]
        running = [c for c in found if c.running]
        current = next((c for c in running if c.name == target), None)
        current_ok = current is not None and current.health == "healthy" and not self.force

        if current_ok and len(running) == 1:
            self.say(name, f"up to date ({target})")
            return

        others = [c.name for c in running if c is not current]
        if current is not None and not current_ok:
            # FORCE=1, or an unhealthy copy already at the target name: it is
            # replaced too (parked aside below), so it is not "no copy".
            others = [f"{current.name} ({'forced' if self.force else current.health})"] + others
        replacing = f" (replacing {', '.join(others)})" if others else " (no copy running yet)"
        self.say(name, f"swap -> {target}{replacing}")
        for n in notes:
            self.detail(n)
        self.check_edge_before_swap(name, [c for c in running if c is not current])
        timeout = health_timeout(svc)
        if self.dry_run:
            for c in stale:
                self.detail(f"would remove leftover container {c.name} ({c.state})")
            if current_ok:
                self.detail(f"{target} is already running and healthy; would retire {', '.join(others)}")
            else:
                self.detail(f"would start {target} beside the running copy and wait up to {int(timeout)}s for healthy")
                for c in running:
                    self.detail(f"then retire {c.name}: docker stop (grace {c.stop_timeout or 10}s) + rm")
            return

        for c in stale:
            self.detail(f"removing leftover container {c.name} ({c.state})")
            self.must(self.docker("rm", "-f", c.id), f"docker rm {c.name}")

        old = [c for c in running if c is not current]
        created = False
        self.parked = None
        if current_ok:
            self.detail(f"{target} is already running and healthy; retiring the other copies")
            new = current
        else:
            if current is not None:
                parked = f"{target}-old-{current.id[:8]}"
                self.detail(f"renaming {current.name} ({current.health}) to {parked}; it retires once the new copy is healthy")
                self.must(self.docker("rename", current.id, parked), f"docker rename {current.name}")
                current.name = parked
                old.append(current)
                # abort() gives it its name back if the new copy is thrown away.
                self.parked = (current, target)
            new = self.start_copy(name, target, scale=len(old) + 1)
            created = True
            new = self.wait_healthy(name, new, timeout, old)

        try:
            self.wait_edge(name, new)
        except DeployError as exc:
            if created:
                self.abort(new, str(exc), old)
            raise

        for c in old:
            self.retire(c)
        self.detail(f"done: {target} is serving")

    def start_copy(self, name: str, target: str, scale: int) -> Container:
        before = {c.id for c in self.containers(name)}
        self.detail(f"starting {target} (docker compose up --no-recreate --scale {name}={scale})")
        r = self.compose("up", "-d", "--no-deps", "--no-recreate", "--scale", f"{name}={scale}", name, capture=False)
        fresh = [c for c in self.containers(name) if c.id not in before]
        if not r.ok or len(fresh) != 1:
            for c in fresh:
                self.remove(c)
            if not r.ok:
                raise DeployError(f"docker compose up --scale {name}={scale} failed: {r.why()}")
            raise DeployError(f"expected compose to create one new container, it created {len(fresh)}")
        new = fresh[0]
        r = self.docker("rename", new.id, target)
        if not r.ok:
            self.remove(new)
            raise DeployError(f"docker rename {new.name} {target} failed: {r.why()}")
        new.name = target
        return new

    def wait_healthy(self, name: str, new: Container, timeout: float, old: list) -> Container:
        self.detail(f"waiting up to {int(timeout)}s for {new.name} to report healthy")
        deadline = self.clock() + timeout
        while True:
            c = self.refresh(new)
            if c is None:
                raise DeployError(f"{new.name} disappeared while starting; {self.serving(old)}")
            if not c.running:
                self.abort(c, f"{c.name} exited (code {c.exit_code}) before becoming healthy", old)
            if c.health == "healthy":
                self.detail(f"{c.name} is healthy")
                return c
            if c.health == "unhealthy":
                self.abort(c, f"{c.name} reported unhealthy", old)
            if self.clock() >= deadline:
                self.abort(c, f"{c.name} was not healthy after {int(timeout)}s", old)
            self.sleep(POLL_S)

    def serving(self, old: list) -> str:
        return f"{', '.join(c.name for c in old)} keeps serving" if old else "no copy was running before"

    def abort(self, new: Container, reason: str, old: list) -> None:
        """Throw the new copy away, gracefully, and leave the old one exactly as it was.

        `docker stop`, not `rm -f`: by now the edge (or Docker DNS) may already be
        sending it requests -- the edge-wait abort happens after it was healthy --
        and a kill drops those mid-flight. Then a copy parked under
        `<target>-old-<id>` gets its name back, or the next run would see no
        container at the target name and swap again instead of "up to date".
        """
        self.print_logs(new)
        self.detail(f"stopping the new copy {new.name} (docker stop, grace {new.stop_timeout or 10}s)")
        self.docker("stop", new.id)
        self.remove(new)
        parked, self.parked = self.parked, None
        if parked is not None:
            c, target = parked
            r = self.docker("rename", c.id, target)
            if r.ok:
                c.name = target
                self.detail(f"renamed {c.id[:12]} back to {target}")
            else:
                self.detail(f"could not rename {c.name} back to {target} ({r.why()}); the next run swaps it")
        raise DeployError(f"{reason}; removed the new copy, {self.serving(old)}")

    def edge_container(self) -> Optional[Container]:
        edges = self.containers(EDGE_SERVICE, project=EDGE_PROJECT, running_only=True)
        return edges[0] if edges else None

    def check_edge_before_swap(self, name: str, old: list) -> None:
        """Refuse or warn when the edge that fronts this service is not running.

        A fronted service publishes no host port of its own, so with the edge
        down its LAN port has no listener. Three cases:
          * an old copy still publishes the port itself -- the one-time cutover,
            where the edge cannot start until the swaps free the ports: warn;
          * nothing is running yet (a fresh box; the edge comes up later in a
            full `make up`): warn;
          * otherwise the edge has gone missing after the cutover: refuse,
            unless --force.
        """
        port = self.edge_listens.get(name)
        if port is None or self.edge_container() is not None:
            return
        holders = [c for c in old if c.published]
        if holders:
            self.detail(
                f"WARNING: the edge is not running. {holders[0].name} still publishes host port "
                f"{', '.join(holders[0].published)} itself (the one-time cutover); once it retires, "
                f"LAN port {port} has no listener until `make up edge`"
            )
        elif not old:
            self.detail(f"WARNING: the edge is not running; LAN port {port} has no listener until `make up edge`")
        elif self.force:
            self.detail(f"WARNING: the edge is not running, so LAN port {port} has no listener; swapping anyway (FORCE=1)")
        else:
            raise DeployError(
                f"the edge is not running, so LAN port {port} has no listener and the new copy cannot be "
                f"checked through it. Run `make up edge` first, or FORCE=1 to swap anyway"
            )
        self.edge_missing.append((name, port))

    def wait_edge(self, name: str, new: Container) -> None:
        """Wait until the edge HAProxy marks the new copy UP, if it fronts this service."""
        if name not in self.edge_listens:
            return
        edge = self.edge_container()
        if edge is None:
            return  # check_edge_before_swap already warned or refused
        if not new.ips:
            self.detail(f"{new.name} has no network address; not waiting for the edge")
            return
        ips = set(new.ips)
        deadline = self.clock() + EDGE_TIMEOUT_S
        announced = False
        while True:
            r = self.docker("exec", edge.id, "wget", "-qO-", EDGE_STATS_URL)
            if not r.ok:
                raise DeployError(
                    f"could not read the edge's stats ({r.why()}), so there is no proof it routes to "
                    f"{new.name}; not retiring the old copy"
                )
            rows = parse_edge_stats(r.stdout, name)
            if not rows:
                raise DeployError(
                    f"the running edge has no backend '{name}' although {EDGE_CFG} has one -- it is "
                    f"running an older config; run `make up edge` to reload it, then retry"
                )
            servers = [r for r in rows if r.get("svname") not in ("FRONTEND", "BACKEND")]
            if servers and not any((r.get("addr") or "").strip() for r in servers):
                # HAProxy fills `addr` only with `stats show-legends`; without
                # it no row can ever match, so do not wait out the timeout.
                raise DeployError(
                    f"the edge's stats show no server addresses for '{name}', so the new copy cannot be "
                    f"identified -- the edge's stats frontend (127.0.0.1:8404) needs `stats show-legends` "
                    f"in {EDGE_CFG}; add it and run `make up edge`"
                )
            if not announced:
                self.detail(f"waiting up to {int(EDGE_TIMEOUT_S)}s for the edge to mark it UP")
                announced = True
            for row in rows:
                host = (row.get("addr") or "").rsplit(":", 1)[0].strip("[]")
                if host in ips and (row.get("status") or "").strip() == "UP":
                    self.detail(f"edge backend {name}/{row.get('svname')} is UP")
                    return
            if self.clock() >= deadline:
                seen = "; ".join(
                    f"{row.get('svname')}={(row.get('status') or '').strip() or '?'}@{row.get('addr') or '-'}"
                    for row in rows
                )
                raise DeployError(
                    f"the edge did not mark {new.name} UP within {int(EDGE_TIMEOUT_S)}s "
                    f"(looked for {', '.join(sorted(ips))}; backend '{name}' shows: {seen})"
                )
            self.sleep(POLL_S)

    def retire(self, c: Container) -> None:
        self.detail(f"retiring {c.name}: docker stop (grace {c.stop_timeout or 10}s), then rm")
        r = self.docker("stop", c.id)
        if not r.ok:
            self.detail(f"docker stop {c.name} failed ({r.why()}); forcing removal")
        self.must(self.docker("rm", "-f", c.id), f"docker rm {c.name}")


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[list] = None, runner: Runner = subprocess_runner, out=None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy a compose stack, swapping com.zeo.deploy=swap services side by side.",
    )
    parser.add_argument("-f", "--file", action="append", required=True, help="compose file (repeatable)")
    parser.add_argument("-p", "--project-name", required=True, help="compose project name, e.g. ai-litellm")
    parser.add_argument("--env-file", help="passed through to docker compose")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument("--force", action="store_true", help="swap swap-class services even when up to date")
    parser.add_argument("--docker", default="docker", help="docker binary (default: docker)")
    parser.add_argument("--repo-root", default=REPO_ROOT, help="bind mounts under this directory are hashed")
    parser.add_argument("services", nargs="*", help="services to deploy (default: every service)")
    args = parser.parse_args(argv)

    deployer = Deployer(
        Docker(runner, args.docker),
        args.file,
        args.project_name,
        args.env_file,
        repo_root=args.repo_root,
        dry_run=args.dry_run,
        force=args.force,
        out=out,
        sleep=sleep,
        clock=clock,
    )
    return deployer.run(args.services)


if __name__ == "__main__":
    sys.exit(main())
