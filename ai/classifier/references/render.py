"""A reference's image files — and the text catalogue ``auto`` selects from.

    page.jpg       the ORIGINAL page pixels (the coordinate frame every stored
                   region is in) — what a reviewer opens.
    working.jpg    the ≤1000-px working copy, the same pixels the pipeline
                   (and, as a candidate, the vision model) sees.
    c.<slug>.jpg   one per criterion that guides the vision model: the WORKING
                   image with that criterion's regions drawn on it
                   (``common.vision.draw_regions``, no captions). This is the
                   exact image an /assess shows the model as the example, so
                   it is rendered ONCE, here, and never re-derived — the
                   example a job was guided by cannot change under it. A
                   ``whole_page`` criterion's composite is the bare working
                   image (the prompt then says the whole image is the
                   example).

Regions are stored in original page pixels and moved into the working frame
for drawing (``common.vision.to_working``), so a composite of a 4000-px photo
costs a 1000-px encode. Colours and strokes are ``common.vision``'s — one hue
per criterion, the heavy ``manual`` stroke for a caller's region — so a
composite and the job's own layers read the same way.

The CATALOGUE is the other rendering of a reference: one text entry per
reference (id, title, description, tags, and its usable criteria with their
expected verdict and score) — what the per-item selection call of
``references: "auto"`` reads beside the candidate page.

    catalogue_entry()  one reference → its entry (plain data; the submit puts
                       the pool's entries in the job's plan).
    catalogue_text()   entries → the text block the selection prompt carries.

Blocking (JPEG encodes), so the runner calls ``render_files`` in a thread.

Process flow position: called by ``jobs.runners.run_reference`` after
``references.finalize``; the bytes go to ``references.store.reference_files``.
"""

from __future__ import annotations

from typing import Any

from common.vision import PageGeometry, Region, annotate_to_jpeg, to_working

from config import REFERENCE_DESCRIPTION_MAX_CHARS, REFERENCE_JPEG_QUALITY
from references.model import PAGE_FILE, WORKING_FILE, Reference


def encode_jpeg(image_bgr: Any, quality: int = REFERENCE_JPEG_QUALITY) -> bytes:
    """A BGR array as JPEG bytes."""
    import cv2

    ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:  # pragma: no cover - cv2 only fails on an empty array
        raise ValueError("could not encode the reference image as JPEG")
    return buf.tobytes()


def composite(working_bgr: Any, geometry: PageGeometry, regions: list[Region]) -> bytes:
    """The working image with ``regions`` (original page pixels) drawn on it."""
    height, width = working_bgr.shape[:2]
    frame = PageGeometry(page=0, width=int(width), height=int(height))
    moved = [
        Region(
            page=0, kind=r.kind, points=to_working(r.points, geometry.working_scale),
            label=r.label, score=r.score, source=r.source, attrs=dict(r.attrs),
        )
        for r in regions
    ]
    rgb = working_bgr[:, :, ::-1]
    return annotate_to_jpeg(
        rgb, moved, geometry=frame, label_regions=False, quality=REFERENCE_JPEG_QUALITY,
    )


def render_files(
    page_bgr: Any,
    working_bgr: Any,
    geometry: PageGeometry,
    criteria_record: dict[str, dict[str, Any]],
) -> dict[str, bytes]:
    """Every image file of one reference, by name.

    Args:
        page_bgr:        The original page image.
        working_bgr:     Its ≤1000-px working copy.
        geometry:        The page's frame (original size, working scale).
        criteria_record: ``references.finalize.merge``'s output — each entry's
                         ``composite`` name (None for criteria that do not
                         guide the llm) and ``regions``.
    """
    files = {PAGE_FILE: encode_jpeg(page_bgr), WORKING_FILE: encode_jpeg(working_bgr)}
    for entry in criteria_record.values():
        name = entry.get("composite")
        if not name:
            continue
        regions = [Region.from_dict(r) for r in entry.get("regions") or []]
        files[name] = composite(working_bgr, geometry, regions)
    return files


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def catalogue_entry(ref: Reference) -> dict[str, Any]:
    """One reference's catalogue entry: who it is and what it can show."""
    criteria = []
    for name, entry in ((ref.record or {}).get("criteria") or {}).items():
        if not entry.get("usable"):
            continue
        expected = entry.get("expected") or {}
        criteria.append({
            "name": name,
            "verdict": expected.get("verdict"),
            "score": expected.get("score"),
        })
    return {
        "reference_id": ref.id,
        "title": ref.title,
        "description": (ref.description or "")[:REFERENCE_DESCRIPTION_MAX_CHARS] or None,
        "tags": list(ref.tags),
        "criteria": criteria,
    }


def catalogue_text(entries: list[dict[str, Any]]) -> str:
    """The catalogue as the selection prompt shows it — one block per
    reference, the id first so the answer can quote it exactly."""
    blocks = []
    for e in entries:
        lines = [f"- id: {e['reference_id']}"]
        if e.get("title"):
            lines.append(f"  title: {e['title']}")
        if e.get("description"):
            lines.append(f"  description: {e['description']}")
        if e.get("tags"):
            lines.append(f"  tags: {', '.join(e['tags'])}")
        shown = "; ".join(
            f"'{c['name']}' was {c['verdict']} ({c['score']})" for c in e.get("criteria") or []
        )
        lines.append(f"  criteria: {shown or '(none)'}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
