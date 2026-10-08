"""References: stored, reviewed examples the vision model is shown as guidance.

A reference is ONE page — a JPEG/PNG, or one page of a PDF — with the
criteria asked of it, the answer each one should get, and where on the page
the feature is. Creating one runs it as a job (``jobs.runners.run_reference``):
whatever the caller supplied (a breakdown, regions) is kept, whatever is
missing is classified and located by the pipeline, and the merged record is
frozen. After that its content never changes; only its title, description
and tags may be edited.

    model.py     the Reference row, the id grammar, the file-name grammar, and
                 which criteria guide the vision model (``guides_llm``).
    store.py     ``ReferenceRegistry`` (the ``reference_examples`` table in
                 the classifier-db Postgres) and the reference file store — a
                 ``common.vision.ArtifactStore`` rooted at
                 CLASSIFIER_REFERENCE_DIR that nothing sweeps — plus the
                 startup reconcile and the gauges.
    resolve.py   an /assess request's ``references`` against the store, at
                 submit: unknown / not-ready ids, inherited criteria, which
                 examples guide which criterion — the plan the worker follows
                 (``analysis.references.JobReferences``), and the ``auto``
                 candidate pool.
    finalize.py  caller answers + pipeline answers → the frozen record
                 (caller beats pipeline; no region means the whole page).
    render.py    the files: page.jpg, working.jpg, and one composite
                 ``c.<slug>.jpg`` per criterion that guides the vision model —
                 the working image with that criterion's regions drawn, which
                 is the exact image an example shows the model.

Layering: this package sits beside ``cv`` / ``llm`` / ``regions`` — it may
import ``config``, ``api.schemas``, ``cv`` and ``common``, and must never
import ``analysis`` or ``llm``. The runner (``jobs.runners``) is what joins it
to the pipeline: it hands this package plain data (arrays, outcome dicts),
never a pipeline object.
"""
