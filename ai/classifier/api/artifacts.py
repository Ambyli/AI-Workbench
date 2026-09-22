"""The four artifact routes: manifest, one file, the zip, and an early delete.

All under `/jobs/{job_id}`, all served from the per-job directory
``regions.artifacts`` wrote during the job:

    GET    /jobs/{job_id}/artifacts             the manifest
    GET    /jobs/{job_id}/artifacts/{name}      one file, optionally filtered
    GET    /jobs/{job_id}/artifacts.zip         all of them, streamed
    DELETE /jobs/{job_id}/artifacts             free the disk, keep the job

They live here rather than in ``common.jobs.router`` because that router is
about job ROWS; artifacts are the classifier's own idea. Auth posture is the
same as every other route: the LiteLLM pass-through does the bearer check, and
direct `:8005` access is unauthenticated, as today.

Two things worth knowing before editing:

  * **Filtered renders are re-rendered, never sliced.** ``?criterion=`` and
    friends re-run the same ``common.vision.render_*`` functions over the
    subset of ``regions.json``, so a filtered layer is always consistent with
    the combined one. SVGs are cheap enough to serve uncached; PNG and
    preview renders are cached into the directory as ``p0.<slug>.layer.png``
    — the same names ``regions.layers_per_criterion`` pre-renders, so the two
    paths cannot produce different files.
  * **404 and 410 mean different things.** 404 is "there never was one, or
    the job is unknown"; 410 is "this job DID have artifacts and the sweeper
    (or a DELETE) has taken them". A caller that stored a URL can tell
    "expired" from "wrong URL" without guessing.

Process flow position: the router is mounted by ``main``. The writers, the TTL
sweeper and the delete hook are ``regions.artifacts`` / ``regions.sweeper``;
this module only reads what they wrote.
"""

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse

from common.vision import (
    PageGeometry,
    Region,
    content_type_for,
    render_png_layer,
    render_preview,
    render_svg,
)

from config import JOB_TTL_HOURS, LAYER_STEM_PREFIX_RE, PREVIEW_JPEG_QUALITY
from logger import logger
from regions.store import store
from regions.sweeper import _refresh_gauges

# LAYER_STEM_PREFIX_RE (config.py § Region layers) matches what can sit in
# FRONT of the page number in a layer file name — a compare job's diff layers
# and an example's own layers — so all three families answer the same filters.


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _result_of(job: Any) -> dict:
    """The job's result dict, or {} — compare and assess results differ."""
    return job.result if isinstance(getattr(job, "result", None), dict) else {}


def _had_artifacts(job: Any) -> bool:
    """True when this job's stored result says it once had an artifact directory.

    That is what separates 410 (gone) from 404 (never existed): a result
    carrying an ``artifacts`` block is a promise the directory was written,
    so its absence now means something removed it.
    """
    result = _result_of(job)
    if result.get("artifacts"):
        return True
    subject = result.get("input_analysis")
    return isinstance(subject, dict) and bool(subject.get("artifacts"))


async def _resolve(registry: Any, job_id: str) -> None:
    """Raise the right error when this job has no readable artifact directory.

    Returns quietly when the directory is there and can be served.
    """
    job = await registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    if store.exists(job_id):
        return
    if _had_artifacts(job):
        raise HTTPException(
            status_code=410,
            detail=(
                f"job {job_id!r} had artifacts but its directory is gone — the TTL "
                f"sweeper (JOB_TTL_HOURS={JOB_TTL_HOURS}) or an explicit "
                "DELETE .../artifacts removed it. Re-submit the job to rebuild them."
            ),
        )
    raise HTTPException(
        status_code=404,
        detail=(
            f"job {job_id!r} has no artifacts — submit it with the `regions` option "
            "to have regions collected and layers rendered (see API.md § Regions "
            "and layers)."
        ),
    )


