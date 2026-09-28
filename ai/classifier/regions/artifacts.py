"""Writing a job's artifact directory: geometry, text, base image, manifest.

One directory per job under CLASSIFIER_ARTIFACT_DIR. EVERY job writes it —
regions are no longer opt-in — and it holds only what cannot be re-derived:

    regions.json      the canonical geometry, keyed BY CRITERION rather than
                      as a flat list — which is what makes "give me the
                      artifact for this criterion" a lookup, not a filter.
    text.<key>.json   one per distinct text layer the job produced, written
                      by the OCR memo the moment the layer exists (see
                      ``analysis.context``): the exact string ``match_text``
                      searched, where it came from, and its OCR lines. Shared
                      by every criterion that used the same settings.
    p0.base.jpg       the un-annotated page, for a page with an image: every
                      preview (combined or filtered) is composited from it.
    manifest.json     what is in the directory, and which criterion used
                      which text layer.

The SVG / PNG / preview LAYERS are not written here at all: ``api.artifacts``
renders them from regions.json on first fetch and caches them into the same
directory (``p0.svg``, ``p0.<slug>.layer.png``, …), so a job that nobody
looks at costs a JSON file and a JPEG.

    prepare_job_dir()        — clear a stale directory before a job writes.
    text_layer_payload()     — the ``text.<key>.json`` body.
    write_text_layer()       — write one, cap-safe (JSON is never dropped).
    write_job_artifacts()    — regions.json + base image + manifest, and the
                               ``artifacts`` blocks for the result.
    render_layer()           — one layer's bytes, for the lazy renderer.
    layer_name()             — ``p0[.<slug>].<suffix>`` for a format.

Blocking (file I/O and a JPEG encode), so callers run these in a thread.

Process flow position: ``prepare_job_dir`` / ``write_text_layer`` /
``write_job_artifacts`` are called by ``analysis.pipeline``; ``render_layer``
by ``api.artifacts``. Nothing here imports ``analysis``.
"""

from __future__ import annotations

from typing import Any, Optional

from common.documents import Document, TextLayer
from common.vision import (
    PageGeometry,
    Region,
    build_manifest,
    render_png_layer,
    render_preview,
    render_svg,
    slugify_criterion,
)

from api.schemas import CriterionInput
from config import (
    ARTIFACT_MAX_BYTES,
    JOB_TTL_HOURS,
    LAYER_FILE_SUFFIXES,
    PREVIEW_JPEG_QUALITY,
    REGION_LAYER_FORMATS,
)
from logger import logger
from regions.store import store

BASE_IMAGE_NAME = "p0.base.jpg"
_SUFFIX = dict(LAYER_FILE_SUFFIXES)


def artifact_url(job_id: str, name: str) -> str:
    """The path the artifact file endpoint serves ``name`` at.

    Relative on purpose: the same job is reachable directly on :8005 and
    through LiteLLM's ``/v1/classifier`` pass-through, and a stored absolute
    URL would be wrong for one of them.
    """
    return f"/jobs/{job_id}/artifacts/{name}"


def text_file_name(key: str) -> str:
    """``text.<key>.json`` — the one name a text layer is stored under."""
    return f"text.{key}.json"


def layer_name(fmt: str, slug: Optional[str] = None, page: int = 0) -> str:
    """``p0.svg`` / ``p0.<slug>.layer.png`` / … for one format."""
    middle = f"{slug}." if slug else ""
    return f"p{page}.{middle}{_SUFFIX[fmt]}"


# ---------------------------------------------------------------------------
# Before the job, and during it
# ---------------------------------------------------------------------------


def prepare_job_dir(job_id: str) -> None:
    """A re-run of the same job id must not inherit stale files."""
    store.delete(job_id)


def text_layer_payload(
    key: str, settings: dict, layer: TextLayer, engine: Optional[str]
) -> dict[str, Any]:
    """The ``text.<key>.json`` body.

    ``text`` is byte-for-byte the string ``match_text`` searched (the layer's
    own text, unmodified), so a failed match can be diagnosed from this file
    alone. ``lines`` are the OCR lines with their polygons in ORIGINAL page
    pixels; empty for native text — a .txt / .docx has no geometry, and a
    native PDF's layer is PyMuPDF's reading-order text, whose line geometry is
    not recorded (text hits on it are still located, via
    ``common.documents.pdf_text_regions``).
    """
    return {
        "key": key,
        "settings": dict(settings),
        "source": layer.source,
        "engine": engine,
        "chars": len(layer.text),
        "confidence": layer.confidence,
        "text": layer.text,
        "lines": [
            {
                "text": str(line.get("text", "")),
                "polygon": line.get("box"),
                "confidence": line.get("confidence"),
            }
            for line in layer.lines
        ],
    }


