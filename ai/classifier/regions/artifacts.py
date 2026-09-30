"""Writing a job's artifact directory: geometry, text, base images, manifest.

One directory per job under CLASSIFIER_ARTIFACT_DIR. EVERY job writes it —
regions are no longer opt-in — and it holds only what cannot be re-derived.
A job has ITEMS (every page of every document), and the GLOBAL item index n
is the page index everything here uses, so the shared vision code needs no
notion of documents:

    regions.json          the canonical geometry, keyed BY CRITERION rather
                          than as a flat list — which is what makes "give me
                          the artifact for this criterion" a lookup, not a
                          filter. Every region carries ``page = n``; ``pages``
                          holds one frame per item that has an image.
    text.p{n}.<key>.json  one per item and distinct text layer, written by the
                          item's OCR memo the moment the layer exists (see
                          ``analysis.context``): the exact string
                          ``match_text`` searched on that page, where it came
                          from, and its OCR lines.
    text.d{i}.<key>.json  a document-scope ``text`` search's joined text —
                          document i's pages, in order, as one string — with
                          the segment map back to each page.
    p{n}.base.jpg         the un-annotated page, for an item with an image:
                          every preview of that page (combined or filtered) is
                          composited from it.
    manifest.json         what is in the directory, ``items`` (n → document,
                          page, filename), and which criterion used which
                          text layers.

The SVG / PNG / preview LAYERS are not written here at all: ``api.artifacts``
renders them from regions.json on first fetch and caches them into the same
directory (``p{n}.svg``, ``p{n}.<slug>.layer.png``, …), so a job that nobody
looks at costs a JSON file and a JPEG per page.

**The byte cap is per item.** CLASSIFIER_ARTIFACT_MAX_BYTES is multiplied by
the job's item count (``job_byte_cap``), so a twenty-page PDF gets twenty
times a photo's allowance.

    prepare_job_dir()        — clear a stale directory before a job writes.
    text_layer_payload()     — the ``text.p{n}.<key>.json`` body.
    write_text_layer()       — write one, cap-safe (JSON is never dropped).
    write_job_artifacts()    — regions.json + base images + manifest, and the
                               ``artifacts`` blocks for the result.
    job_byte_cap()           — this job's cap, from its manifest.
    FileLabeler / labeler_for() — every file's kind / format / item /
                               document / criteria, for the result and the
                               manifest endpoint alike.
    render_layer()           — one layer's bytes, for the lazy renderer.
    layer_name()             — ``p{n}[.<slug>].<suffix>`` for a format.
    base_image_name()        — ``p{n}.base.jpg``.

Blocking (file I/O and JPEG encodes), so callers run these in a thread.

Process flow position: ``prepare_job_dir`` / ``write_text_layer`` /
``write_job_artifacts`` are called by ``analysis.pipeline``; ``render_layer``
and ``job_byte_cap`` by ``api.artifacts``. Nothing here imports ``analysis``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from common.documents import TextLayer
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
    JOB_TTL_HOURS,
    LAYER_FILE_SUFFIXES,
    PREVIEW_JPEG_QUALITY,
    REGION_LAYER_FORMATS,
)
from logger import logger
from regions.store import store

_SUFFIX = dict(LAYER_FILE_SUFFIXES)


@dataclass
class ItemInfo:
    """What the writers need to know about one item — plain data, so this
    package never imports ``analysis``."""

    item: int
    document: int
    page: int
    filename: str
    geometry: Optional[PageGeometry]
    image_bgr: Any = None

    def as_map_entry(self) -> dict[str, Any]:
        return {"document": self.document, "page": self.page, "filename": self.filename}


def artifact_url(job_id: str, name: str) -> str:
    """The path the artifact file endpoint serves ``name`` at.

    Relative on purpose: the same job is reachable directly on :8005 and
    through LiteLLM's ``/v1/classifier`` pass-through, and a stored absolute
    URL would be wrong for one of them.
    """
    return f"/jobs/{job_id}/artifacts/{name}"


def base_image_name(page: int) -> str:
    """``p{n}.base.jpg`` — the un-annotated item every preview is built on."""
    return f"p{page}.base.jpg"


def layer_name(fmt: str, slug: Optional[str] = None, page: int = 0) -> str:
    """``p{n}.svg`` / ``p{n}.<slug>.layer.png`` / … for one format."""
    middle = f"{slug}." if slug else ""
    return f"p{page}.{middle}{_SUFFIX[fmt]}"


def job_byte_cap(job_id: str) -> int:
    """CLASSIFIER_ARTIFACT_MAX_BYTES × the job's item count (0 = no cap).

    Read from the manifest, so the lazy renderer caps a directory exactly as
    the job that wrote it did. A directory with no manifest yet counts as one
    item.
    """
    manifest = store.read_json(job_id, "manifest.json") or {}
    return per_item_cap() * max(1, len(manifest.get("items") or {}))


def per_item_cap() -> int:
    """The store's cap (CLASSIFIER_ARTIFACT_MAX_BYTES, set in regions.store) —
    read off the store so the value lives in exactly one place."""
    return int(store.max_bytes or 0)


# ---------------------------------------------------------------------------
# Before the job, and during it
# ---------------------------------------------------------------------------


def prepare_job_dir(job_id: str) -> None:
    """A re-run of the same job id must not inherit stale files."""
    store.delete(job_id)


def text_layer_payload(
    key: str, settings: dict, layer: TextLayer, engine: Optional[str], *, item: int = 0
) -> dict[str, Any]:
    """The ``text.p{n}.<key>.json`` body.

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
        "item": item,
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


