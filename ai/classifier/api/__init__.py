"""The HTTP layer: request shapes, endpoint handlers, and nothing else.

    schemas.py       the Pydantic models every endpoint validates against.
    assess.py        POST /assess, POST /assess/compare.
    locate.py        POST /locate, and the `features` field it parses.
    introspection.py GET /hints, /cv-detectors, /document-kinds, /health.
    artifacts.py     the four /jobs/{id}/artifacts routes.

Each handler module exposes a ``router`` that ``main`` mounts; ``artifacts``
exposes a factory instead, because every one of its routes has to look the job
up first.

Deliberately NOTHING is imported here. ``regions``, ``analysis`` and ``llm``
all import ``api.schemas``, so a router import in this file would close a
cycle back through ``jobs``. Import the submodule you mean.
"""
