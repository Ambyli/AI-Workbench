"""Detector families, and the factory that picks one from a model id.

``DETECTOR_MODEL`` is a `.env` knob, which is only true if changing it can
reach a *different implementation* and not just different weights. That is
what this module is: one class per model family in its own file, and
:func:`build_detector` mapping an id onto one of them.

Adding a family — Grounding DINO is the one this service is shaped for — is
three steps and no edits anywhere else in the service:

  1. Write ``detectors/<family>.py`` with a subclass of
     ``base.OpenVocabularyDetector`` implementing ``load`` and
     ``_detect_prepared`` (boxes in PREPARED pixels; the base class handles
     the rescale back to original).
  2. Add a ``(pattern, loader)`` row to ``_FAMILIES`` below.
  3. Add the extra dependency to ``ai/detector/pyproject.toml`` if it needs
     one, and mention the id in ``DETECTOR.md § Swapping the model``.

Grounding DINO is deliberately NOT implemented yet — it is recognised and
refused with a message naming this file, so an operator who sets the id gets
an actionable startup error instead of a confusing ``AutoProcessor`` traceback
about an unsupported architecture.

Process flow position: called once from ``app.py``'s lifespan.
"""

from __future__ import annotations

from typing import Callable

from .base import Detection, OpenVocabularyDetector

__all__ = ["Detection", "OpenVocabularyDetector", "build_detector"]


def _owl(model_id: str, device: str, max_image_side: int) -> OpenVocabularyDetector:
    from .owlv2 import Owlv2Detector

    return Owlv2Detector(model_id, device, max_image_side=max_image_side)


def _grounding_dino(
    model_id: str, device: str, max_image_side: int
) -> OpenVocabularyDetector:
    raise NotImplementedError(
        f"DETECTOR_MODEL='{model_id}' names a Grounding DINO checkpoint, which "
        "this build does not implement. Add a GroundingDinoDetector to "
        "ai/detector/detectors/ (subclass OpenVocabularyDetector, implement "
        "load + _detect_prepared) and register it in detectors/__init__.py, "
        "or set DETECTOR_MODEL back to an OWL checkpoint such as "
        "google/owlv2-base-patch16-ensemble."
    )


# Matched in order against the LOWERCASED model id, substring semantics. Order
# matters only where one family's name contains another's; it does not today.
_FAMILIES: tuple[tuple[tuple[str, ...], Callable[..., OpenVocabularyDetector]], ...] = (
    (("owlv2", "owlvit", "owl-vit"), _owl),
    (("grounding-dino", "groundingdino"), _grounding_dino),
)


def build_detector(
    model_id: str, device: str, *, max_image_side: int
) -> OpenVocabularyDetector:
    """Return the detector implementation for ``model_id``.

    Construction is cheap — no weights are touched until ``load()`` — so this
    is safe to call at import or startup time and the slow part can be moved
    into a worker thread by the caller.

    Args:
        model_id:       HuggingFace repo id from ``DETECTOR_MODEL``.
        device:         ``"cuda"`` or ``"cpu"``.
        max_image_side: Long side images are resized to before inference.

    Raises:
        NotImplementedError: A recognised family with no implementation yet.
        ValueError:          An id matching no family at all.
    """
    key = model_id.lower()
    for patterns, loader in _FAMILIES:
        if any(pattern in key for pattern in patterns):
            return loader(model_id, device, max_image_side)

    raise ValueError(
        f"DETECTOR_MODEL='{model_id}' matches no detector family. Known "
        "families: OWL (ids containing 'owlv2' / 'owlvit'), Grounding DINO "
        "(ids containing 'grounding-dino' — recognised but not implemented). "
        "See ai/detector/detectors/__init__.py to add one."
    )
