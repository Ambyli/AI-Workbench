"""
common.vision — say *where*, then draw it.

A detector, an OCR line, a PDF text hit, and (later) a vision model all know
where on a page they found something, and all of them used to throw that away.
This package is the shared vocabulary for keeping it: one region type, one
coordinate contract, three render formats, and a per-job file store to put
them in.

    Region / PageGeometry     a finding and the page frame it is expressed in.
                              Regions are ALWAYS in original page pixels; the
                              geometry carries the ≤1000-px working scale (and
                              a PDF page's size in points) needed to get there.

    geometry.*                the transforms between the four spaces: working
                              image, original page, PDF points, and the 0–1000
                              grid a vision model answers on. Plus ``iou`` for
                              cross-checking two producers' boxes.

    slugify_criterion         criterion name → a stable file/id/query handle
                              with a 4-hex hash of the exact name, so
                              ``has faces`` and ``Has Faces!`` cannot collide.
    criterion_color /         one hue per criterion (hashed, stable across
    stroke_for                pages and formats); stroke style per source —
                              solid cv/detector/pdf-text, dashed ocr, dotted
                              llm, double diff.

    render_svg                overlay whose viewBox is the original page, one
                              ``<g id="c-<slug>">`` per criterion so a client
                              can toggle criteria inside the file.
    render_png_layer          transparent RGBA PNG the size of the page — the
                              literal "Photoshop layer".
    render_preview            the page with the layer burned in, JPEG.

    ArtifactStore             one directory per job: write / list / open /
                              delete / zip, a manifest, a byte cap with a
                              defined drop order, and a TTL sweeper that also
                              prunes job rows.

Typical flow::

    geom = PageGeometry(page=0, width=3024, height=4032, working_scale=0.3307)
    regions = [rescale_region(r, geom.working_scale, geom) for r in detector_out]

    store = ArtifactStore("/data/artifacts", max_bytes=50_000_000)
    store.write_json(job_id, "regions.json", {...})
    store.write(job_id, "p0.svg", render_svg(geom, regions))
    store.write(job_id, "p0.layer.png", render_png_layer(geom, regions))
    store.enforce_cap(job_id)

Dependencies: the model, geometry, palette and store modules are pure stdlib.
``render_png_layer`` / ``render_preview`` need Pillow (already in the
``documents`` extra) and import it at call time, so a consumer that only wants
regions and SVG pays nothing for it. Nothing here imports OpenCV or numpy.
"""

from .geometry import (
    DEFAULT_GRID,
    box_region,
    clamp_points,
    grid_to_pixels,
    iou,
    pixels_to_grid,
    pixels_to_points,
    points_to_pixels,
    rescale_region,
    scale_points,
    to_original,
    to_working,
)
from .model import (
    REGION_SOURCES,
    PageGeometry,
    Region,
    RegionKind,
    RegionSource,
)
from .palette import (
    criterion_color,
    dasharray,
    legend_entries,
    slugify_criterion,
    stroke_for,
)
from .render import (
    PREVIEW_QUALITY,
    render_png_layer,
    render_preview,
    render_svg,
    stroke_width,
)
from .store import (
    CONTENT_TYPES,
    MANIFEST_NAME,
    REGIONS_NAME,
    ArtifactStore,
    build_manifest,
    content_type_for,
    expires_at,
)

__all__ = [
    # model
    "PageGeometry",
    "Region",
    "RegionKind",
    "RegionSource",
    "REGION_SOURCES",
    # geometry
    "DEFAULT_GRID",
    "box_region",
    "clamp_points",
    "grid_to_pixels",
    "iou",
    "pixels_to_grid",
    "pixels_to_points",
    "points_to_pixels",
    "rescale_region",
    "scale_points",
    "to_original",
    "to_working",
    # palette
    "criterion_color",
    "dasharray",
    "legend_entries",
    "slugify_criterion",
    "stroke_for",
    # render
    "PREVIEW_QUALITY",
    "render_png_layer",
    "render_preview",
    "render_svg",
    "stroke_width",
    # store
    "ArtifactStore",
    "CONTENT_TYPES",
    "MANIFEST_NAME",
    "REGIONS_NAME",
    "build_manifest",
    "content_type_for",
    "expires_at",
]
