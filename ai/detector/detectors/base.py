"""What every detector family has to provide, and the coordinate contract.

One class per model family lives beside this file; the factory in
``__init__.py`` picks one from the model id. This module holds the two things
they share: the :class:`Detection` record they all emit, and the
:class:`OpenVocabularyDetector` base class that fixes the lifecycle (build
cheap, load lazily, detect many times) and — more importantly — the
**coordinate contract**.

**The coordinate contract.** ``detect()`` is handed the ORIGINAL image and
returns boxes in ORIGINAL image pixels, whatever the model did in between.
Every family resizes, pads, or normalises differently (OWLv2 pads to a square
and works on a 960-px grid; Grounding DINO normalises to 0-1 on the unpadded
image), and a caller must not have to know which. :meth:`prepare` does the
one resize this service controls, and :meth:`to_original` undoes it — so a
family implementation only has to get its own model's output into the
*prepared* frame and call that.

Process flow position: imported by ``detectors/__init__.py`` and by each
family module; nothing outside the package imports it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image


@dataclass(frozen=True)
class Detection:
    """One box, in ORIGINAL image pixels.

    Attributes:
        label: The caller's label string, echoed exactly — the caller matches
            results back to its own criteria by this, so it is never
            normalised, lower-cased, or de-duplicated on the way out.
        score: Model confidence. NOT a calibrated probability; comparable
            between boxes of the same model, not between models.
        box:   ``(x1, y1, x2, y2)``, top-left origin, x1 < x2 and y1 < y2,
               clipped to the image.
    """

    label: str
    score: float
    box: tuple[float, float, float, float]

    def as_dict(self) -> dict:
        """JSON-ready form. Coordinates are rounded to a pixel tenth — more
        precision than that is noise from the model's own grid, and it keeps
        the response small when a page has hundreds of boxes."""
        return {
            "label": self.label,
            "score": round(float(self.score), 4),
            "box": [round(float(v), 1) for v in self.box],
        }


class OpenVocabularyDetector:
    """Base class for a text-prompted object detector.

    Subclasses implement :meth:`load` (build the model — slow, once) and
    :meth:`_detect_prepared` (run it on the already-resized image and return
    boxes in PREPARED pixels). Everything else is here so the families cannot
    disagree about it.
    """

    #: Short name for the family, reported in ``GET /health`` so an operator
    #: can see which implementation a model id actually resolved to.
    family: str = "base"

    def __init__(self, model_id: str, device: str, *, max_image_side: int) -> None:
        self.model_id = model_id
        self.device = device
        self.max_image_side = max_image_side
        self._loaded = False

    # ── Lifecycle ─────────────────────────────────────────────────────────
    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        """Build the model and processor. Blocking and slow (seconds); call
        it from a worker thread, and exactly once — it is idempotent."""
        raise NotImplementedError

    # ── Inference ─────────────────────────────────────────────────────────
    def detect(
        self, image: "Image", labels: Sequence[str], threshold: float
    ) -> list[Detection]:
        """Boxes for ``labels`` in ``image``, in ORIGINAL image pixels.

        Args:
            image:     The original RGB image (already EXIF-transposed).
            labels:    Text prompts, in the caller's own spelling.
            threshold: Confidence floor. Applied by the family implementation
                       so the model's own post-processing can use it too —
                       filtering after the fact costs the same but throws away
                       the model's chance to short-circuit.

        Returns:
            Detections sorted by descending score, ORIGINAL pixel coordinates.
        """
        if not self._loaded:
            self.load()

        prepared, scale = self.prepare(image)
        raw = self._detect_prepared(prepared, list(labels), threshold)
        width, height = image.size
        out = [
            Detection(
                label=d.label,
                score=d.score,
                box=self.to_original(d.box, scale, width, height),
            )
            for d in raw
        ]
        out.sort(key=lambda d: d.score, reverse=True)
        return out

    def _detect_prepared(
        self, image: "Image", labels: list[str], threshold: float
    ) -> list[Detection]:
        """Family-specific inference; boxes in PREPARED pixels."""
        raise NotImplementedError

    # ── Coordinates ───────────────────────────────────────────────────────
    def prepare(self, image: "Image") -> tuple["Image", float]:
        """Resize the long side down to ``max_image_side``.

        Returns ``(prepared_image, scale)`` where ``scale`` is
        prepared ÷ original. An image already small enough is returned
        untouched with ``scale = 1.0`` — never upscaled, because inventing
        pixels does not invent detail and only makes the model slower.
        """
        from PIL import Image as PILImage

        width, height = image.size
        longest = max(width, height)
        if longest <= self.max_image_side:
            return image, 1.0
        scale = self.max_image_side / longest
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        # The true scale is the ROUNDED size over the original, not the ratio
        # we asked for — a 1-pixel rounding error compounds into a visibly
        # offset box on a 4000-px photo.
        prepared = image.resize(size, PILImage.BILINEAR)
        return prepared, size[0] / width

    @staticmethod
    def to_original(
        box: tuple[float, float, float, float],
        scale: float,
        width: int,
        height: int,
    ) -> tuple[float, float, float, float]:
        """Prepared-pixel box → original-pixel box, clipped to the image.

        Clipping is not cosmetic: OWLv2's square padding lets a box run past
        the right or bottom edge of a non-square image, and a consumer that
        crops by these numbers would get an IndexError rather than a picture.
        """
        x1, y1, x2, y2 = (v / scale for v in box)
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        return (
            max(0.0, min(x1, float(width))),
            max(0.0, min(y1, float(height))),
            max(0.0, min(x2, float(width))),
            max(0.0, min(y2, float(height))),
        )
