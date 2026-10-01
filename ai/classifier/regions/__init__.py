"""Regions: turning "where did you find it" into files and result fields.

The package reads geometry OFF the outcomes the evaluators already produced
and only ever ADDS keys, which is why storing regions can never move a score
or flip a verdict. Every job writes its directory; the rendered layers are
produced lazily by ``api.artifacts`` through ``artifacts.render_layer``.

    store.py     the one ``common.vision.ArtifactStore`` instance for the
                 process — the routes, the writers, the delete hook and the
                 sweeper all share it, so the byte cap lives in one place.
    collect.py   what a layer shows, and the inline copy that goes into a
                 result.
    artifacts.py the writers: regions.json, the ``text.<key>.json`` text
                 layers, the base image, the manifest, the per-criterion
                 ``artifacts`` blocks — and the one layer renderer.
    sweeper.py   the TTL: the background task, the DELETE /jobs/{id} hook, and
                 the disk gauges.

Nothing here imports ``analysis`` — the dependency runs the other way, which
is what keeps the step additive.

Submodules are imported explicitly (``from regions.artifacts import ...``) so
that ``api`` can stay above this package without an import cycle.
"""
