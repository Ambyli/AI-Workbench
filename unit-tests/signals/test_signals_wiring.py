"""The signals subsystem is wired into the repo the way CLAUDE.md asks.

* compose: swap-class (label, healthcheck, stop_grace_period, no
  container_name / ports -- deploy.py classifies it ``swap``), on ai_shared
  only, read-only with a tmpfs /tmp, no volume, the CPU pinning knob in its
  ``${VAR-default}`` form, the thread / cache env vars;
* the image: python 3.12-slim, the locked workspace install, the models
  baked and checked at build time, a non-root user, no torch / transformers;
* the workspace member and its lock entry;
* the Postman collection covers every route the app serves, from
  ``{{signals}}``, and its /signals bodies are valid wire requests;
* the files that run in Linux are LF.

The edge listener, the Makefile stack, the Prometheus job, .env.example and
the docs tables are workstream D's (unit-tests/deploy covers the edge
invariants every fronted service shares).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

import yaml

from common.signals import SignalsRequest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "signals"
COMPOSE = SERVICE_DIR / "docker-compose.signals.yml"
COLLECTION = SERVICE_DIR / "signals.postman_collection.json"
SKIP_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _deploy():
    if "deploy" in sys.modules:
        return sys.modules["deploy"]
    spec = importlib.util.spec_from_file_location("deploy", ROOT / "scripts" / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy"] = module
    spec.loader.exec_module(module)
    return module


def _compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _service():
    return _compose()["services"]["signals"]


def _env(svc) -> dict:
    return dict(item.split("=", 1) for item in svc["environment"])


def test_the_service_is_swap_class():
    deploy = _deploy()
    svc = _service()
    assert list(_compose()["services"]) == ["signals"]
    assert deploy.classify("signals", svc) == (deploy.SWAP, "")
    assert "container_name" not in svc and "ports" not in svc
    assert svc["restart"] == "unless-stopped"
    assert deploy.parse_duration(svc["stop_grace_period"]) >= 10
    test = " ".join(svc["healthcheck"]["test"])
    assert "http://localhost:8000/health" in test


def test_it_is_on_ai_shared_only_with_nothing_writable_but_tmp():
    doc, svc = _compose(), _service()
    assert svc["networks"] == ["shared"] and doc["networks"]["shared"] == {"external": True, "name": "ai_shared"}
    assert svc["read_only"] is True and svc["tmpfs"] == ["/tmp"]
    assert "volumes" not in svc and "volumes" not in doc
    assert svc["cap_drop"] == ["ALL"] and "no-new-privileges:true" in svc["security_opt"]
    assert "deploy" not in svc                                      # no GPU reservation, no replicas


def test_cpu_pinning_defaults_to_the_boxs_cores_and_an_empty_value_unpins():
    svc = _service()
    # Without the colon: unset -> 24-31; SIGNALS_CPUSET= (empty) -> no pinning.
    assert svc["cpuset"] == "${SIGNALS_CPUSET-24-31}"
    assert svc["cpu_shares"] == 2048
    assert svc["mem_limit"] == "${SIGNALS_MEM_LIMIT:-4g}"


def test_the_env_vars_the_service_reads_are_passed_through():
    env = _env(_service())
    assert env["SIGNALS_THREADS_PII"] == "${SIGNALS_THREADS_PII:-2}"
    assert env["SIGNALS_THREADS_EMBED"] == "${SIGNALS_THREADS_EMBED:-2}"
    assert env["SIGNALS_THREADS_INJECTION"] == "${SIGNALS_THREADS_INJECTION:-2}"
    assert env["SIGNALS_CACHE_ENTRIES"] == "${SIGNALS_CACHE_ENTRIES:-20000}"
    assert env["HF_HUB_OFFLINE"] == "1" and env["LOG_LEVEL"] == "INFO"
    read = set(re.findall(r'"(SIGNALS_[A-Z_]+)"', (SERVICE_DIR / "signals" / "config.py").read_text(encoding="utf-8")))
    # Every SIGNALS_* variable config.py reads is set by compose, except the
    # models dir (fixed in the image's ENV).
    assert {"SIGNALS_THREADS_PII", "SIGNALS_CACHE_ENTRIES", "SIGNALS_MODELS_DIR"} <= read
    assert read - {"SIGNALS_MODELS_DIR"} <= set(env)


def test_the_image_is_cpu_only_locked_baked_and_unprivileged():
    dockerfile = (SERVICE_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.splitlines()[[l.startswith("FROM ") for l in dockerfile.splitlines()].index(True)] == \
        "FROM python:3.12-slim"
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "RUN uv sync --package signals --no-dev --frozen" in dockerfile
    assert "COPY ai/signals/models.yaml ./ai/signals/" in dockerfile
    assert "python -m signals.runtime --check" in dockerfile
    assert "PYTHONPATH=/app/ai/signals" in dockerfile and "HF_HUB_OFFLINE=1" in dockerfile
    assert re.search(r"^USER signals$", dockerfile, re.M)
    assert '"signals.app:app"' in dockerfile and '"8000"' in dockerfile
    deps = (SERVICE_DIR / "pyproject.toml").read_text(encoding="utf-8").lower()
    for banned in ("torch", "transformers", "huggingface", "optimum"):
        assert f'"{banned}' not in deps, banned


def test_the_dockerfile_app_check_runs():
    # The build's `python -c` app check, run here with the same PYTHONPATH,
    # so a check that breaks on a dependency bump (FastAPI 0.139 made an
    # included router an `_IncludedRouter` in app.routes, with no `.path`)
    # fails in the unit tests, not halfway through a build on the box.
    import subprocess

    dockerfile = (SERVICE_DIR / "Dockerfile").read_text(encoding="utf-8")
    (code,) = re.findall(r'python -c "(from signals\.app import app;[^"]*)"', dockerfile)
    env = {**__import__("os").environ, "PYTHONPATH": str(SERVICE_DIR)}
    done = subprocess.run([sys.executable, "-c", code], cwd=SERVICE_DIR, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "app ok:" in done.stdout and "/signals" in done.stdout


def test_it_is_a_workspace_member_with_a_lock_entry():
    root = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"ai/signals"' in root
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "signals"' in lock and 'source = { virtual = "ai/signals" }' in lock
    assert 'name = "fasttext-predict"' in lock


def _app_routes():
    from signals.app import app

    out = set()
    for path, methods in app.openapi()["paths"].items():
        if path not in SKIP_ROUTES:
            out.update((m.upper(), path) for m in methods)
    return out


def _items():
    return json.loads(COLLECTION.read_text(encoding="utf-8"))["item"]


def test_the_collection_covers_every_route_from_the_signals_variable():
    doc = json.loads(COLLECTION.read_text(encoding="utf-8"))
    assert doc["auth"] == {"type": "noauth"}
    assert {v["key"] for v in doc["variable"]} >= {"signals", "requestId"}
    covered = set()
    for item in _items():
        req = item["request"]
        assert req["url"]["raw"].startswith("{{signals}}/") and req["url"]["host"] == ["{{signals}}"]
        assert all("{{" not in seg for seg in req["url"]["path"])
        assert req["description"]
        covered.add((req["method"], "/" + "/".join(req["url"]["path"])))
    assert covered == _app_routes()


def test_the_collection_bodies_are_valid_wire_requests():
    posts = [item["request"] for item in _items() if item["request"]["method"] == "POST"]
    assert {t["task"] for r in posts for t in json.loads(r["body"]["raw"])["tasks"]} == \
           {"pii", "nearest", "injection", "language"}
    for req in posts:
        parsed = SignalsRequest.from_json(json.loads(req["body"]["raw"]))
        assert parsed.cap_problem() is None


def test_linux_side_files_are_lf():
    files = [SERVICE_DIR / "Dockerfile", COMPOSE, SERVICE_DIR / "models.yaml", SERVICE_DIR / "pyproject.toml",
             *sorted((SERVICE_DIR / "signals").rglob("*.py"))]
    for path in files:
        assert b"\r\n" not in path.read_bytes(), f"{path.name} must stay LF"