def write_text_layer(job_id: str, key: str, payload: dict[str, Any]) -> None:
    """Write one text layer. JSON is in the store's never-dropped set."""
    store.write_json(job_id, text_file_name(key), payload)
    logger.debug("write_text_layer: job %s wrote %s (%d chars)",
                 job_id, text_file_name(key), payload.get("chars", 0))


# ---------------------------------------------------------------------------
# After the job
# ---------------------------------------------------------------------------


def _regions_json(
    job_id: str,
    geometry: Optional[PageGeometry],
    region_map: dict[str, list[Region]],
    criteria: list[CriterionInput],
    detector: Optional[dict],
    localizations: dict[str, dict],
    text_keys: dict[str, str],
) -> dict:
    """The canonical ``regions.json``: keyed by criterion, not a flat list.

    ``pages`` stays a list (of zero or one) because ``common.vision`` is
    page-aware and the filtered renderer reads the frame from it.
    """
    types = {c.name: c.type for c in criteria}
    entries: dict[str, dict] = {}
    for name, regions in region_map.items():
        sources = sorted({r.source for r in regions})
        entries[name] = {
            "slug": slugify_criterion(name),
            "type": types.get(name, "llm"),
            "sources": sources,
            "count": len(regions),
            "regions": [r.as_dict() for r in regions],
            "localization": localizations.get(name),
            "text_layer": text_keys.get(name),
        }
    return {
        "job_id": job_id,
        "pages": [geometry.as_dict()] if geometry is not None else [],
        # Which detector produced the source="detector" regions below, on
        # what device, and what it cost. Null when it was not used.
        "detector": detector,
        "criteria": entries,
    }


def write_job_artifacts(
    job_id: str,
    doc: Document,
    geometry: Optional[PageGeometry],
    region_map: dict[str, list[Region]],
    criteria: list[CriterionInput],
    *,
    localizations: Optional[dict[str, dict]] = None,
    detector: Optional[dict] = None,
    text_refs: Optional[dict[str, dict]] = None,
    notes: Optional[list[str]] = None,
) -> tuple[dict, dict[str, Optional[dict]]]:
    """Write regions.json, the base image and the manifest; describe them.

    The text layers are already there (written while the criteria ran).
    Order matters: files first, THEN the byte cap, THEN the manifest from
    what survived — so the manifest can never promise a file the cap removed.

    Args:
        job_id:        Directory name under CLASSIFIER_ARTIFACT_DIR.
        doc:           The analysed document (the page image for the base).
        geometry:      The page frame, or None (.txt / .docx).
        region_map:    ``{criterion name: [Region, ...]}`` in original pixels;
                       every criterion has an entry, empty or not.
        criteria:      The request's criteria.
        localizations: ``{name: localization}`` from the LLM loop.
        detector:      What the detector did, or None when unused.
        text_refs:     ``{name: {"key", "source", "chars"}}`` for every
                       criterion that used a text layer.
        notes:         Caller-facing sentences for the manifest.

    Returns:
        ``(artifacts_block, {criterion name: artifacts block or None})``.
    """
    localizations = localizations or {}
    text_refs = text_refs or {}
    text_keys = {name: ref["key"] for name, ref in text_refs.items()}

    store.write_json(
        job_id,
        "regions.json",
        _regions_json(job_id, geometry, region_map, criteria, detector,
                      localizations, text_keys),
    )
    page = doc.pages[0] if doc.pages else None
    if geometry is not None and page is not None and page.image_bgr is not None:
        rgb = page.image_bgr[:, :, ::-1]  # the renderer wants RGB, OpenCV holds BGR
        store.write(
            job_id,
            BASE_IMAGE_NAME,
            render_preview(rgb, geometry, [], quality=PREVIEW_JPEG_QUALITY),
        )

    dropped = store.enforce_cap(job_id)
    if dropped:
        logger.warning(
            "write_job_artifacts: job %s exceeded CLASSIFIER_ARTIFACT_MAX_BYTES (%d) "
            "— dropped %s", job_id, ARTIFACT_MAX_BYTES, dropped,
        )

    manifest = build_manifest(
        job_id,
        files=store.list(job_id),
        page_geometry=[geometry.as_dict()] if geometry is not None else [],
        options={
            "layers": sorted(REGION_LAYER_FORMATS) if geometry is not None else [],
            "layers_rendered": "on first fetch, then cached in this directory",
        },
        criteria={
            name: {
                "slug": slugify_criterion(name),
                "count": len(regions),
                "sources": sorted({r.source for r in regions}),
                "text_layer": text_keys.get(name),
            }
            for name, regions in region_map.items()
        },
        ttl_hours=JOB_TTL_HOURS,
        notes=list(notes or []),
        dropped=dropped,
    )
    store.write_json(job_id, "manifest.json", manifest)

    files = store.list(job_id)
    present = {f["name"] for f in files}
    layers = (
        {fmt: artifact_url(job_id, layer_name(fmt)) for fmt, _ in LAYER_FILE_SUFFIXES
         if fmt != "preview" or BASE_IMAGE_NAME in present}
        if geometry is not None
        else {}
    )
    artifacts = {
        "dir": str(store.dir_for(job_id)),
        "files": [{**f, "url": artifact_url(job_id, f["name"])} for f in files],
        "layers": layers,
        "zip_url": f"/jobs/{job_id}/artifacts.zip",
        "total_bytes": sum(f["bytes"] for f in files),
        "expires_at": manifest["expires_at"],
        "dropped": dropped,
        "notes": list(notes or []),
    }
    per_criterion = {
        name: _criterion_artifacts(
            job_id, name, regions, text_refs.get(name), layers
        )
        for name, regions in region_map.items()
    }
    logger.info(
        "write_job_artifacts: job %s holds %d file(s), %d bytes, %d criteria with regions",
        job_id, len(files), artifacts["total_bytes"],
        sum(1 for r in region_map.values() if r),
    )
    return artifacts, per_criterion


