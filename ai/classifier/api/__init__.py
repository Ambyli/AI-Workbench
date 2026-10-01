"""The HTTP layer: request shapes, endpoint handlers, and nothing else.

    schemas.py           the Pydantic models every endpoint validates against
                         (AssessRequest, CriterionInput, DocumentInput).
    criterion_options.py the per-type options models, their defaults and caps.
    assess.py            POST /assess — JSON or multipart, one model.
    introspection.py     GET /criterion-types, /hints, /cv-detectors,
                         /document-kinds, /health.
    artifacts.py         the four /jobs/{id}/artifacts routes, and the lazy
                         layer renderer behind them.

Each handler module exposes a ``router`` that ``main`` mounts; ``artifacts``
exposes a factory instead, because every one of its routes has to look the job
up first.

Deliberately NOTHING is imported here. ``regions``, ``analysis`` and ``llm``
all import ``api.schemas``, so a router import in this file would close a
cycle back through ``jobs``. Import the submodule you mean.
"""
