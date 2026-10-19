"""route-policy-db access.

    pool.py          Database: the one asyncpg pool (+ one-off connections off its loop)
    migrations/      numbered SQL files, applied once each at startup
    repositories/    SQL per table group
"""