def _criterion_artifacts(
    job_id: str,
    name: str,
    regions: list[Region],
    text_ref: Optional[dict],
    layers: dict[str, str],
) -> Optional[dict]:
    """The per-criterion ``artifacts`` block, or None when there is nothing.

    Two independent halves:

      * geometry, when the criterion has regions: ``regions_url`` and one URL
        per layer format filtered to this criterion (``?criterion=<slug>``,
        rendered on first fetch), plus ``attempts`` — one entry per
        enforcement-loop attempt with a drawable box, each carrying
        ``&attempt=<n>`` so a REJECTED box can be looked at on its own.
      * ``text``, when the criterion used a text layer: the ``text.<key>.json``
        link, its source and length. The text itself is never inlined — it
        can be a whole contract.
    """
    block: dict[str, Any] = {}
    if regions:
        slug = slugify_criterion(name)
        block["slug"] = slug
        block["regions_url"] = f"{artifact_url(job_id, 'regions.json')}?criterion={slug}"
        block["layers"] = {fmt: f"{url}?criterion={slug}" for fmt, url in layers.items()}
        attempts = []
        numbered = [
            r for r in regions if r.source == "llm" and isinstance(r.attrs.get("attempt"), int)
        ]
        for region in sorted(numbered, key=lambda r: r.attrs["attempt"]):
            number = region.attrs["attempt"]
            attempts.append(
                {
                    "attempt": number,
                    "accepted": bool(region.attrs.get("accepted")),
                    **{
                        fmt: f"{url}?criterion={slug}&attempt={number}"
                        for fmt, url in layers.items()
                    },
                }
            )
        block["attempts"] = attempts
    if text_ref is not None:
        block["text"] = {
            "key": text_ref["key"],
            "url": artifact_url(job_id, text_file_name(text_ref["key"])),
            "source": text_ref["source"],
            "chars": text_ref["chars"],
        }
    return block or None


# ---------------------------------------------------------------------------
# The lazy renderer's one entry point
# ---------------------------------------------------------------------------


def render_layer(
    job_id: str, fmt: str, geometry: PageGeometry, regions: list[Region]
) -> Optional[bytes]:
    """One layer's bytes; None for a preview when the base image is gone.

    Every layer — combined, per criterion, per attempt — is produced by this
    one function over some subset of regions.json, so a filtered view and
    the unfiltered file can never disagree.
    """
    if fmt == "svg":
        return render_svg(geometry, regions).encode("utf-8")
    if fmt == "png":
        return render_png_layer(geometry, regions)
    base = store.open(job_id, BASE_IMAGE_NAME)
    if base is None:
        return None
    return render_preview(base, geometry, regions, quality=PREVIEW_JPEG_QUALITY)
