"""How LiteLLM gets at the hooks: the overflow.py shim, the `hooks` package,
the bind mounts that put both (and two parts of shared/common) into the
container, and the class-body rule the proxy dispatches by.

* The proxy activates async_post_call_streaming_iterator_hook and
  async_pre_call_hook only when the NAME is in the registered class's own
  __dict__ (proxy/utils.py:1718-1745 at v1.95.0) -- so all five hooks must be
  defined on Handler itself, not inherited (async_post_call_failure_hook is
  dispatched on any CustomLogger, but stays in the class body by the same
  rule, so no hook can drift onto a component).
* overflow.py exports nothing but `handler`, refuses a foreign top-level
  `hooks`, and leaves sys.path as it found it.
* A fresh interpreter laid out like the container's /app -- built from the
  compose file's own bind mounts -- loads the shim the way get_instance_fn
  does and gets a working handler, with `common` limited to the mounted parts.
* Repo invariants: every `common.<x>` the hooks import is mounted; the mounted
  parts of common are stdlib-only; the hooks import nothing the LiteLLM image
  lacks.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from hooks.handler import Handler
from hooks.routing.code_route import DEFAULT_CODE_ROUTE_TOOL_REGEX

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"
HOOKS_DIR = _LITELLM_DIR / "hooks"
COMPOSE = _LITELLM_DIR / "docker-compose.litellm.yml"
COMMON_DIR = _ROOT / "shared" / "common" / "src" / "common"

# Third-party top-level packages the LiteLLM image provides: litellm itself and
# two of its own dependencies (the proxy loads its config with yaml; httpx is
# its HTTP client).
IMAGE_PACKAGES = {"litellm", "yaml", "httpx"}

try:
    import litellm  # noqa: F401
except ImportError:
    litellm = None

try:
    import yaml
except ImportError:
    yaml = None

needs_yaml = pytest.mark.skipif(yaml is None, reason="needs PyYAML")


# ── the leaf-class rule ──────────────────────────────────────────────────────
DISPATCHED = ("async_filter_deployments", "async_pre_call_hook", "async_post_call_success_hook",
              "async_post_call_failure_hook")


def test_dispatched_hooks_are_defined_on_the_leaf_class():
    """Not inherited from a mixin or base: the proxy reads type(cb).__dict__."""
    for name in DISPATCHED:
        assert name in Handler.__dict__, name
        assert inspect.iscoroutinefunction(Handler.__dict__[name]), name
    assert "async_post_call_streaming_iterator_hook" in Handler.__dict__
    assert inspect.isasyncgenfunction(Handler.__dict__["async_post_call_streaming_iterator_hook"])


# ── the shim ─────────────────────────────────────────────────────────────────
def test_shim_exports_only_handler_and_restores_sys_path(load_shim):
    path_before = list(sys.path)
    mod = load_shim()
    assert sorted(k for k in vars(mod) if not k.startswith("__")) == ["handler"]
    assert isinstance(mod.handler, Handler)
    assert sys.path == path_before


def test_shim_refuses_a_foreign_hooks_package(fresh_hooks, load_shim, tmp_path):
    """A `hooks` imported from anywhere else (an installed package, say) would
    otherwise fail later with a confusing 'No module named hooks.handler'."""
    foreign = types.ModuleType("hooks")
    foreign.__file__ = str(tmp_path / "site-packages" / "hooks" / "__init__.py")
    path_before = list(sys.path)
    with fresh_hooks():
        sys.modules["hooks"] = foreign
        with pytest.raises(ImportError, match="different top-level 'hooks' package"):
            load_shim()
    assert sys.path == path_before


# ── the container layout, in a fresh interpreter ─────────────────────────────
CHILD = r'''
import dataclasses, importlib.util, json, os, sys

app = sys.argv[1]
# In the proxy, litellm is fully imported before the callbacks load -- and
# some litellm modules edit sys.path when imported, so import it first here too.
try:
    import litellm.integrations.custom_logger  # noqa: F401
except ImportError:
    pass
path_before = list(sys.path)
# Exactly LiteLLM's get_instance_fn: not registered, no sys.path change.
spec = importlib.util.spec_from_file_location("overflow", os.path.join(app, "overflow.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
h = mod.handler
h.overflow.filter_deployments("local-general", [
    {"model_name": "local-general", "litellm_params": {"model": "openai/qwen3.8-solo", "order": 1,
                                                       "api_base": "http://qwen3.8-solo:8000/v1"}},
    {"model_name": "local-general", "litellm_params": {"model": "anthropic/claude-sonnet-5", "order": 2}},
])
backend = next(iter(h.overflow.poller.backends.values()))
print(json.dumps({
    "public": sorted(k for k in vars(mod) if not k.startswith("__")),
    "mro": [c.__module__ + "." + c.__qualname__ for c in type(h).__mro__],
    "class_hooks": sorted(n for n in vars(type(h)) if n.startswith("async_")),
    "hooks_file": sys.modules["hooks"].__file__,
    "common_path": list(sys.modules["common"].__path__),
    "modules": sorted(k for k in sys.modules if k.split(".")[0] in ("hooks", "common")),
    "documents_visible": importlib.util.find_spec("common.documents") is not None,
    "path_restored": sys.path == path_before and app not in sys.path,
    "shim_registered": "overflow" in sys.modules,
    "backend_is_dataclass": dataclasses.is_dataclass(backend),
    "signal": type(h.code_route.text_signal).__name__,
    "regex": getattr(h.code_route.regex, "pattern", None),
    "footer": h.footer.enabled,
    "policy": [h.policy.enabled, h.policy.base_url, h.policy.timeout],
}))
'''


def _app_mounts() -> list:
    """[(source path, path under /app)] for every bind mount into /app."""
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["litellm"]
    out = []
    for spec in service["volumes"]:
        source, target = spec.split(":")[:2]
        if target.startswith("/app/"):
            out.append(((COMPOSE.parent / source).resolve(), target[len("/app/"):]))
    return out


def _same(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@needs_yaml
def test_container_layout_loads_the_hook_in_a_fresh_interpreter(tmp_path):
    app = tmp_path / "app"
    for source, rel in _app_mounts():
        dest = app / rel
        if source.is_dir():
            shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = dict(os.environ)
    for name in [k for k in env if k.startswith(("OVERFLOW_", "CODE_ROUTE_", "AUTO_FOOTER_", "ROUTE_POLICY_"))]:
        del env[name]
    # As the compose file sets it, relocated under the temporary /app.
    env["CODE_ROUTE_ROUTER_CONFIG"] = str(app / "semantic-router-config.yaml")
    # -I: no PYTHONPATH, no user site, no script dir on sys.path -- only the
    # interpreter's own site-packages, like the image.
    proc = subprocess.run([sys.executable, "-I", str(script), str(app)], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path), timeout=240)
    assert proc.returncode == 0, proc.stderr[-4000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got["public"] == ["handler"]
    assert got["mro"][0] == "hooks.handler.Handler"
    if litellm is not None:
        assert "litellm.integrations.custom_logger.CustomLogger" in got["mro"]
    assert got["class_hooks"] == ["async_filter_deployments", "async_post_call_failure_hook",
                                  "async_post_call_streaming_iterator_hook", "async_post_call_success_hook",
                                  "async_pre_call_hook"]
    assert _same(got["hooks_file"], app / "hooks" / "__init__.py")
    assert len(got["common_path"]) == 1 and _same(got["common_path"][0], app / "common")
    common = {m for m in got["modules"] if m.split(".")[0] == "common"}
    assert common == {"common", "common.conversation", "common.conversation.history",
                      "common.keywordmatch", "common.keywordmatch.rules"}
    assert {"hooks.handler", "hooks.settings", "hooks.overflow.filter", "hooks.footer.stream",
            "hooks.routing.code_route", "hooks.routing.policy_client"} <= set(got["modules"])
    assert got["documents_visible"] is False  # only the mounted parts of common exist
    assert got["path_restored"] is True and got["shim_registered"] is False
    assert got["backend_is_dataclass"] is True
    assert got["signal"] == "KeywordRule"  # the mounted router config was read
    assert got["regex"] == DEFAULT_CODE_ROUTE_TOOL_REGEX and got["footer"] is True
    assert got["policy"] == [True, "http://route-policy:8000", 2.0]  # the defaults: on, by service DNS


# ── repo invariants ──────────────────────────────────────────────────────────
def _imports(path: pathlib.Path):
    """(module, level) for every import statement in a file, at any depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, 0
        elif isinstance(node, ast.ImportFrom):
            yield node.module or "", node.level


