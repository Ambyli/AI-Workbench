"""Make ``signals`` (ai/signals) importable, with no real model anywhere.

``ai/signals`` goes first on sys.path (the container's PYTHONPATH), so
``import signals`` works from the repo root. Every test runs on FAKE models
(sig_helpers.py: controllable steps, gates, call records) through the real
scheduler, cache, API and privacy code; only
test_signals_real_models.py loads the pinned ONNX / fastText files, and it
skips unless they are there.

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/signals -q -p no:cacheprovider

Run it in its own pytest process, like unit-tests/route-policy.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "signals"

for path in (SERVICE_DIR, pathlib.Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

