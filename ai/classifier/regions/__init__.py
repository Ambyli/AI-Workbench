"""Regions: turning "where did you find it" into files and result fields.

The package reads geometry OFF the results the detectors and matchers already
produced and only ever ADDS keys, which is why turning regions on can never
move a score or flip a verdict.

    store.py     the one ``common.vision.ArtifactStore`` instance for the
                 process — the routes, the writers, the delete hook and the
                 sweeper all share it, so the byte cap lives in one place.
    collect.py   what a layer shows, the inline copy that goes into a result,
                 and the synthetic criterion a diff is filed under.
    artifacts.py the writers: regions.json, the rendered layers, the manifest,
                 and the per-criterion ``artifacts`` blocks.
    sweeper.py   the TTL: the background task, the DELETE /jobs/{id} hook, and
                 the disk gauges.

Nothing here imports ``analysis`` — the dependency runs the other way, which
is what keeps the step additive.

Submodules are imported explicitly (``from regions.artifacts import ...``) so
that ``api`` can stay above this package without an import cycle.
"""
