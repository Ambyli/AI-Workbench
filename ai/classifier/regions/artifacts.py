"""Writing a job's artifact directory: geometry, layers, manifest, URLs.

One directory per job under CLASSIFIER_ARTIFACT_DIR, written once while the
job runs and served afterwards by ``api.artifacts``:

    regions.json   the canonical geometry, keyed BY CRITERION rather than as a
                   flat list — which is what makes "give me the artifact for
                   this criterion" a lookup instead of a client-side filter.
    p{n}.svg / p{n}.layer.png / p{n}.preview.jpg   the rendered layers the
                   request asked for, one set per page, plus the
                   un-annotated ``p{n}.base.jpg`` a FILTERED preview is
                   re-composited from.
    manifest.json  what actually survived the byte cap.

    _regions_json()          — the canonical file's shape.
    _artifact_url()          — the (relative) path the file endpoint serves at.
    write_region_artifacts() — the single-document flow: render, cap, manifest.
    write_compare_artifacts() — the compare flow's APPEND: diff layers, the
                               examples' own layers and their own
                               ``e{i}.regions.json``, and a rewritten manifest.
    _write_page_layers()     — render and store one page's requested formats.
    _criterion_artifacts()   — the per-criterion ``artifacts`` block, with one
                               entry per enforcement-loop attempt.
    _unimplemented_notes()   — say plainly which requested options this
                               endpoint could not honour.

Order of operations matters: layers are rendered first, THEN the byte cap is
enforced, THEN the manifest is written from what survived — so the manifest
can never promise a file the cap removed.

Blocking (rendering and file I/O), so callers run both writers in a worker
thread.

Process flow position: ``write_region_artifacts`` is step 9 of
``analysis.pipeline.analyze_document``; ``write_compare_artifacts`` is
``jobs.runners.run_compare``'s second pass, after the scoring is frozen.
"""

from common.documents import Document
from common.vision import (
    ArtifactStore,
    PageGeometry,
    Region,
    build_manifest,
    render_png_layer,
    render_preview,
    render_svg,
    slugify_criterion,
)

from api.schemas import CriterionInput, RegionsOptions
from config import (
    ARTIFACT_MAX_BYTES,
    DIFF_CRITERION_PREFIX,
    JOB_TTL_HOURS,
    LAYER_FILE_SUFFIXES,
    PREVIEW_JPEG_QUALITY,
)
from logger import logger
from regions.collect import visible_regions
from regions.store import store


def _regions_json(
    job_id: str,
    geometries: dict[int, PageGeometry],
    region_map: dict[str, list[Region]],
    criteria: list[CriterionInput],
    detector: dict | None = None,
    localizations: dict[str, dict] | None = None,
) -> dict:
    """The canonical ``regions.json``: keyed by criterion, not a flat list.

    Keying by criterion is what makes "give me the artifact for THIS
    criterion" a lookup rather than a client-side filter — the slug in each
    entry is the same one that appears in file names, SVG group ids, and the
    ``?criterion=`` query parameter.

    ``localizations`` is the LLM enforcement loop's record per criterion —
    every attempt, accepted or not, and which one was accepted. It is stored
    here in full; the job result carries the same object inline (it is a
    handful of numbers, never large).
    """
    types = {c.name: c.type for c in criteria}
    entries: dict[str, dict] = {}
    for name, regions in region_map.items():
        sources = sorted({r.source for r in regions})
        entries[name] = {
            "slug": slugify_criterion(name),
            "type": types.get(name, "llm"),
            "source": sources[0] if len(sources) == 1 else (sources or None),
            "sources": sources,
            "pages": sorted({r.page for r in regions}),
            "count": len(regions),
            "regions": [r.as_dict() for r in regions],
            "localization": (localizations or {}).get(name),
        }
    return {
        "job_id": job_id,
        "pages": [geometries[i].as_dict() for i in sorted(geometries)],
        # Which detector produced the source="detector" regions below, on
        # what device, and what it cost. Null when it was not used — a
        # consumer comparing two jobs' geometry needs to know whether the
        # same model drew the boxes.
        "detector": detector,
        "criteria": entries,
    }


def _artifact_url(job_id: str, name: str) -> str:
    """The path the artifact file endpoint serves ``name`` at.

    Relative on purpose: the same job is reachable directly on :8005 and
    through LiteLLM's ``/v1/classifier`` pass-through, and a stored absolute
    URL would be wrong for one of them.
    """
    return f"/jobs/{job_id}/artifacts/{name}"


