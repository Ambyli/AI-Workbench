"""Central configuration for the open-vocabulary detector service.

Every tuneable is here — the model id, the device, the confidence floor, the
inference resize, and the per-call label cap. The other modules import from
this one and hold no constants of their own.

Everything is environment-driven so the same image can be reconfigured with a
recreate rather than a rebuild. The one exception is the model weights: the
default checkpoint is baked into the image (see ``Dockerfile.detector``), so
pointing ``DETECTOR_MODEL`` at something else means the first request after a
recreate pays a download into the ``detector_data`` volume.

Process flow position: loaded first by every other module at import time.
"""

import os

# ---------------------------------------------------------------------------
# Model and device
# ---------------------------------------------------------------------------
# DETECTOR_MODEL is a HuggingFace repo id. The default is OWLv2 base/ensemble:
# permissive licence (Apache-2.0), text-prompted, ~600 MB in fp32, and decent
# on everyday objects — a house, a tree, a car, a swimming pool. It is loaded
# through `detectors.build_detector`, which picks the implementation FAMILY
# from the id, so swapping to another OWL-family checkpoint
# (`google/owlv2-large-patch14-ensemble`, `google/owlvit-base-patch32`) is a
# `.env` edit. A Grounding DINO id is recognised and refused with a message
# naming the class to write — see ai/detector/detectors/__init__.py.
#
# DETECTOR_DEVICE is `cuda` or `cpu`. On this deployment the container is
# pinned to GPU 2, which it SHARES with qwen3.8-solo; if that pairing runs out
# of memory, `cpu` is the documented fallback and costs a few seconds per
# image rather than a few hundred milliseconds. Any value that is not `cpu`
# is treated as `cuda`, and app.py falls back to `cpu` with a warning when
# torch reports no CUDA device — a detector that answers slowly is better
# than a container that will not start.
DETECTOR_MODEL: str = os.environ.get(
    "DETECTOR_MODEL", "google/owlv2-base-patch16-ensemble"
).strip()
DETECTOR_DEVICE: str = os.environ.get("DETECTOR_DEVICE", "cuda").strip().lower()

# ---------------------------------------------------------------------------
# Detection behaviour
# ---------------------------------------------------------------------------
# DEFAULT_THRESHOLD is the confidence floor applied when a request does not
# send one. OWLv2 scores are not calibrated probabilities — 0.25 keeps the
# obvious findings and drops most of the noise; below ~0.1 every label matches
# something somewhere.
#
# MAX_IMAGE_SIDE is the long side an image is resized to BEFORE inference.
# OWLv2 pads to a square and resizes to its own 960 px grid internally, so
# feeding it a 4000 px photo only costs decode and transfer time. Boxes are
# rescaled back to ORIGINAL pixels before they are returned, so a caller never
# sees this number — it exists to bound work, not to change answers.
#
# MAX_LABELS caps how many labels one call may carry. OWLv2 embeds every label
# as its own text query and the cost is linear in that count, so an unbounded
# list is a denial-of-service on a shared GPU. A caller with more labels
# should send more requests.
DEFAULT_THRESHOLD: float = float(os.environ.get("DETECTOR_DEFAULT_THRESHOLD", "0.25"))
MAX_IMAGE_SIDE: int = max(64, int(os.environ.get("DETECTOR_MAX_IMAGE_SIDE", "1024")))
MAX_LABELS: int = max(1, int(os.environ.get("DETECTOR_MAX_LABELS", "32")))

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
# Timeout for fetching a caller-supplied image URL. Short on purpose: the
# classifier's own job has its own budget and a detector call that hangs for
# two minutes on a slow host is worse than one that fails fast.
FETCH_TIMEOUT: float = float(os.environ.get("DETECTOR_FETCH_TIMEOUT", "20"))
FETCH_CONNECT_TIMEOUT: float = 10.0

# Smallest image worth running. Anything below this is a thumbnail or an icon
# and OWLv2's answers on it are noise.
MIN_IMAGE_SIDE: int = 32

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Set LOG_LEVEL=DEBUG in docker-compose.detector.yml to see per-request timing
# and the raw score distribution before the threshold is applied.
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