def _regions_name(stem_prefix: str) -> str:
    """Which regions file a layer's filters are resolved against.

    An EXAMPLE's pages are a different coordinate frame from the subject's, so
    ``regions.examples`` writes it its own ``e{i}.regions.json`` and a filter
    on ``e0.p0.svg`` has to read that one — reading the subject's would
    re-render the example's layer at the subject's page size. A DIFF is in the
    subject's frame and is filed in the subject's ``regions.json``, so it
    resolves there.
    """
    if stem_prefix and not stem_prefix.startswith("diff-"):
        return f"{stem_prefix}regions.json"
    return "regions.json"


def _load_regions(job_id: str, name: str = "regions.json") -> dict:
    """Read a job's regions file, or 404 when it never wrote one."""
    data = store.read_json(job_id, name)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} has no {name} in its artifact directory",
        )
    return data


def _filtered(
    data: dict,
    criteria: list[str],
    sources: Optional[set[str]],
    attempt: Optional[int],
    accepted: Optional[bool],
) -> tuple[dict, list[Region]]:
    """Apply the query filters to ``regions.json``'s criterion map.

    Args:
        data:     The parsed regions.json.
        criteria: Slugs to keep; empty keeps all.
        sources:  Region sources to keep; None keeps all.
        attempt:  LLM attempt number to keep — the enforcement loop stores
                  every attempt, so this is how a REJECTED box is viewed on
                  its own. Non-LLM regions have no attempt and are dropped by
                  this filter, which is what "show me attempt 2" means.
        accepted: Tri-state. ``True`` (or omitted, when some other filter is
                  in play) keeps only the accepted LLM attempt — the default,
                  and the same subset the stored combined layer shows.
                  ``False`` keeps only the REJECTED ones, for looking at what
                  the loop threw away. Never filters non-LLM regions.

    Returns:
        ``(entries, regions)`` — the surviving criterion entries and their
        flattened Region objects.

    Raises:
        HTTPException(400): A criterion slug that is not in this job.
    """
    entries: dict[str, dict] = data.get("criteria", {}) or {}
    known = {entry.get("slug"): name for name, entry in entries.items()}
    unknown = [slug for slug in criteria if slug not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown criterion slug(s) {unknown} for this job. Known slugs: "
                f"{sorted(s for s in known if s)}"
            ),
        )

    kept: dict[str, dict] = {}
    regions: list[Region] = []
    for name, entry in entries.items():
        if criteria and entry.get("slug") not in criteria:
            continue
        selected = [Region.from_dict(r) for r in entry.get("regions", [])]
        if sources:
            selected = [r for r in selected if r.source in sources]
        if attempt is not None:
            selected = [r for r in selected if r.attrs.get("attempt") == attempt]
        elif accepted is False:
            selected = [
                r for r in selected
                if r.source == "llm" and r.attrs.get("accepted") is False
            ]
        else:
            # accepted=true, or no LLM filter at all: the accepted attempt
            # only. This is the DEFAULT because it matches what the stored
            # combined layer shows (regions.collect.visible_regions) — a filtered
            # view and the unfiltered file must never disagree about a
            # criterion they both contain.
            selected = [
                r for r in selected
                if r.source != "llm" or r.attrs.get("accepted") is not False
            ]
        kept[name] = {**entry, "regions": [r.as_dict() for r in selected],
                      "count": len(selected)}
        regions.extend(selected)
    return kept, regions


def _geometry_for(data: dict, page: int) -> PageGeometry:
    """The PageGeometry for one page out of regions.json's ``pages`` list."""
    for entry in data.get("pages", []):
        if int(entry.get("page", -1)) == page:
            return PageGeometry.from_dict(entry)
    raise HTTPException(
        status_code=404, detail=f"no page geometry recorded for page {page}"
    )


