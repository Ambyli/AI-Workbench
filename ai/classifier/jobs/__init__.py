"""The async job pattern: payloads on disk, workers on the queue, runners.

POST /assess, /assess/compare and /locate all return 202 immediately, so the
work happens here:

    payloads.py  the JSON-safe dict each endpoint stores at enqueue time
                 (file bytes base64'd, Pydantic models dumped) so a queued job
                 survives a restart.
    runners.py   the actual work: run_assess / run_compare / run_locate. They
                 know nothing about the registry, the worker pool, or
                 Prometheus.
    queue.py     ClassifierQueue — the payload store, the worker pool, and the
                 process-wide registry / queue / sweeper singletons the app
                 and every endpoint share.

Why the DB is the queue, not an asyncio.Queue: an in-memory queue loses every
pending job on restart and cannot be shared across processes. With the
registry as the queue, ``start()`` requeues rows a previous process left in
"processing", and the payload on disk means the work can actually be redone.

Layering: this package may import ``analysis``, ``compare`` and ``regions``;
``api`` imports it, and nothing here imports ``api`` except ``api.schemas``.
"""
