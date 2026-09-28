"""The four artifact routes: manifest, one file, the zip, and an early delete.

All under `/jobs/{job_id}`, all served from the per-job directory that
``regions.artifacts`` wrote during the job:

    GET    /jobs/{job_id}/artifacts             the manifest
    GET    /jobs/{job_id}/artifacts/{name}      one file, optionally filtered
    GET    /jobs/{job_id}/artifacts.zip         all of them, streamed
    DELETE /jobs/{job_id}/artifacts             free the disk, keep the job

They live here rather than in ``common.jobs.router`` because that router is
about job ROWS; artifacts are the classifier's own idea. Auth posture is the
same as every other route: the LiteLLM pass-through does the bearer check, and
direct `:8005` access is unauthenticated, as today.

Things worth knowing before editing:

  * **One page per ITEM.** ``n`` in every ``p{n}.*`` name is the job's
    global item index (every page of every document, in order); the
    manifest's ``items`` map says which document and page each n is.
  * **Layers are rendered on first fetch.** A job writes only regions.json,
    its text layers, one base image per item and the manifest.
    ``p{n}.svg``, ``p{n}.layer.png``, ``p{n}.preview.jpg`` — and the
    per-criterion ``p{n}.<slug>.<suffix>`` — are rendered from regions.json
    by ``regions.artifacts.render_layer`` the first time they are asked for
    and cached into the directory (so the cache counts against the byte cap
    — per item, ``regions.artifacts.job_byte_cap`` — and goes with the job).
    A filtered request (``?criterion=`` / ``?source=`` / ``?attempt=`` /
    ``?accepted=``) is rendered by the same function over a subset; only the
    plain single-criterion view is cached, under the same
    ``p{n}.<slug>.<suffix>`` name — the two paths cannot produce different
    files.
  * **``text.<name>.txt`` is rendered from ``text.<name>.json``** — e.g.
    ``text.p0.auto.txt`` or ``text.d1.always.txt`` — on every request
    (plain text, never cached — the JSON is the evidence, this is a
    convenience view of its ``text`` field).
  * **404 and 410 mean different things.** 404 is "there never was one, or
    the job is unknown / not finished"; 410 is "this job DID have artifacts
    and the sweeper (or a DELETE) has taken them".

Process flow position: the router is mounted by ``main``. The writers, the TTL
sweeper and the delete hook are ``regions.artifacts`` / ``regions.sweeper``;
this module reads what they wrote and renders layers lazily.
"""

import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse

from common.vision import PageGeometry, Region, content_type_for

from config import JOB_TTL_HOURS, LAYER_FILE_SUFFIXES
from logger import logger
from regions.artifacts import base_image_name, job_byte_cap, layer_name, render_layer
from regions.collect import visible_regions
from regions.store import store
from regions.sweeper import _refresh_gauges

# p{n}.svg | p{n}.layer.png | p{n}.preview.jpg, optionally with a criterion
# slug in the middle: p{n}.<slug>.svg. Slugs are lowercase [a-z0-9-]
# (common.vision); n is the item.
_SUFFIX_TO_FORMAT = {suffix: fmt for fmt, suffix in LAYER_FILE_SUFFIXES}
_LAYER_RE = re.compile(
    r"^p(?P<page>\d+)\.(?:(?P<slug>[a-z0-9][a-z0-9-]*)\.)?"
    r"(?P<suffix>svg|layer\.png|preview\.jpg)$"
)
# text.p{n}.<key>.txt / text.d{i}.<key>.txt → the matching .json's text.
_TEXT_TXT_RE = re.compile(r"^text\.(?P<key>[A-Za-z0-9_.-]+)\.txt$")


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _had_artifacts(job: Any) -> bool:
    """True when this job's stored result says it once had a directory.

    That is what separates 410 (gone) from 404 (never existed): a result
    carrying an ``artifacts`` block is a promise the directory was written,
    so its absence now means something removed it.
    """
    result = job.result if isinstance(getattr(job, "result", None), dict) else {}
    return bool(result.get("artifacts"))


async def _resolve(registry: Any, job_id: str) -> None:
    """Raise the right error when this job has no readable artifact directory."""
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
            f"job {job_id!r} has no artifacts yet — every job writes them when it "
            "runs, so this one has not started, is still running, or failed "
            "before it got that far. Poll GET /jobs/{job_id}."
        ),
    )


def _load_regions(job_id: str) -> dict:
    """Read a job's regions.json, or 404 when it never wrote one."""
    data = store.read_json(job_id, "regions.json")
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"job {job_id!r} has no regions.json in its artifact directory",
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
        accepted: ``False`` keeps only the REJECTED LLM attempts; otherwise
                  the accepted one (the default, and what the combined layer
                  shows). Never filters non-LLM regions.

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
            selected = visible_regions(selected)
        kept[name] = {**entry, "regions": [r.as_dict() for r in selected],
                      "count": len(selected)}
        regions.extend(selected)
    return kept, regions


def _geometry_for(data: dict, page: int) -> PageGeometry:
    """The PageGeometry for the page out of regions.json's ``pages`` list."""
    for entry in data.get("pages", []):
        if int(entry.get("page", -1)) == page:
            return PageGeometry.from_dict(entry)
    raise HTTPException(
        status_code=404,
        detail=(
            f"no page geometry recorded for page {page} — that item has no page "
            "image (a .txt / .docx) or does not exist in this job, so there is "
            "nothing to draw on"
        ),
    )