def write_region_artifacts(
    job_id: str,
    doc: Document,
    geometries: dict[int, PageGeometry],
    region_map: dict[str, list[Region]],
    options: RegionsOptions,
    criteria: list[CriterionInput],
    detector: dict | None = None,
    extra_notes: list[str] | None = None,
    localizations: dict[str, dict] | None = None,
) -> tuple[dict, dict[str, dict | None]]:
    """Write the job's artifact directory and describe it.

    Blocking (rendering and file I/O), so callers run it in a worker thread.

    Order of operations matters: layers are rendered first, THEN the byte cap
    is enforced, THEN the manifest is written from what actually survived —
    so the manifest can never promise a file the cap removed.

    Args:
        job_id:     Directory name under CLASSIFIER_ARTIFACT_DIR.
        doc:        The analysed document (page images for the previews).
        geometries: Page frames.
        region_map: ``{criterion name: [Region, ...]}`` in original pixels.
        options:    The request's regions options.
        criteria:   Full criteria list (for the type of each entry).
        detector:   What the open-vocabulary detector did, or None when it
                    was not used — recorded in ``regions.json`` so a consumer
                    can tell which model drew the ``source="detector"`` boxes.
        extra_notes: Caller-facing sentences to append to the manifest's
                    ``notes`` (a detector that was asked for and could not be
                    reached, say). Joined with the not-implemented notes.
        localizations: ``{criterion name: localization dict}`` from the LLM
                    enforcement loop, stored whole in ``regions.json``.

    Returns:
        ``(artifacts_block, per_criterion)`` — the top-level ``artifacts``
        object for the result, and ``{criterion name: artifacts block or
        None}`` for the per-criterion results.
    """
    store.delete(job_id)  # a re-run of the same id must not inherit stale files

    all_regions = [r for regions in region_map.values() for r in regions]
    pages_with_regions = sorted({r.page for r in all_regions} & set(geometries))
    layers = set(options.layers)

    store.write_json(
        job_id,
        "regions.json",
        _regions_json(job_id, geometries, region_map, criteria, detector, localizations),
    )

    # ── Per-page combined layers ──────────────────────────────────────────
    # `visible_regions` drops the enforcement loop's REJECTED attempts: they
    # are all in regions.json and all reachable at `?attempt=n`, but three
    # overlapping boxes for one criterion is not a readable overlay. The page
    # list is built from every region so a page whose only findings were
    # rejected attempts still gets a (near-empty) file for that filter to
    # render against.
    for page_index in pages_with_regions:
        geometry = geometries[page_index]
        page_regions = visible_regions(r for r in all_regions if r.page == page_index)
        _write_page_layers(store, job_id, doc, geometry, page_regions, layers, "")

    # ── Optional per-criterion pre-render ─────────────────────────────────
    if options.layers_per_criterion:
        for name, regions in region_map.items():
            if not regions:
                continue
            slug = slugify_criterion(name)
            for page_index in sorted({r.page for r in regions} & set(geometries)):
                _write_page_layers(
                    store,
                    job_id,
                    doc,
                    geometries[page_index],
                    visible_regions(r for r in regions if r.page == page_index),
                    layers,
                    f"{slug}.",
                )

    dropped = store.enforce_cap(job_id)
    if dropped:
        logger.warning(
            "write_region_artifacts: job %s exceeded CLASSIFIER_ARTIFACT_MAX_BYTES "
            "(%d) — dropped %s",
            job_id,
            ARTIFACT_MAX_BYTES,
            dropped,
        )

    notes = _unimplemented_notes(options) + list(extra_notes or [])
    manifest = build_manifest(
        job_id,
        files=store.list(job_id),
        page_geometry=[geometries[i].as_dict() for i in sorted(geometries)],
        options=options.as_manifest(),
        criteria={
            name: {
                "slug": slugify_criterion(name),
                "count": len(regions),
                "pages": sorted({r.page for r in regions}),
                "sources": sorted({r.source for r in regions}),
            }
            for name, regions in region_map.items()
        },
        ttl_hours=JOB_TTL_HOURS,
        notes=notes,
        dropped=dropped,
    )
    store.write_json(job_id, "manifest.json", manifest)

    files = store.list(job_id)
    present = {f["name"] for f in files}
    artifacts = {
        "dir": str(store.dir_for(job_id)),
        "files": [{**f, "url": _artifact_url(job_id, f["name"])} for f in files],
        "zip_url": f"/jobs/{job_id}/artifacts.zip",
        "total_bytes": sum(f["bytes"] for f in files),
        "expires_at": manifest["expires_at"],
        "dropped": dropped,
        "notes": notes,
    }
    per_criterion = {
        name: _criterion_artifacts(job_id, name, regions, present)
        for name, regions in region_map.items()
    }
    logger.info(
        "write_region_artifacts: job %s wrote %d file(s), %d bytes, %d criteria with regions",
        job_id,
        len(files),
        artifacts["total_bytes"],
        sum(1 for r in region_map.values() if r),
    )
    return artifacts, per_criterion


