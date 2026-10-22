"""Entry point LiteLLM loads for this deployment's hooks -- a shim. The code
is the `hooks` package beside this file (ai/litellm/hooks/).

Registered in ai/litellm/litellm_config.yaml as::

    litellm_settings:
      callbacks: ["overflow.handler"]

and mounted at /app/overflow.py, next to /app/config.yaml, with the package
at /app/hooks and the stdlib-only parts of shared/common it uses at
/app/common (ai/litellm/docker-compose.litellm.yml). LiteLLM's get_instance_fn
resolves "overflow.handler" to <dirname(config)>/overflow.py :: handler
(litellm/proxy/types_utils/utils.py:8-61, identical in 1.93 and 1.95).

WHY A SHIM. get_instance_fn loads this file with
importlib.util.spec_from_file_location + exec_module and never registers it
in sys.modules, never adds its directory to sys.path, and cannot load a
package. Code loaded that way cannot use relative imports, and @dataclass
crashes on it ("'NoneType' object has no attribute '__dict__'": dataclasses
looks sys.modules[cls.__module__] up). So this file does one thing: import
`hooks` the NORMAL way -- registered in sys.modules like any package -- and
build the instance.

sys.path. This directory goes to the FRONT of sys.path for that one import,
so /app/hooks and /app/common win over anything installed under the same
names, and is removed again right after. The proxy already APPENDS its cwd
(/app) to sys.path (proxy_cli.py), and that low-precedence entry is what
resolves the image's own /app/enterprise; leaving /app first would let
everything under /app shadow installed packages for the rest of the
process. Every module in the package is imported eagerly below, so nothing
needs the entry afterwards.

Keep this file free of logic -- and of @dataclass.
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))

# A `hooks` already imported from anywhere else would make the import below
# fail with a confusing "No module named 'hooks.handler'" -- say what it is.
_loaded = getattr(_sys.modules.get("hooks"), "__file__", None)
if "hooks" in _sys.modules and (
    _os.path.normcase(_os.path.realpath(_os.path.dirname(_loaded or "")))
    != _os.path.normcase(_os.path.realpath(_os.path.join(_HERE, "hooks")))
):
    raise ImportError(
        f"a different top-level 'hooks' package is already imported ({_loaded!r}); "
        f"this proxy's hooks live in {_os.path.join(_HERE, 'hooks')}"
    )

_sys.path.insert(0, _HERE)
try:
    from hooks.handler import Handler as _Handler
finally:
    try:
        _sys.path.remove(_HERE)  # the first occurrence: the one inserted above
    except ValueError:
        pass

# The instance LiteLLM imports: litellm_settings.callbacks: ["overflow.handler"]
handler = _Handler()

del _os, _sys, _HERE, _loaded, _Handler
