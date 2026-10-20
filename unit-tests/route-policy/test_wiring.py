"""The route-policy subsystem is wired into the repo the way CLAUDE.md asks.

* the Postman collection covers every HTTP route the app serves (method and
  path, path parameters as ``:name`` with a ``variable`` entry), and its
  /decide, /observe and /evaluate bodies validate against the real models;
* compose: the service swap-class with a healthcheck and no
  container_name / ports, the database recreate-class on its registered port
  with the trino-reader init script, every ``${VAR}`` documented in
  .env.example;
* Trino: the catalog's ``${ENV:...}`` variables reach the coordinator, each
  with a ``:-`` default (an unset one would stop the whole coordinator);
* LiteLLM's ``/v1/route-policy`` pass-through (auth on), the Prometheus
  scrape job, the Makefile stack (before `edge`), the workspace member, the
  Dockerfile copying what runs, LF line endings on what runs in Linux, and
  the docs tables that list subsystems.

The edge invariants shared by every fronted service (allow-list, 503,
status page, published port) are unit-tests/deploy/test_deploy.py's.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from rp_helpers import ROOT, SERVICE_DIR

from route_policy.api.schemas import DecideRequest, EvaluateRequest, ObserveRequest

COLLECTION = SERVICE_DIR / "route-policy.postman_collection.json"
COMPOSE = SERVICE_DIR / "docker-compose.route-policy.yml"
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
SKIP_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _collection():
    return json.loads(COLLECTION.read_text(encoding="utf-8"))


def _app_routes():
    """(METHOD, path with {} for each parameter) of every route the real app
    serves -- the module-level app, instrumented, so /metrics is included."""
    from route_policy.app import app

    out = set()
    for path, methods in app.openapi()["paths"].items():
        if path in SKIP_ROUTES:
            continue
        for method in methods:
            out.add((method.upper(), re.sub(r"\{[^}]+\}", "{}", path)))
    return out


def _collection_routes():
    out = set()
    for item in _collection()["item"]:
        req = item["request"]
        segments = req["url"]["path"]
        assert segments[:2] == ["v1", "route-policy"], item["name"]
        path = "/" + "/".join("{}" if s.startswith(":") else s for s in segments[2:])
        out.add((req["method"], path))
    return out


def test_collection_covers_every_route():
    assert _collection_routes() == _app_routes()


def test_collection_path_parameters_use_colon_syntax():
    names = {v["key"] for v in _collection()["variable"]}
    assert {"litellm", "virtual master key"} <= names
    for item in _collection()["item"]:
        url = item["request"]["url"]
        raw_path = url["raw"].split("?", 1)[0].replace("{{litellm}}", "")
        assert "{{" not in raw_path, f"{item['name']}: a path segment uses {{...}}; use :name"
        params = [s[1:] for s in url["path"] if s.startswith(":")]
        assert [v["key"] for v in url.get("variable", [])] == params, item["name"]
        for p in params:
            assert f":{p}" in raw_path and p in names, item["name"]
        assert item["request"].get("description"), item["name"]


@pytest.mark.parametrize("path, model", [("decide", DecideRequest), ("observe", ObserveRequest),
                                         ("evaluate", EvaluateRequest)])
def test_collection_bodies_match_the_request_models(path, model):
    variables = {v["key"]: v["value"] for v in _collection()["variable"]}
    bodies = [i["request"]["body"]["raw"] for i in _collection()["item"]
              if i["request"]["url"]["path"][-1] == path]
    assert bodies
    for raw in bodies:
        text = re.sub(r"\{\{([^}]+)\}\}", lambda m: variables[m.group(1)], raw)
        model.model_validate(json.loads(text))


def test_compose_classes_ports_and_env():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    svc = compose["services"]["route-policy"]
    assert svc["labels"]["com.zeo.deploy"] == "swap"
    assert "container_name" not in svc and "ports" not in svc
    assert svc["healthcheck"] and svc["stop_grace_period"]
    assert svc["networks"] == ["shared"] and compose["networks"]["shared"]["name"] == "ai_shared"
    assert svc["depends_on"] == {"route-policy-db": {"condition": "service_healthy"}}
    # The analysis queue's infrastructure settings, each with the default config.py uses.
    env = dict(e.split("=", 1) for e in svc["environment"])
    assert env["ROUTE_POLICY_CLASSIFIER_URL"] == "${ROUTE_POLICY_CLASSIFIER_URL:-http://classifier:8000}"
    for var, default in (("ROUTE_POLICY_CLASSIFY_TIMEOUT_S", "180"), ("ROUTE_POLICY_CLASSIFY_POLL_S", "1"),
                         ("ROUTE_POLICY_ANALYSIS_QUEUE_MAX", "64"), ("ROUTE_POLICY_ANALYSIS_CONCURRENCY", "2")):
        assert env[var] == f"${{{var}:-{default}}}", var
    db = compose["services"]["route-policy-db"]
    assert db["container_name"] == "route-policy-db" and db["image"] == "postgres:17-alpine"
    assert "com.zeo.deploy" not in (db.get("labels") or {})
    assert db["ports"] == ["${PORT_ROUTE_POLICY_DB:-5441}:5432"]
    assert "./db-init/10-trino-reader.sh:/docker-entrypoint-initdb.d/10-trino-reader.sh:ro" in db["volumes"]
    assert db["networks"] == ["shared"]
    text = COMPOSE.read_text(encoding="utf-8")
    for var in sorted(set(re.findall(r"\$\{([A-Z0-9_]+)", text))):
        assert f"\n{var}=" in ENV_EXAMPLE, f"{var} is not in .env.example"
    assert re.search(r"^PORT_ROUTE_POLICY=8027\s.*edge\.yml", ENV_EXAMPLE, re.M)
    assert re.search(r"^PORT_ROUTE_POLICY_DB=5441\s.*route-policy\.yml", ENV_EXAMPLE, re.M)
    registry = re.findall(r"^PORT_[A-Z0-9_]+=(\d+)", ENV_EXAMPLE, re.M)
    assert registry.count("8027") == 1 and registry.count("5441") == 1 and "5434" not in registry


def test_trino_catalog_env_reaches_the_coordinator():
    catalog = (ROOT / "ai/trino/catalogs/postgres_route_policy.properties").read_text(encoding="utf-8")
    assert "jdbc:postgresql://route-policy-db:5432/" in catalog and "connection-user=trino_reader" in catalog
    trino = yaml.safe_load((ROOT / "ai/trino/docker-compose.trino.yml").read_text(encoding="utf-8"))
    env = trino["services"]["trino-coordinator"]["environment"]
    for var in re.findall(r"\$\{ENV:([A-Z0-9_]+)\}", catalog):
        assert var in env, f"{var} is not in trino-coordinator's environment"
        assert ":-" in str(env[var]), f"{var} needs a :- default (unset would stop the coordinator)"
        assert f"\n{var}=" in ENV_EXAMPLE


def test_litellm_pass_through_and_prometheus():
    config = yaml.safe_load((ROOT / "ai/litellm/litellm_config.yaml").read_text(encoding="utf-8"))
    (entry,) = [e for e in config["general_settings"]["pass_through_endpoints"] if e["path"] == "/v1/route-policy"]
    assert entry == {"path": "/v1/route-policy", "target": "http://route-policy:8000", "include_subpath": True,
                     "methods": ["GET", "POST"], "forward_headers": True, "auth": True}
    prom = yaml.safe_load((ROOT / "ai/litellm/prometheus.yml").read_text(encoding="utf-8"))
    targets = [t for job in prom["scrape_configs"] for sc in job["static_configs"] for t in sc["targets"]]
    assert "route-policy:8000" in targets


def test_makefile_workspace_and_dockerfile():
    calls = [line for line in (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
             if line.startswith("$(eval $(call service,")]
    assert "$(eval $(call service,route-policy,route-policy-db route-policy))" in calls
    assert calls[-1].startswith("$(eval $(call service,edge,")
    root = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"ai/route-policy"' in root
    dockerfile = (SERVICE_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ai/route-policy/route_policy ./ai/route-policy/route_policy" in dockerfile
    assert "COPY ai/route-policy/rulesets ./ai/route-policy/rulesets" in dockerfile
    assert "COPY ai/route-policy/bench ./ai/route-policy/bench" in dockerfile   # the benchmark runs from the image
    assert "import bench.run, bench.points, bench.report, bench.convert, bench.metrics" in dockerfile
    assert "PYTHONPATH=/app/ai/route-policy" in dockerfile
    assert '"route_policy.app:app"' in dockerfile


def test_linux_side_files_are_lf():
    files = [SERVICE_DIR / "db-init" / "10-trino-reader.sh", SERVICE_DIR / "Dockerfile",
             *sorted((SERVICE_DIR / "route_policy" / "db" / "migrations").glob("*.sql"))]
    for path in files:
        assert b"\r\n" not in path.read_bytes(), f"{path.name} must stay LF"


def test_docs_list_the_subsystem():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "ai/route-policy/route-policy.postman_collection.json" in claude
    for var in ("PORT_ROUTE_POLICY", "PORT_ROUTE_POLICY_DB", "ROUTE_POLICY_TRINO_READER_PASSWORD"):
        assert f"`{var}`" in claude, var
    infra = (ROOT / "ai/AI_INFRA.md").read_text(encoding="utf-8")
    assert "route-policy/docker-compose.route-policy.yml" in infra
    assert "route-policy/ROUTE_POLICY.md" in infra
    assert "| route-policy | `8027`" in infra and "| route-policy-db (postgres) | `5441` |" in infra
