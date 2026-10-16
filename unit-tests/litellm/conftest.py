"""Make ai/litellm's hooks importable the way the container does, and load the
overflow.py shim the way LiteLLM does.

In the container the `hooks` package sits at /app/hooks and the two
stdlib-only parts of shared/common it imports at /app/common (bind mounts,
ai/litellm/docker-compose.litellm.yml). Here, `ai/litellm` goes first on
sys.path for `hooks`, and `shared/common/src` last for `common` -- a no-op in
the workspace venv, where `common` is installed, and what lets these tests
run in a bare `litellm[proxy]` venv too.

`hooks.settings` reads the environment ONCE, at import. A test of that read
loads the shim against a FRESH import of the package with :func:`fresh_hooks`,
which puts the original modules back afterwards, so the classes every other
test imported at module level stay the ones in sys.modules.
"""

from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import sys
from types import ModuleType
from typing import Iterator

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
LITELLM_DIR = ROOT / "ai" / "litellm"
COMMON_SRC = ROOT / "shared" / "common" / "src"
SHIM = LITELLM_DIR / "overflow.py"

if str(LITELLM_DIR) not in sys.path:
    sys.path.insert(0, str(LITELLM_DIR))
if str(COMMON_SRC) not in sys.path:
    sys.path.append(str(COMMON_SRC))


def _is_hooks_module(name: str) -> bool:
    return name == "hooks" or name.startswith("hooks.")


@contextlib.contextmanager
def fresh_hooks_modules() -> Iterator[None]:
    """Inside the block, `hooks` and its submodules import afresh (so
    hooks.settings re-reads the environment); on exit the original modules
    are back in sys.modules."""
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if _is_hooks_module(k)}
    try:
        yield
    finally:
        for k in [k for k in sys.modules if _is_hooks_module(k)]:
            del sys.modules[k]
        sys.modules.update(saved)


def _load_shim(path: pathlib.Path = SHIM) -> ModuleType:
    """Load overflow.py exactly as LiteLLM's get_instance_fn does
    (litellm/proxy/types_utils/utils.py): spec_from_file_location under the
    module name "overflow", exec_module, NOT registered in sys.modules."""
    spec = importlib.util.spec_from_file_location("overflow", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fresh_hooks():
    """The fresh_hooks_modules context manager: `with fresh_hooks(): ...`."""
    return fresh_hooks_modules


@pytest.fixture
def load_shim():
    """Load overflow.py the way LiteLLM does: `module = load_shim()`."""
    return _load_shim