def write_compare_artifacts(
    job_id: str,
    diff_regions: dict[str, list[Region]],
    subject_doc: Document,
    subject_geometries: dict[int, PageGeometry],
    examples: list[dict],
    options: RegionsOptions,
    extra_notes: list[str] | None = None,
) -> tuple[dict | None, dict[str, dict | None], dict[int, dict]]:
    """Add the compare flow's extra layers to a job directory already written.

    Blocking (rendering and file I/O), so the caller runs it in a thread.

    The subject's ``write_region_artifacts`` has already run — it deletes the
    directory and writes it fresh, so this cannot run before it. Everything
    here is therefore an APPEND: two new families of layer file, the diff
    criteria merged into the existing ``regions.json``, a per-example
    ``e{i}.regions.json`` (an example's pages are its own coordinate frame and
    must not be mixed into the subject's ``pages`` list), and a rewritten
    manifest so nothing promises a file that is not there.

    Args:
        job_id:             The SUBJECT's job — examples have none of their own.
        diff_regions:       ``{"_diff:e0": [Region, ...]}`` in the SUBJECT's
                            original page pixels.
        subject_doc:        For the diff previews' base pixels.
        subject_geometries: The subject's page frames.
        examples:           One dict per example whose layers are wanted:
                            ``{"index", "document", "geometries",
                            "region_map", "criteria", "localizations"}``.
        options:            The request's regions options (which layers).
        extra_notes:        Sentences to append to the manifest's ``notes``.

    Returns:
        ``(artifacts, diff_per_criterion, example_artifacts)`` — the refreshed
        top-level block, the per-criterion blocks for the ``_diff:e{i}``
        entries, and ``{example index: {"artifacts", "per_criterion"}}``.
        ``artifacts`` is None when the job has no directory to append to.
    """
    data = store.read_json(job_id, "regions.json")
    manifest = store.manifest(job_id)
    if data is None or manifest is None:
        logger.warning(
            "write_compare_artifacts: job %s has no artifact directory to append "
            "to — the subject's regions were never written", job_id,
        )
        return None, {}, {}

    layers = set(options.layers)

    # ── Diff layers: subject pixels, subject geometry, their own prefix ───
    for name, regions in diff_regions.items():
        index = _diff_example_index(name)
        for page_index in sorted({r.page for r in regions} & set(subject_geometries)):
            _write_page_layers(
                store,
                job_id,
                subject_doc,
                subject_geometries[page_index],
                [r for r in regions if r.page == page_index],
                layers,
                "",
                stem_prefix=f"diff-e{index}-",
                write_base=False,  # the subject's own p{n}.base.jpg is the base
            )

    # ── Example layers: the example's own pixels and frames ───────────────
    example_artifacts: dict[int, dict] = {}
    for entry in examples:
        index = entry["index"]
        prefix = f"e{index}."
        geometries: dict[int, PageGeometry] = entry.get("geometries") or {}
        region_map: dict[str, list[Region]] = entry.get("region_map") or {}
        all_regions = [r for rs in region_map.values() for r in rs]
        for page_index in sorted({r.page for r in all_regions} & set(geometries)):
            _write_page_layers(
                store,
                job_id,
                entry["document"],
                geometries[page_index],
                visible_regions(r for r in all_regions if r.page == page_index),
                layers,
                "",
                stem_prefix=prefix,
            )
        store.write_json(
            job_id,
            f"{prefix}regions.json",
            _regions_json(
                job_id,
                geometries,
                region_map,
                entry.get("criteria") or [],
                None,
                entry.get("localizations") or {},
            ),
        )
        example_artifacts[index] = {"prefix": prefix, "region_map": region_map}

    dropped = list(manifest.get("dropped") or []) + store.enforce_cap(job_id)
    files = store.list(job_id)
    present = {f["name"] for f in files}
    notes = list(manifest.get("notes") or []) + list(extra_notes or [])

    # ── Rewrite the manifest from what actually survived the cap ──────────
    manifest["files"] = files
    manifest["total_bytes"] = sum(f["bytes"] for f in files)
    manifest["dropped"] = dropped
    manifest["notes"] = notes
    manifest["criteria"] = {
        **manifest.get("criteria", {}),
        **{
            name: {
                "slug": slugify_criterion(name),
                "count": len(regions),
                "pages": sorted({r.page for r in regions}),
                "sources": sorted({r.source for r in regions}),
            }
            for name, regions in diff_regions.items()
        },
    }
    store.write_json(job_id, "manifest.json", manifest)

    # ── Merge the diff criteria into the subject's regions.json ───────────
    # In the SUBJECT's file because a diff's coordinates are the subject's;
    # its `type` is stamped afterwards because `_regions_json` reads types off
    # a criteria list and `_diff:e0` is not a criterion to be on one.
    merged = _regions_json(
        job_id, subject_geometries, diff_regions, [], None, None
    )
    for entry in merged["criteria"].values():
        entry["type"] = "diff"
    data["criteria"] = {**data.get("criteria", {}), **merged["criteria"]}
    store.write_json(job_id, "regions.json", data)

    artifacts = {
        "dir": str(store.dir_for(job_id)),
        "files": [{**f, "url": _artifact_url(job_id, f["name"])} for f in files],
        "zip_url": f"/jobs/{job_id}/artifacts.zip",
        "total_bytes": manifest["total_bytes"],
        "expires_at": manifest["expires_at"],
        "dropped": dropped,
        "notes": notes,
    }
    diff_per_criterion = {
        name: _criterion_artifacts(
            job_id, name, regions, present, f"diff-e{_diff_example_index(name)}-"
        )
        for name, regions in diff_regions.items()
    }
    for index, entry in example_artifacts.items():
        prefix = entry["prefix"]
        entry["artifacts"] = {
            "dir": str(store.dir_for(job_id)),
            "files": [
                {**f, "url": _artifact_url(job_id, f["name"])}
                for f in files
                if f["name"].startswith(prefix)
            ],
            "zip_url": f"/jobs/{job_id}/artifacts.zip",
            "expires_at": manifest["expires_at"],
            "notes": [
                f"Example {index} has no job of its own: its layers live in the "
                f"subject's artifact directory under the `{prefix}` prefix."
            ],
        }
        entry["artifacts"]["total_bytes"] = sum(
            f["bytes"] for f in entry["artifacts"]["files"]
        )
        entry["per_criterion"] = {
            name: _criterion_artifacts(job_id, name, regions, present, prefix)
            for name, regions in entry["region_map"].items()
        }
        entry.pop("region_map")

    logger.info(
        "write_compare_artifacts: job %s now holds %d file(s), %d bytes "
        "(%d diff criteria, %d example(s) rendered)",
        job_id, len(files), manifest["total_bytes"],
        len(diff_regions), len(example_artifacts),
    )
    return artifacts, diff_per_criterion, example_artifacts