def _render(job_id: str, fmt: str, geometry: PageGeometry, regions: list[Region],
            cache_name: Optional[str]) -> Response:
    """Render one layer, cache it under ``cache_name`` when given, return it."""
    payload = render_layer(job_id, fmt, geometry, regions)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"the un-annotated base image ({base_image_name(geometry.page)}) is "
                "not in this job's directory — the byte cap dropped it — so a preview "
                "cannot be composited. Request the SVG or PNG layer instead."
            ),
        )
    if cache_name:
        store.write(job_id, cache_name, payload)
        store.enforce_cap(job_id, max_bytes=job_byte_cap(job_id))
        _refresh_gauges()
    return Response(content=payload, media_type=content_type_for(cache_name or layer_name(fmt)))


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

        ``files`` is what is on disk NOW — lazily rendered layers appear once
        they have been fetched. ``layers`` lists the combined layer URLs that
        can be fetched (and will be rendered) whether or not they exist yet.
        """
        await _resolve(registry, job_id)
        manifest = store.manifest(job_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"job {job_id!r} has no manifest.json")
        manifest["files"] = [
            {**f, "url": f"/jobs/{job_id}/artifacts/{f['name']}"}
            for f in store.list(job_id)
        ]
        manifest["zip_url"] = f"/jobs/{job_id}/artifacts.zip"
        return JSONResponse(content=manifest)

    @api.get("/jobs/{job_id}/artifacts.zip")
    async def get_zip(job_id: str):
        """Stream every file in the directory as one zip — what is on disk."""
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
                        "llm, detector.",
        ),
        attempt: Optional[int] = Query(
            default=None,
            description="LLM criteria only: the box from enforcement-loop attempt "
                        "n, accepted or not.",
        ),
        accepted: Optional[bool] = Query(
            default=None,
            description="LLM criteria only. true (the default when no `attempt` is "
                        "given) keeps the accepted attempt; false the rejected ones.",
        ),
    ):
        """Stream one artifact file, rendering it on first fetch when needed.

        Errors: **400** for an unknown criterion slug or a filter on a name
        that is not a layer, **404** for an unknown or unfinished job or a
        file that is not there and cannot be rendered, **410** when the job
        exists but its directory has been swept or deleted.
        """
        await _resolve(registry, job_id)
        try:
            store.path_of(job_id, name)  # name validation, before anything else
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        sources = {s.strip() for s in source.split(",") if s.strip()} if source else None
        filtering = bool(criterion or sources or attempt is not None or accepted is not None)
        layer = _LAYER_RE.match(name)

        # The text a criterion searched, as plain text.
        text_match = _TEXT_TXT_RE.match(name)
        if text_match and not filtering:
            data = store.read_json(job_id, f"text.{text_match.group('key')}.json")
            if data is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"job {job_id!r} has no text layer {text_match.group('key')!r}",
                )
            return Response(
                content=str(data.get("text", "")).encode("utf-8"),
                media_type="text/plain; charset=utf-8",
            )

        if not filtering:
            raw = store.open(job_id, name)
            if raw is not None:
                return Response(content=raw, media_type=content_type_for(name))
            if layer is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"{name!r} is not in job {job_id!r}'s artifact directory",
                )
            # A layer nobody has fetched yet: render it now and cache it.
            data = _load_regions(job_id)
            page = int(layer.group("page"))
            slug = layer.group("slug")
            fmt = _SUFFIX_TO_FORMAT[layer.group("suffix")]
            _, regions = _filtered(data, [slug] if slug else [], None, None, None)
            geometry = _geometry_for(data, page)
            logger.debug("get_artifact: first fetch of %s for job %s — rendering", name, job_id)
            return _render(job_id, fmt, geometry, [r for r in regions if r.page == page], name)

        data = _load_regions(job_id)
        kept, regions = _filtered(data, criterion, sources, attempt, accepted)
        if name == "regions.json":
            return JSONResponse(
                content={"job_id": data.get("job_id", job_id),
                         "pages": data.get("pages", []),
                         "items": data.get("items", {}), "criteria": kept}
            )
        if layer is None or layer.group("slug"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name!r} cannot be filtered — filters apply to regions.json and "
                    "the combined layers p{n}.svg / p{n}.layer.png / p{n}.preview.jpg"
                ),
            )
        page = int(layer.group("page"))
        fmt = _SUFFIX_TO_FORMAT[layer.group("suffix")]
        geometry = _geometry_for(data, page)
        page_regions = [r for r in regions if r.page == page]
        # Only the plain "this criterion, default subset" render is cached,
        # under the criterion's own layer name. A source or attempt filter is
        # a DIFFERENT subset, and caching it under that name would serve it
        # back to the next caller who asked for the plain one.
        cacheable = (
            len(criterion) == 1 and attempt is None and accepted is not False and not sources
        )
        cache_name = layer_name(fmt, criterion[0], page) if cacheable else None
        if cache_name:
            cached = store.open(job_id, cache_name)
            if cached is not None:
                return Response(content=cached, media_type=content_type_for(cache_name))
        logger.debug(
            "get_artifact: rendering %s for job %s (%d region(s) after filters)",
            name, job_id, len(page_regions),
        )
        return _render(job_id, fmt, geometry, page_regions, cache_name)

    @api.delete("/jobs/{job_id}/artifacts", status_code=204)
    async def delete_artifacts(job_id: str):
        """Remove the artifact directory, keeping the job row and its result."""
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