def write_text_layer(job_id: str, name: str, payload: dict[str, Any]) -> None:
    """Write one text layer file. JSON is in the store's never-dropped set."""
    store.write_json(job_id, name, payload)
    logger.debug("write_text_layer: job %s wrote %s (%d chars)",
                 job_id, name, payload.get("chars", 0))


# ---------------------------------------------------------------------------
# After the job
# ---------------------------------------------------------------------------


def _regions_json(
    job_id: str,
    items: list[ItemInfo],
    region_map: dict[str, list[Region]],
    criteria: list[CriterionInput],
    detector: Optional[dict],
    localizations: dict[str, dict],
    text_names: dict[str, list[str]],
) -> dict:
    """The canonical ``regions.json``: keyed by criterion, not a flat list.

    ``pages`` holds one frame per item with an image (its ``page`` is the
    item index); the filtered renderer reads the frame from it.
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
            "text_layers": text_names.get(name, []),
        }
    return {
        "job_id": job_id,
        "pages": [i.geometry.as_dict() for i in items if i.geometry is not None],
        "items": {str(i.item): i.as_map_entry() for i in items},
        # Which detector produced the source="detector" regions below, on
        # what device, and what it cost. Null when it was not used.
        "detector": detector,
        "criteria": entries,
    }


def _item_layers(job_id: str, info: ItemInfo, present: set[str]) -> dict[str, str]:
    """The combined layer URLs for one item — {} when it has no image."""
    if info.geometry is None:
        return {}
    return {
        fmt: artifact_url(job_id, layer_name(fmt, page=info.item))
        for fmt, _ in LAYER_FILE_SUFFIXES
        if fmt != "preview" or base_image_name(info.item) in present
    }


def write_job_artifacts(
    job_id: str,
    items: list[ItemInfo],
    region_map: dict[str, list[Region]],
    criteria: list[CriterionInput],
    *,
    localizations: Optional[dict[str, dict]] = None,
    detector: Optional[dict] = None,
    text_refs: Optional[dict[str, list[dict]]] = None,
    notes: Optional[list[str]] = None,
) -> tuple[dict, dict[str, Optional[dict]]]:
    """Write regions.json, the base images and the manifest; describe them.

    The text layers are already there (written while the units ran). Order
    matters: files first, THEN the byte cap, THEN the manifest from what
    survived — so the manifest can never promise a file the cap removed.

    Args:
        job_id:        Directory name under CLASSIFIER_ARTIFACT_DIR.
        items:         Every item, in global order.
        region_map:    ``{criterion name: [Region, ...]}`` in original pixels,
                       each region's ``page`` its item; every criterion has an
                       entry, empty or not.
        criteria:      The request's criteria.
        localizations: ``{name: localization}`` from the LLM loop (merged
                       across items when there are several).
        detector:      What the detector did, or None when unused.
        text_refs:     ``{name: [{"item" | "document", "key", "name",
                       "source", "chars"}, ...]}`` — every text layer each
                       criterion's units used.
        notes:         Caller-facing sentences for the manifest.

    Returns:
        ``(artifacts_block, {criterion name: artifacts block or None})``.
    """
    localizations = localizations or {}
    text_refs = text_refs or {}
    text_names = {name: [r["name"] for r in refs] for name, refs in text_refs.items()}

    regions_doc = _regions_json(job_id, items, region_map, criteria, detector,
                                localizations, text_names)
    store.write_json(job_id, "regions.json", regions_doc)
    for info in items:
        if info.geometry is not None and info.image_bgr is not None:
            rgb = info.image_bgr[:, :, ::-1]  # the renderer wants RGB, OpenCV holds BGR
            store.write(
                job_id,
                base_image_name(info.item),
                render_preview(rgb, info.geometry, [], quality=PREVIEW_JPEG_QUALITY),
            )

    cap = per_item_cap() * max(1, len(items))
    dropped = store.enforce_cap(job_id, max_bytes=cap)
    if dropped:
        logger.warning(
            "write_job_artifacts: job %s exceeded its cap (%d items × "
            "CLASSIFIER_ARTIFACT_MAX_BYTES %d) — dropped %s",
            job_id, len(items), per_item_cap(), dropped,
        )

    manifest = build_manifest(
        job_id,
        files=store.list(job_id),
        page_geometry=[i.geometry.as_dict() for i in items if i.geometry is not None],
        options={
            "layers": (
                sorted(REGION_LAYER_FORMATS) if any(i.geometry for i in items) else []
            ),
            "layers_rendered": "on first fetch, then cached in this directory",
            "byte_cap": {"per_item": per_item_cap(), "items": len(items), "total": cap},
        },
        criteria={
            name: {
                "slug": slugify_criterion(name),
                "count": len(regions),
                "sources": sorted({r.source for r in regions}),
                "text_layers": text_names.get(name, []),
            }
            for name, regions in region_map.items()
        },
        ttl_hours=JOB_TTL_HOURS,
        notes=list(notes or []),
        dropped=dropped,
    )
    # The item map: n → where the page came from. Added here rather than to
    # common.vision.build_manifest, which knows nothing about documents.
    manifest["items"] = {str(i.item): i.as_map_entry() for i in items}
    labeler = FileLabeler(regions_doc, manifest["items"])
    manifest["files"] = labeler.describe(job_id, manifest["files"])
    store.write_json(job_id, "manifest.json", manifest)

    files = store.list(job_id)
    present = {f["name"] for f in files}
    item_layers = {i.item: _item_layers(job_id, i, present) for i in items}
    artifacts = {
        "dir": str(store.dir_for(job_id)),
        # Labelled by the same FileLabeler the manifest endpoint uses.
        "files": labeler.describe(job_id, files),
        "items": [
            {"item": i.item, "layers": item_layers[i.item]}
            for i in items if item_layers[i.item]
        ],
        "zip_url": f"/jobs/{job_id}/artifacts.zip",
        "total_bytes": sum(f["bytes"] for f in files),
        "expires_at": manifest["expires_at"],
        "dropped": dropped,
        "notes": list(notes or []),
    }
    per_criterion = {
        name: _criterion_artifacts(job_id, name, regions, text_refs.get(name, []), item_layers)
        for name, regions in region_map.items()
    }
    logger.info(
        "write_job_artifacts: job %s holds %d file(s), %d bytes, %d item(s), "
        "%d criteria with regions",
        job_id, len(files), artifacts["total_bytes"], len(items),
        sum(1 for r in region_map.values() if r),
    )
    return artifacts, per_criterion


def text_link(job_id: str, ref: dict) -> dict:
    """One text-layer link: where it is, and what it is."""
    link = {k: ref[k] for k in ("item", "document") if k in ref}
    link.update(
        key=ref["key"],
        url=artifact_url(job_id, ref["name"]),
        source=ref["source"],
        chars=ref["chars"],
    )
    return link


def _criterion_artifacts(
    job_id: str,
    name: str,
    regions: list[Region],
    text_refs: list[dict],
    item_layers: dict[int, dict[str, str]],
) -> Optional[dict]:
    """The per-criterion ``artifacts`` block, or None when there is nothing.

    Two independent halves:

      * geometry, when the criterion has regions: ``regions_url``, and one
        ``items`` entry per item the criterion found something on — that
        item's layer URLs filtered to this criterion (``p{n}.svg?criterion=
        <slug>``, rendered on first fetch) plus ``attempts``, one per
        enforcement-loop attempt with a drawable box on that item, each
        carrying ``&attempt=<n>`` so a REJECTED box can be looked at alone.
      * ``text``, when the criterion used text layers: one link per layer
        (per item, or per document for scope "document"). The text itself is
        never inlined — it can be a whole contract.
    """
    block: dict[str, Any] = {}
    if regions:
        slug = slugify_criterion(name)
        block["slug"] = slug
        block["regions_url"] = f"{artifact_url(job_id, 'regions.json')}?criterion={slug}"
        entries = []
        for item in sorted({r.page for r in regions}):
            layers = item_layers.get(item) or {}
            if not layers:
                continue
            on_item = [r for r in regions if r.page == item]
            numbered = [
                r for r in on_item
                if r.source == "llm" and isinstance(r.attrs.get("attempt"), int)
            ]
            attempts = [
                {
                    "attempt": region.attrs["attempt"],
                    "accepted": bool(region.attrs.get("accepted")),
                    **{
                        fmt: f"{url}?criterion={slug}&attempt={region.attrs['attempt']}"
                        for fmt, url in layers.items()
                    },
                }
                for region in sorted(numbered, key=lambda r: r.attrs["attempt"])
            ]
            entries.append({
                "item": item,
                "count": len(on_item),
                "layers": {fmt: f"{url}?criterion={slug}" for fmt, url in layers.items()},
                "attempts": attempts,
            })
        block["items"] = entries
    if text_refs:
        block["text"] = [text_link(job_id, ref) for ref in text_refs]
    return block or None


# ---------------------------------------------------------------------------
# File labels — what each file in the directory is, and whose it is
# ---------------------------------------------------------------------------

# The name grammar of this directory. ``n`` is the global item, ``i`` the
# document; a slug is common.vision's lowercase [a-z0-9-] (always ending in
# a hash, so it can never be "base" or "layer").
_LAYER_FILE_RE = re.compile(
    r"^p(?P<item>\d+)\.(?:(?P<slug>[a-z0-9][a-z0-9-]*)\.)?"
    r"(?P<suffix>svg|layer\.png|preview\.jpg)$"
)
_BASE_FILE_RE = re.compile(r"^p(?P<item>\d+)\.base\.jpg$")
_TEXT_FILE_RE = re.compile(
    r"^text\.(?:p(?P<item>\d+)|d(?P<document>\d+))\.[A-Za-z0-9_-]+\.json$"
)
_FORMAT_FOR_SUFFIX = {suffix: fmt for fmt, suffix in LAYER_FILE_SUFFIXES}


class FileLabeler:
    """Label every file of one job from its ``regions.json`` and item map.

    The ONE place a file name is turned into ``kind`` / ``format`` / ``item``
    / ``document`` / ``criteria``, used by both the job-result writer
    (``write_job_artifacts``) and the manifest endpoint, so the two cannot
    disagree. Everything is derived from the job's own records, never
    guessed:

      * regions per item — each region's ``page`` in regions.json;
      * text layers per criterion — regions.json's ``criteria[*].text_layers``
        (the same names the manifest's criterion map and each criterion's
        ``artifacts.text`` links carry);
      * document of an item — the ``items`` map (manifest, else regions.json).

    Args:
        regions_doc: The parsed regions.json (``criteria``, ``pages``, ``items``).
        items_map:   ``{"n": {"document", "page", "filename"}}``; defaults to
                     regions.json's own ``items``.
    """

    def __init__(self, regions_doc: dict, items_map: Optional[dict] = None) -> None:
        entries: dict[str, dict] = (regions_doc or {}).get("criteria") or {}
        self.items_map: dict[str, dict] = dict(
            items_map if items_map is not None else (regions_doc or {}).get("items") or {}
        )
        self.all_names: list[str] = sorted(entries)
        self.slug_to_name: dict[str, str] = {
            e.get("slug"): name for name, e in entries.items() if e.get("slug")
        }
        self.name_to_slug: dict[str, str] = {v: k for k, v in self.slug_to_name.items()}
        # item → criteria with at least one region there (any region, rejected
        # LLM attempts included: they are drawable with ?attempt=).
        self.region_items: dict[str, set[int]] = {}
        self.by_item: dict[int, set[str]] = {}
        # text layer file → criteria that read it.
        self.text_layers: dict[str, set[str]] = {}
        self.by_text: dict[str, set[str]] = {}
        for name, entry in entries.items():
            items = {int(r.get("page", 0)) for r in entry.get("regions") or []}
            self.region_items[name] = items
            for n in items:
                self.by_item.setdefault(n, set()).add(name)
            layers = set(entry.get("text_layers") or [])
            self.text_layers[name] = layers
            for layer in layers:
                self.by_text.setdefault(layer, set()).add(name)
        self.image_items: set[int] = {
            int(p.get("page", -1)) for p in (regions_doc or {}).get("pages") or []
        }

    # ── Per item / per document ───────────────────────────────────────────
    def document_of(self, item: int) -> Optional[int]:
        entry = self.items_map.get(str(item))
        return None if entry is None else entry.get("document")

    def items_of_document(self, document: int) -> list[int]:
        return sorted(
            int(n) for n, e in self.items_map.items() if e.get("document") == document
        )

    def text_items(self, layer: str) -> set[int]:
        """The items a text layer covers: its own for ``text.p{n}``, every page
        of document i for ``text.d{i}`` (the pages that search joined)."""
        match = _TEXT_FILE_RE.match(layer)
        if match is None:
            return set()
        if match.group("item") is not None:
            return {int(match.group("item"))}
        return set(self.items_of_document(int(match.group("document"))))

    def items_for(self, name: str) -> set[int]:
        """Items this criterion has regions on or read a text layer for."""
        items = set(self.region_items.get(name, set()))
        for layer in self.text_layers.get(name, set()):
            items |= self.text_items(layer)
        return items

    # ── One file ──────────────────────────────────────────────────────────
    def label(self, name: str) -> dict[str, Any]:
        """``{kind, format?, item, document, criteria}`` for one file name."""
        if name == "manifest.json":
            return self._label("manifest", None, None, None, self.all_names)
        if name == "regions.json":
            return self._label("regions", None, None, None, self.all_names)
        match = _TEXT_FILE_RE.match(name)
        if match:
            criteria = self.by_text.get(name, set())
            if match.group("item") is not None:
                item = int(match.group("item"))
                return self._label("text", "json", item, self.document_of(item), criteria)
            return self._label("text", "json", None, int(match.group("document")), criteria)
        match = _BASE_FILE_RE.match(name)
        if match:
            item = int(match.group("item"))
            return self._label("base", None, item, self.document_of(item),
                               self.by_item.get(item, set()))
        match = _LAYER_FILE_RE.match(name)
        if match:
            item = int(match.group("item"))
            fmt = _FORMAT_FOR_SUFFIX[match.group("suffix")]
            slug = match.group("slug")
            if slug:
                owner = self.slug_to_name.get(slug)
                criteria = {owner} if owner else set()
            else:
                criteria = self.by_item.get(item, set())
            return self._label("layer", fmt, item, self.document_of(item), criteria)
        return self._label("other", None, None, None, set())

    @staticmethod
    def _label(kind: str, fmt: Optional[str], item: Optional[int],
               document: Optional[int], criteria) -> dict[str, Any]:
        label: dict[str, Any] = {"kind": kind}
        if fmt:
            label["format"] = fmt
        label.update(item=item, document=document, criteria=sorted(criteria))
        return label

    def describe(self, job_id: str, files: list[dict]) -> list[dict]:
        """Store entries (``name``, ``bytes``, ``content_type``) → labelled
        entries with their ``url`` — the shape of ``artifacts.files`` and the
        manifest endpoint's ``files``."""
        return [
            {**f, "url": artifact_url(job_id, f["name"]), **self.label(f["name"])}
            for f in files
        ]


def labeler_for(job_id: str) -> FileLabeler:
    """The labeler for a job's directory as it is on disk."""
    regions_doc = store.read_json(job_id, "regions.json") or {}
    manifest = store.read_json(job_id, "manifest.json") or {}
    return FileLabeler(regions_doc, manifest.get("items"))


# ---------------------------------------------------------------------------
# The lazy renderer's one entry point
# ---------------------------------------------------------------------------


def render_layer(
    job_id: str, fmt: str, geometry: PageGeometry, regions: list[Region]
) -> Optional[bytes]:
    """One layer's bytes; None for a preview when the base image is gone.

    Every layer — combined, per criterion, per attempt — is produced by this
    one function over some subset of regions.json, so a filtered view and
    the unfiltered file can never disagree. ``geometry.page`` is the item.
    """
    if fmt == "svg":
        return render_svg(geometry, regions).encode("utf-8")
    if fmt == "png":
        return render_png_layer(geometry, regions)
    base = store.open(job_id, base_image_name(geometry.page))
    if base is None:
        return None
    return render_preview(base, geometry, regions, quality=PREVIEW_JPEG_QUALITY)
