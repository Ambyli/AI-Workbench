"""The one shape every OpenCV detector returns.

A detector's measurement used to arrive as a sentence — "Laplacian variance:
412.3 (threshold: 100.0)" — which a caller could only read, not use. Every
detector now builds its result here, so the numbers are data and the sentence
is the ``reason``:

    {
      "score": 1-10, "verdict": "PASS" | "MARGINAL" | "FAIL", "confidence": 0-100,
      "method": "cv",
      "reason": "the same sentence as before, for a person",
      "detail": {
        "detector":     the detector function's name,
        "metric":       the ONE headline number's name,
        "value":        that number,
        "measurements": every number the detector counted, keyed by name,
        "thresholds":   the lines that decide the score / verdict,
        "parameters":   the config values it measured with,
        "state":        a categorical outcome, when there is one
                        ("underexposed" / "normal" / "overexposed", ...)
      },
      "regions": [...]      # feature detectors only
    }

``metric`` / ``value`` are the same two keys on every detector, so a consumer
comparing pages or jobs never has to know which detector ran. Names carry
their unit: ``*_ratio`` is 0-1, ``*_px`` is pixels, ``*_count`` a count.
Pixel figures are in the WORKING image the detector was handed (≤1000 px on
the long side); ``analysis.cv_eval`` adds that frame as ``detail.image``.

Process flow position: imported by ``cv.quality`` and ``cv.features``.
"""

from __future__ import annotations

from typing import Any, Optional


def _clean(value: Any) -> Any:
    """Plain JSON numbers: numpy scalars to Python, floats rounded to 4 dp."""
    if hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
        value = value.item()
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


def cv_result(
    *,
    detector: str,
    score: int,
    verdict: str,
    confidence: int,
    reason: str,
    metric: str,
    measurements: dict[str, Any],
    thresholds: Optional[dict[str, Any]] = None,
    parameters: Optional[dict[str, Any]] = None,
    state: Optional[str] = None,
    regions: Optional[list[dict]] = None,
) -> dict:
    """Assemble a detector result. ``metric`` must be a key of ``measurements``.

    Raises:
        KeyError: ``metric`` is not one of the measurements — a detector that
            names a headline number it did not measure is a bug, caught the
            first time it runs.
    """
    measured = _clean(dict(measurements))
    detail: dict[str, Any] = {
        "detector": detector,
        "metric": metric,
        "value": measured[metric],
        "measurements": measured,
        "thresholds": _clean(dict(thresholds or {})),
        "parameters": _clean(dict(parameters or {})),
    }
    if state is not None:
        detail["state"] = state
    result: dict[str, Any] = {
        "score": int(score),
        "verdict": verdict,
        "confidence": int(confidence),
        "method": "cv",
        "reason": reason,
        "detail": detail,
    }
    if regions is not None:
        result["regions"] = regions
    return result
