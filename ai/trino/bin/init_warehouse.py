"""One-shot: seed the Iceberg warehouse with a demo table so the
end-to-end verification steps in TRINO.md have something to hit.

Run once after `docker compose -f ai/trino/docker-compose.trino.yml up -d`:

    docker compose -f ai/trino/docker-compose.trino.yml exec trino-mcp \\
        python /app/ai/trino/bin/init_warehouse.py

Creates ``iceberg.demo`` schema (if missing) and ``iceberg.demo.events``
table with three synthetic rows. Idempotent — re-runs replace the rows,
not the table, so the Parquet layout on MinIO stays stable across
sessions.
"""
from __future__ import annotations

import sys

from common.trino import TrinoClient, TrinoQueryError


def main() -> int:
    client = TrinoClient(
        host="trino-coordinator",
        port=8080,
        user="warehouse-init",
        allow_writes=True,
    )

    print("Creating iceberg.demo schema…")
    client.execute(
        "CREATE SCHEMA IF NOT EXISTS iceberg.demo "
        "WITH (location = 's3a://warehouse/demo')"
    )

    print("Creating iceberg.demo.events table…")
    client.execute(
        """
        CREATE TABLE IF NOT EXISTS iceberg.demo.events (
            id BIGINT,
            kind VARCHAR,
            created_at TIMESTAMP(6) WITH TIME ZONE
        )
        """
    )

    print("Loading seed rows…")
    client.execute("DELETE FROM iceberg.demo.events")
    client.execute(
        """
        INSERT INTO iceberg.demo.events VALUES
            (1, 'signup',   TIMESTAMP '2026-08-27 09:00:00 UTC'),
            (2, 'login',    TIMESTAMP '2026-08-27 09:05:00 UTC'),
            (3, 'purchase', TIMESTAMP '2026-08-27 09:12:34 UTC')
        """
    )

    _, rows = client.execute("SELECT count(*) FROM iceberg.demo.events")
    print(f"iceberg.demo.events now contains {rows[0][0]} rows.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TrinoQueryError as e:
        print(f"init failed: {e}", file=sys.stderr)
        sys.exit(1)
