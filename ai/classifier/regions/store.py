"""The process-wide artifact store.

``common.vision.ArtifactStore`` holds no state — every call reads the
directory — so one instance serves the writers (``regions.artifacts``), the
TTL sweeper (``regions.sweeper``), the delete hook, and the four artifact
routes (``api.artifacts``). Sharing it is free and keeps
CLASSIFIER_ARTIFACT_MAX_BYTES in exactly one place.

Process flow position: the bottom of the regions package; imported by
everything else in it and by ``api.artifacts``.
"""

from common.vision import ArtifactStore

from config import ARTIFACT_DIR, ARTIFACT_MAX_BYTES

store = ArtifactStore(ARTIFACT_DIR, max_bytes=ARTIFACT_MAX_BYTES)