def _page_and_format(name: str) -> tuple[int, str]:
    """Split ``p3.layer.png`` into ``(3, "png")``.

    Three families of layer live in one directory and all three are
    filterable, so the page-number prefix is stripped first (see
    ``regions.artifacts._write_page_layers``):

        p3.layer.png          the subject's own layer
        diff-e0-p3.svg        change detection against example 0
        e0.p3.preview.jpg     example 0's own layer

    Raises HTTPException(400) for a name the renderers cannot reproduce —
    which is what stops a filter being applied to, say, ``manifest.json``.
    """
    stem = LAYER_STEM_PREFIX_RE.sub("", name, count=1)
    if not stem.startswith("p"):
        raise HTTPException(
            status_code=400,
            detail=f"{name!r} is not a per-page layer, so it cannot be filtered",
        )
    head, _, rest = stem.partition(".")
    try:
        page = int(head[1:])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"cannot read a page number from {name!r}")
    if rest.endswith("svg"):
        return page, "svg"
    if rest.endswith("layer.png"):
        return page, "png"
    if rest.endswith("preview.jpg"):
        return page, "preview"
    raise HTTPException(
        status_code=400,
        detail=f"{name!r} is not a renderable layer (expected .svg, .layer.png, .preview.jpg)",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def build_artifacts_router(registry: Any) -> APIRouter:
    """Build the four artifact routes against ``registry``.

    A factory rather than a module-level router because every route has to
    look the job up first — 404 for an unknown job id is the difference
    between "your URL is wrong" and "your file is gone".
    """
    api = APIRouter(tags=["artifacts"])

    @api.get("/jobs/{job_id}/artifacts")
    async def get_manifest(job_id: str):
        """Return the manifest: what is in this job's artifact directory.

        The same object as the ``artifacts`` block in the job result, plus the
        criterion → slug map and the page geometry — fetchable without pulling
        the (possibly large) result.
        """
        await _resolve(registry, job_id)
        manifest = store.manifest(job_id)
        if manifest is None:
            raise HTTPException(
                status_code=404, detail=f"job {job_id!r} has no manifest.json"
            )
        manifest["files"] = [
            {**f, "url": f"/jobs/{job_id}/artifacts/{f['name']}"}
            for f in manifest.get("files", [])
        ]
        manifest["zip_url"] = f"/jobs/{job_id}/artifacts.zip"
        return JSONResponse(content=manifest)

    @api.get("/jobs/{job_id}/artifacts.zip")
    async def get_zip(job_id: str):
        """Stream every file in the directory as one zip.

        Built on the fly from the files that are actually there, so a job the
        byte cap trimmed produces a zip of what survived rather than failing.
        """
        await _resolve(registry, job_id)
        logger.info("get_zip: streaming artifacts for job %s", job_id)
        return StreamingResponse(
            store.zip_chunks(job_id),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{job_id}-artifacts.zip"'
            },
        )

    @api.get("/jobs/{job_id}/artifacts/{name}")
    async def get_artifact(
        job_id: str,
        name: str,
        criterion: list[str] = Query(
            default=[],
            description="Criterion slug to keep (repeatable). Omit for every criterion.",
        ),
        source: Optional[str] = Query(
            default=None,
            description="Comma list of region sources to keep: cv, ocr, pdf-text, "
                        "llm, detector, diff.",
        ),
        attempt: Optional[int] = Query(
            default=None,
            description="LLM criteria only: the box from enforcement-loop attempt "
                        "n, accepted or not. This is how a REJECTED box is viewed "
                        "on its own.",
        ),
        accepted: Optional[bool] = Query(
            default=None,
            description="LLM criteria only. true (the default when no `attempt` is "
                        "given) keeps the accepted attempt; false keeps the "
                        "rejected ones.",
        ),
    ):
        """Stream one artifact file, re-rendering it when filters are given.

        With no query parameters the stored file is streamed as-is. With any
        of them the layer is re-rendered from ``regions.json`` by the same
        functions that wrote the stored one, so the filtered and combined
        views can never disagree.

        Errors: **400** for an unknown criterion slug or a name that is not a
        renderable layer, **404** for an unknown job or a file that is not in
        the directory, **410** when the job exists but its directory has been
        swept or deleted.
        """
        await _resolve(registry, job_id)
        try:
            store.path_of(job_id, name)  # name validation, before anything else
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        sources = (
            {s.strip() for s in source.split(",") if s.strip()} if source else None
        )
        filtering = bool(
            criterion or sources or attempt is not None or accepted is not None
        )

        if not filtering:
            raw = store.open(job_id, name)
            if raw is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"{name!r} is not in job {job_id!r}'s artifact directory",
                )
            return Response(content=raw, media_type=content_type_for(name))

        match = LAYER_STEM_PREFIX_RE.match(name)
        stem_prefix = match.group(0) if match else ""
        data = _load_regions(job_id, _regions_name(stem_prefix))
        kept, regions = _filtered(data, criterion, sources, attempt, accepted)

        if name.endswith("regions.json"):
            return JSONResponse(
                content={"job_id": data.get("job_id", job_id),
                         "pages": data.get("pages", []), "criteria": kept}
            )

        page, fmt = _page_and_format(name)
        geometry = _geometry_for(data, page)
        page_regions = [r for r in regions if r.page == page]
        logger.debug(
            "get_artifact: re-rendering %s for job %s (%d region(s) after filters)",
            name, job_id, len(page_regions),
        )
        return _render_filtered(
            job_id, fmt, geometry, page_regions, criterion,
            stem_prefix=stem_prefix,
            # Only the plain "this criterion, default subset" render is
            # cached under the criterion's name. A source or attempt filter
            # produces a DIFFERENT subset, and caching it under that name
            # would serve it back to the next caller who asked for the plain
            # one.
            cacheable=(attempt is None and accepted is not False and not sources),
        )

    @api.delete("/jobs/{job_id}/artifacts", status_code=204)
    async def delete_artifacts(job_id: str):
        """Remove the artifact directory, keeping the job row and its result.

        For a caller that copied the layers somewhere else and wants the disk
        back before the TTL. The inline regions in the result (up to
        CLASSIFIER_INLINE_REGIONS_MAX) survive; the files do not.
        """
        job = await registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        if not store.delete(job_id):
            raise HTTPException(
                status_code=404, detail=f"job {job_id!r} has no artifact directory"
            )
        logger.info("delete_artifacts: removed the artifact directory for job %s", job_id)
        _refresh_gauges()
        return Response(status_code=204)

    return api


