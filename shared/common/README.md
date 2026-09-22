# common

Shared Python utilities for Zeo services. Consumed via uv workspace by every Python project in this repo.

## Modules

| Module | Purpose |
|---|---|
| `common.cdp_interceptor` | Site-agnostic Chrome DevTools Protocol interceptor. Launches Chrome (Windows) or Playwright chromium (Linux/mac), injects a fetch/XHR interceptor, streams captured JSON response bodies to callbacks. See **[src/common/cdp_interceptor/README.md](src/common/cdp_interceptor/README.md)** for CLI usage, Python API, and profile management. |
| `common.documents` | Load JPEG/PNG/PDF/TXT/DOCX into one page-oriented shape (`Document` → `Page`s carrying an image, a text layer, or both), detect the kind by magic bytes, fill missing text with OCR (`RapidOCREngine`, or any object matching the `OCREngine` protocol), and search it (`match_text`: contains / exact / regex / fuzzy). Needs the `documents` extra; `detect` and `textmatch` are pure stdlib. Used by `classifier`. See **[src/common/documents/\_\_init\_\_.py](src/common/documents/__init__.py)** for the API tour. |
| `common.env` | `load_env()` — walk-up `.env` loader. |
| `common.logging_setup` | `setup_logging(name, log_dir, debug)` — file + console handlers with sensible defaults. |
| `common.net` | `validate_url(url, blocked_networks=None)` — the SSRF guard for a caller-supplied URL. Resolves the hostname and refuses the fetch when **any** resolved address falls in a private, loopback, or link-local range (`DEFAULT_BLOCKED_NETWORKS`), raising `BlockedURLError`. On `ai_shared` a URL input is a request to proxy into the private network, so the rule lives here once rather than drifting between services. Deliberately framework-free — it raises, it does not return an `HTTPException` — so each consumer maps the refusal to its own error shape. Stdlib only. Used by `classifier` and `detector`. |
| `common.vision` | Regions and the layers you draw them on. `Region` / `PageGeometry` fix one coordinate contract (regions are always in ORIGINAL page pixels) and `geometry.*` converts between the ≤1000-px working image, the page, PDF points, and the 0–1000 grid a vision model answers on; `slugify_criterion` turns a criterion name into a stable file/id/query handle; `render_svg` / `render_png_layer` / `render_preview` draw the three layer formats; `ArtifactStore` keeps one directory per job (write/list/open/delete/zip, a manifest, a byte cap with a defined drop order, and a TTL sweep that prunes job rows too). Stdlib only, except the raster renderers, which import Pillow at call time. Used by `classifier`. See **[src/common/vision/\_\_init\_\_.py](src/common/vision/__init__.py)** for the API tour. |

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