def _py_files(directory: pathlib.Path):
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _common_parts_used() -> set:
    used = set()
    for path in _py_files(HOOKS_DIR):
        for module, level in _imports(path):
            if level == 0 and module.startswith("common."):
                used.add(module.split(".")[1])
    return used


def test_hooks_import_only_the_stdlib_the_image_and_common():
    for path in _py_files(HOOKS_DIR):
        for module, level in _imports(path):
            if level:
                continue  # relative: inside the package
            top = module.split(".")[0]
            assert top in sys.stdlib_module_names or top in IMAGE_PACKAGES or top == "common", (
                f"{path.relative_to(_ROOT)} imports {module!r}, which the LiteLLM container does not have")
            if top == "common":
                assert module.count(".") >= 1, f"{path.relative_to(_ROOT)}: import a common subpackage, not common"


@needs_yaml
def test_every_common_part_the_hooks_import_is_mounted():
    used = _common_parts_used()
    assert used == {"conversation", "keywordmatch"}  # update the compose file when this grows
    volumes = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["litellm"]["volumes"]
    assert "../../shared/common/src/common/__init__.py:/app/common/__init__.py:ro" in volumes
    for part in used:
        assert f"../../shared/common/src/common/{part}:/app/common/{part}:ro" in volumes, part
    assert "./hooks:/app/hooks:ro" in volumes and "./overflow.py:/app/overflow.py:ro" in volumes


def test_the_mounted_common_parts_are_stdlib_only():
    """common/__init__.py and each mounted part import nothing but the stdlib
    and their own modules -- the container has no other part of common."""
    assert list(_imports(COMMON_DIR / "__init__.py")) == []
    for part in _common_parts_used():
        for path in _py_files(COMMON_DIR / part):
            for module, level in _imports(path):
                if level:
                    assert level == 1, f"{path.relative_to(_ROOT)} reaches outside common.{part}"
                    continue
                assert module.split(".")[0] in sys.stdlib_module_names, (
                    f"{path.relative_to(_ROOT)} imports {module!r}: common.{part} must stay stdlib-only")
