"""Request/response shapes for the detector API, and the parsing that is
shared between the multipart and JSON spellings of ``POST /detect``.

``/detect`` accepts the same request two ways — a multipart upload (what a
human with curl or Postman has) and a JSON body carrying base64 or a URL
(what another service has). Rather than two endpoints with two validators
that drift, both spellings go through the same ``parse_*`` functions here and
the endpoint sees one shape. That is why :class:`DetectRequest` types
``labels`` loosely: pydantic must not reject in JSON what multipart accepts.

Everything in this module is pure: no I/O, no model, no network. That is
deliberate — it is the part worth unit-testing without a GPU.

Process flow position: imported by app.py; ``parse_labels`` and
``build_response`` are also the seam the tests use.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, Field

from config import DEFAULT_THRESHOLD, MAX_LABELS


class ImageInput(BaseModel):
    """An image supplied as base64 bytes or a remote URL.

    Same shape as the classifier's ``DocumentInput``, on purpose: a caller
    that already builds one of those can send it here unchanged. URLs are
    SSRF-checked (``common.net.validate_url``) before any fetch — this
    container sits on ``ai_shared``, so an unchecked URL input is a request
    to proxy into the private network.
    """

    data: str = Field(description="Base64-encoded image bytes, or an http(s) URL.")
    type: Literal["base64", "url"] = Field(
        description="Whether `data` is 'base64' or 'url'."
    )


class DetectRequest(BaseModel):
    """JSON body for ``POST /detect``.

    The multipart spelling carries the same four fields as form values, with
    `file` in place of `image`.
    """

    image: ImageInput
    # Deliberately NOT `list[str]`: a JSON caller may send the same comma
    # string a multipart caller does, and pydantic would reject it here with
    # a validation error about list types before :func:`parse_labels` ever
    # saw it — two spellings accepted on one endpoint and refused on the
    # other. The real normalisation and the real error messages live in
    # parse_labels; this field only has to let both shapes through.
    labels: str | list[str] = Field(
        description=(
            "Free-text things to look for, e.g. [\"tree\", \"swimming pool\"] "
            "or the comma string \"tree, swimming pool\". Each is embedded as "
            "its own query, so cost is linear in the count and the list is "
            "capped at DETECTOR_MAX_LABELS."
        ),
    )
    threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence floor. Omit for DETECTOR_DEFAULT_THRESHOLD (0.25). "
            "Scores are not calibrated probabilities — treat this as a knob "
            "to turn, not a percentage."
        ),
    )
    max_per_label: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Keep at most this many boxes per label, highest score first. "
            "Omit to keep every box above the threshold."
        ),
    )


def parse_labels(raw: Any) -> list[str]:
    """Normalise the ``labels`` field from any spelling a caller may send.

    Accepted, because a multipart form carries strings and an n8n node or a
    curl user will produce all four:

        ["tree", "house"]        a real JSON array (JSON body)
        '["tree", "house"]'      the same array as a string (form field)
        'tree, house'            a comma list (form field)
        'tree'                   one label

    Blank entries are dropped and duplicates are collapsed **preserving the
    caller's order and spelling** — the label is how a caller matches results
    back to its own criteria, so it is never lower-cased or re-sorted.

    Raises:
        ValueError: Nothing usable, or past DETECTOR_MAX_LABELS.
    """
    if raw is None:
        raise ValueError("labels is required")

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError("labels is required")
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"labels is not valid JSON: {exc}")
        else:
            raw = text.split(",")

    if isinstance(raw, (str, bytes)):
        raw = [raw]
    if not isinstance(raw, Sequence):
        raise ValueError("labels must be an array of strings or a comma list")

    seen: dict[str, None] = {}
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(
                f"labels must be strings, got {type(item).__name__}"
            )
        label = item.strip()
        if label:
            seen.setdefault(label, None)

    labels = list(seen)
    if not labels:
        raise ValueError("labels is required and must contain at least one name")
    if len(labels) > MAX_LABELS:
        raise ValueError(
            f"{len(labels)} labels exceeds the DETECTOR_MAX_LABELS cap of "
            f"{MAX_LABELS}. Split the list across several requests — OWLv2 "
            "embeds every label as its own query, so the cost is linear."
        )
    return labels


def parse_threshold(raw: Any) -> float:
    """``threshold`` from any spelling; DEFAULT_THRESHOLD when absent.

    Raises:
        ValueError: Not a number, or outside 0-1.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_THRESHOLD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"threshold must be a number between 0 and 1, got {raw!r}")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"threshold must be between 0 and 1, got {value}")
    return value


def parse_max_per_label(raw: Any) -> Optional[int]:
    """``max_per_label`` from any spelling; None (unlimited) when absent.

    Raises:
        ValueError: Not an integer, or below 1.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"max_per_label must be a positive integer, got {raw!r}")
    if value < 1:
        raise ValueError(f"max_per_label must be at least 1, got {value}")
    return value


def apply_max_per_label(
    detections: Sequence, labels: Sequence[str], max_per_label: Optional[int]
) -> dict[str, list]:
    """Group detections by label, newest-best first, honouring the cap.

    Every requested label gets a key even when it found nothing — "we looked
    and there was none" and "we did not look" are different answers, and a
    missing key makes them indistinguishable to a caller.

    Args:
        detections:    Detections sorted by descending score.
        labels:        The requested labels, in the caller's order.
        max_per_label: Keep at most this many per label; None for all.

    Returns:
        ``{label: [Detection, ...]}`` in the caller's label order.
    """
    grouped: dict[str, list] = {label: [] for label in labels}
    for det in detections:
        bucket = grouped.get(det.label)
        if bucket is None:
            # A label the caller did not ask for cannot happen today (the
            # model answers with an index into our own query list), but
            # dropping it silently is better than a KeyError if a future
            # family invents one.
            continue
        if max_per_label is not None and len(bucket) >= max_per_label:
            continue
        bucket.append(det)
    return grouped


def build_response(
    *,
    model: str,
    device: str,
    family: str,
    elapsed_ms: float,
    threshold: float,
    labels: Sequence[str],
    width: int,
    height: int,
    grouped: dict[str, list],
) -> dict:
    """The ``POST /detect`` response body.

    Both views of the same boxes are returned on purpose: ``by_label`` is what
    a caller with one criterion per label wants (the classifier), and
    ``detections`` is what a caller drawing an overlay wants (one pass, score
    order). They are the same objects, so the duplication costs bytes, not
    consistency.

    ``image.width`` / ``height`` are the ORIGINAL size every box is expressed
    in — a caller that resized before sending has everything it needs to map
    back.
    """
    flat = [det for bucket in grouped.values() for det in bucket]
    flat.sort(key=lambda d: d.score, reverse=True)
    return {
        "model": model,
        "family": family,
        "device": device,
        "elapsed_ms": round(elapsed_ms, 1),
        "threshold": round(threshold, 4),
        "image": {"width": width, "height": height},
        "labels": list(labels),
        "counts": {label: len(bucket) for label, bucket in grouped.items()},
        "by_label": {
            label: [det.as_dict() for det in bucket]
            for label, bucket in grouped.items()
        },
        "detections": [det.as_dict() for det in flat],
    }
