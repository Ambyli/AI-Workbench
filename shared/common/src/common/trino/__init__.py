"""
common.trino — thin wrapper over the Trino Python driver.

Two consumers today:

* ``ai/trino/mcp/server.py`` — LiteLLM-facing SQL tool. Discovery calls
  are unrestricted; ``run_query`` runs the read-only guardrail path
  (SELECT-only, LIMIT-clamped, timeout-clamped).
* ``ai/trino/bin/init_warehouse.py`` — one-shot warehouse seed. Runs
  with ``allow_writes=True`` so DDL/DML pass through.

Optional dep: ``trino``. If a consumer imports this module without
having the driver installed, they get a clean ImportError at import
time.
"""
from .client import TrinoClient, TrinoQueryError

__all__ = ["TrinoClient", "TrinoQueryError"]
