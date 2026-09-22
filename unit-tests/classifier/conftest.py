"""Make the classifier's flat modules importable, on a throwaway /data.

`ai/classifier` is a flat package: its modules import each other by bare name
(``from config import ...``), which is what the container's WORKDIR gives
them. A test run from the repo root has neither, so this file supplies both —
and it has to do it at IMPORT time, before any test module runs, because
``config.py`` reads ``DB_PATH`` / ``PAYLOAD_DIR`` / ``CLASSIFIER_ARTIFACT_DIR``
once at import and every other module reads config.

The defaults are absolute container paths (``/data/classifier.db``). Left
alone, a test that touched the store would try to create ``/data`` on the
developer's machine, so all three are pointed at one temp directory for the
whole session. It is deliberately NOT a ``tmp_path`` fixture: the values are
frozen into module constants at import, so a per-test directory would be
ignored by everything that matters.

Run the suite with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_llm_boxes.py -q -p no:cacheprovider
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CLASSIFIER = _ROOT / "ai" / "classifier"

if str(_CLASSIFIER) not in sys.path:
    sys.path.insert(0, str(_CLASSIFIER))

# One directory for the session, cleaned up by the OS rather than by us: a
# test that fails mid-write should leave its artifacts where they can be
# looked at.
_DATA = pathlib.Path(tempfile.mkdtemp(prefix="classifier-tests-"))
os.environ.setdefault("DB_PATH", str(_DATA / "classifier.db"))
os.environ.setdefault("PAYLOAD_DIR", str(_DATA / "payloads"))
os.environ.setdefault("CLASSIFIER_ARTIFACT_DIR", str(_DATA / "artifacts"))
# No OCR models in a unit test: loading three ONNX graphs costs a second and
# nothing here asks a question that needs them.
os.environ.setdefault("CLASSIFIER_OCR_ENGINE", "none")
# The detector is a network dependency. Empty means "off", which is the
# state every test in this directory assumes unless it stubs the client.
os.environ.setdefault("DETECTOR_URL", "")