def _render_filtered(
    job_id: str,
    fmt: str,
    geometry: PageGeometry,
    regions: list[Region],
    criterion: list[str],
    *,
    stem_prefix: str = "",
    cacheable: bool = True,
) -> Response:
    """Re-render one page's layer for a filtered subset of its regions.

    SVG is rendered per request — it is a few kilobytes of text and caching
    every filter combination would fill the directory. PNG and preview are
    expensive enough to cache, but only for the single-criterion case with no
    attempt filter: that is the filter a per-criterion ``artifacts`` URL uses,
    so it is the one that gets requested repeatedly, and its file name
    (``p0.<slug>.layer.png``) is the same one ``layers_per_criterion``
    pre-renders. A ``?attempt=n`` render is deliberately uncached — it is an
    inspection, not a hot path, and caching it would need the attempt in the
    file name too.
    """
    cache_name = (
        f"{stem_prefix}p{geometry.page}.{criterion[0]}."
        f"{'layer.png' if fmt == 'png' else 'preview.jpg'}"
        if cacheable and len(criterion) == 1 and fmt in ("png", "preview")
        else None
    )
    if cache_name:
        cached = store.open(job_id, cache_name)
        if cached is not None:
            return Response(content=cached, media_type=content_type_for(cache_name))

    if fmt == "svg":
        return Response(
            content=render_svg(geometry, regions), media_type="image/svg+xml"
        )

    if fmt == "png":
        payload = render_png_layer(geometry, regions)
        media = "image/png"
    else:
        # A diff layer's pixels ARE the subject's page, so its base is the
        # subject's; an example's layers have their own.
        base_prefix = "" if stem_prefix.startswith("diff-") else stem_prefix
        base = store.open(job_id, f"{base_prefix}p{geometry.page}.base.jpg")
        if base is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no un-annotated base image for page {geometry.page}, so a "
                    "filtered preview cannot be composited. Base images are written "
                    "only when the job asked for the `preview` layer; request the "
                    "SVG or PNG layer instead."
                ),
            )
        payload = render_preview(base, geometry, regions, quality=PREVIEW_JPEG_QUALITY)
        media = "image/jpeg"

    if cache_name:
        store.write(job_id, cache_name, payload)
        store.enforce_cap(job_id)
        _refresh_gauges()
    return Response(content=payload, media_type=media)
