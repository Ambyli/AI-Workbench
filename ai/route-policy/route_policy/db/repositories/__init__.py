"""SQL, one module per table group. Every function takes an open asyncpg
connection (``async with database.acquire() as conn``), so callers choose
the transaction boundaries.

    rulesets.py      rulesets + bindings: seed (immutable versions), upsert, reads
    decisions.py     decisions (+ evaluations, same transaction), idempotent insert, per-chat reads
    evaluations.py   the per-evaluator rows
    outcomes.py      outcomes, idempotent insert

Phase 4 adds analyses.py beside them.
"""