def _write_page_layers(
    store: ArtifactStore,
    job_id: str,
    doc: Document,
    geometry: PageGeometry,
    regions: list[Region],
    layers: set[str],
    prefix: str,
    stem_prefix: str = "",
    write_base: bool = True,
) -> None:
    """Render and store the requested formats for one page.

    ``prefix`` is "" for the combined layer and ``"<slug>."`` for a
    pre-rendered per-criterion one, which is the same naming the file
    endpoint uses when it caches a filtered render — so a pre-rendered file
    and a lazily cached one are the same file.

    ``stem_prefix`` goes in FRONT of the page number and is what gives the
    compare flow its two extra families in the same directory (§ 0 of the
    regions plan):

        ""            the subject's own layers — ``p0.svg``
        "diff-e0-"    change detection against example 0 — ``diff-e0-p0.svg``
        "e0."         example 0's own layers, with ``regions.examples`` on —
                      ``e0.p0.svg``

    Examples have no job of their own, so their layers live in the subject's
    directory; the prefix is what keeps the two from colliding.

    The preview also writes ``{stem_prefix}p{n}.base.jpg``: a preview has its
    overlay burned into the pixels and cannot be un-burned, so a FILTERED
    preview has to be composited from a clean copy of the page. ``write_base``
    is False for the diff layers, whose pixels ARE the subject's page — its
    ``p{n}.base.jpg`` is already there and a second copy under a diff name
    would just be the same JPEG against the byte cap twice.
    """
    page = doc.page(geometry.page)
    stem = f"{stem_prefix}p{geometry.page}.{prefix}"

    if "svg" in layers:
        store.write(job_id, f"{stem}svg", render_svg(geometry, regions))
    if "png" in layers:
        store.write(job_id, f"{stem}layer.png", render_png_layer(geometry, regions))
    if "preview" in layers and page is not None and page.image_bgr is not None:
        rgb = page.image_bgr[:, :, ::-1]  # the renderer wants RGB, OpenCV holds BGR
        store.write(
            job_id,
            f"{stem}preview.jpg",
            render_preview(rgb, geometry, regions, quality=PREVIEW_JPEG_QUALITY),
        )
        base_name = f"{stem_prefix}p{geometry.page}.base.jpg"
        if write_base and not prefix and store.open(job_id, base_name) is None:
            store.write(
                job_id,
                base_name,
                render_preview(rgb, geometry, [], quality=PREVIEW_JPEG_QUALITY),
            )


