# common

Shared Python utilities for Zeo services. Consumed via uv workspace by every Python project in this repo.

## Modules

| Module | Purpose |
|---|---|
| `common.cdp_interceptor` | Site-agnostic Chrome DevTools Protocol interceptor. Launches Chrome (Windows) or Playwright chromium (Linux/mac), injects a fetch/XHR interceptor, streams captured JSON response bodies to callbacks. See [cdp_interceptor/README.md](src/common/cdp_interceptor/) or the widget's original `USAGE_INTERCEPTOR.md` for the public API. |
| `common.env` | `load_env()` — walk-up `.env` loader. |
| `common.logging_setup` | `setup_logging(name, log_dir, debug)` — file + console handlers with sensible defaults. |

## Adding a capability

Any package or module that could plausibly be reused across projects belongs here — not in the project directory that first needs it. See `CLAUDE.md § Shared Python code` for the policy.

1. Create the subpackage under `src/common/<name>/`.
2. Expose the public API from its `__init__.py`.
3. Add tests under `tests/`.
4. If a new external dep is needed, add it to `pyproject.toml`. Consuming projects don't need pyproject changes.

## Local install

Handled by the workspace — from the repo root:

```bash
uv sync
```

Every workspace member gets `common` installed editable.