def _diff_example_index(name: str) -> str:
    """``_diff:e3`` → ``"3"``; the stem prefix builder's only input."""
    return name[len(DIFF_CRITERION_PREFIX):] or "0"


def _criterion_artifacts(
    job_id: str,
    name: str,
    regions: list[Region],
    present: set[str],
    stem_prefix: str = "",
) -> dict | None:
    """The per-criterion ``artifacts`` block, or None when it has no regions.

    Only pages where THIS criterion actually has regions are listed, and only
    formats that were really written (a layer the byte cap dropped, or a
    format the request never asked for, is simply absent rather than a URL
    that 404s). The URLs carry ``?criterion=<slug>``, so following one gets a
    layer filtered to this criterion alone — which for an ``llm`` criterion
    means its ACCEPTED box, since that is the default when no ``attempt`` is
    named.

    ``attempts`` is the other half of returning every attempt: one entry per
    enforcement-loop attempt that produced a drawable box, carrying
    ``?criterion=<slug>&attempt=<n>`` so a REJECTED box can be looked at on
    its own. Absent (empty) for every path but ``llm``.

    ``stem_prefix`` matches ``_write_page_layers``: "" for the subject,
    ``"e0."`` for an example's own layers in the same directory,
    ``"diff-e0-"`` for a change-detection layer. An example keeps its own
    ``e0.regions.json`` — its pages are a different coordinate frame from the
    subject's — so the ``regions_url`` follows the prefix; a diff is in the
    subject's frame and stays in the subject's ``regions.json``.
    """
    if not regions:
        return None
    slug = slugify_criterion(name)
    regions_name = (
        f"{stem_prefix}regions.json"
        if stem_prefix and not stem_prefix.startswith("diff-")
        else "regions.json"
    )
    pages = []
    for page_index in sorted({r.page for r in regions}):
        entry: dict = {"page": page_index}
        for key, suffix in LAYER_FILE_SUFFIXES:
            filename = f"{stem_prefix}p{page_index}.{suffix}"
            if filename in present:
                entry[key] = f"{_artifact_url(job_id, filename)}?criterion={slug}"
        pages.append(entry)

    attempts = []
    numbered = [
        r for r in regions
        if r.source == "llm" and isinstance(r.attrs.get("attempt"), int)
    ]
    for region in sorted(numbered, key=lambda r: r.attrs["attempt"]):
        number = region.attrs["attempt"]
        entry = {
            "attempt": number,
            "page": region.page,
            "accepted": bool(region.attrs.get("accepted")),
        }
        for key, suffix in LAYER_FILE_SUFFIXES:
            filename = f"{stem_prefix}p{region.page}.{suffix}"
            if filename in present:
                entry[key] = (
                    f"{_artifact_url(job_id, filename)}?criterion={slug}&attempt={number}"
                )
        attempts.append(entry)

    return {
        "slug": slug,
        "regions_url": f"{_artifact_url(job_id, regions_name)}?criterion={slug}",
        "pages": pages,
        "attempts": attempts,
    }


def _unimplemented_notes(options: RegionsOptions) -> list[str]:
    """Say plainly which requested options this build could not honour.

    Recorded in the manifest and echoed in ``artifacts.notes`` rather than
    failing the request: silently dropping a requested option is how a caller
    ends up debugging an empty ``localization``.

    Every `regions` field is implemented as of phases 3 and 4, so the only
    entries left are the two that are meaningless on the endpoint they were
    sent to — `diff` and `examples` are compare-only, and a caller who sets
    them on `/assess` or `/locate` should be told rather than left wondering.
    """
    notes: list[str] = []
    if options.diff:
        notes.append(
            "regions.diff was requested on a single-document job. Change "
            "detection needs a reference to compare against, so it applies to "
            "/assess/compare only; no diff layers were written."
        )
    if options.examples:
        notes.append(
            "regions.examples was requested on a single-document job. It "
            "applies to /assess/compare only; layers were rendered for this "
            "document alone."
        )
    for note in notes:
        logger.info("regions: %s", note)
    return notes
