"""rulesets + bindings (+ their shadow slots): seeding, reads.

Every function takes an open connection, so a caller can group several in
one transaction (startup seeds rulesets, bindings, shadow slots and the
active marks together, under an advisory lock).

A binding's shadow slots live in ``binding_shadows`` (0002): ``(binding_id,
position) -> (ruleset_id, version)``, cascading with the binding. Reads
return them as ``shadows: [{"ruleset_id", "version"}, ...]`` on each binding,
in order.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SEED_LOCK = "route_policy.seed"
_SUMMARY_COLUMNS = ("id, version, content_sha256, source, created_at, created_by, active_from, active_to, "
                    "document->>'mode' AS mode, document->>'pin' AS pin, document->>'description' AS description")


class SeedConflict(RuntimeError):
    """A baked-in ruleset version's content differs from the stored one."""


async def seed(conn: Any, rulesets: Iterable[Any], created_by: str = "seed") -> List[Dict[str, Any]]:
    """Insert each ``LoadedRuleset`` unless its id + version exists; an
    existing one must have the same content hash. Returns one
    ``{id, version, inserted}`` per ruleset. Raises SeedConflict listing
    every mismatch."""
    results, conflicts = [], []
    for loaded in rulesets:
        doc = loaded.document
        rid, version = doc["id"], int(doc["version"])
        inserted = await conn.fetchval(
            "INSERT INTO rulesets (id, version, document, content_sha256, source, created_by) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (id, version) DO NOTHING RETURNING true",
            rid, version, doc, loaded.sha256, loaded.source, created_by,
        )
        if not inserted:
            stored = await conn.fetchval(
                "SELECT content_sha256 FROM rulesets WHERE id = $1 AND version = $2", rid, version)
            if stored != loaded.sha256:
                conflicts.append(
                    f"{rid}@{version} ({loaded.source}): stored content {stored[:12]} differs from "
                    f"the baked-in file {loaded.sha256[:12]}")
        results.append({"id": rid, "version": version, "inserted": bool(inserted)})
    if conflicts:
        raise SeedConflict(
            "ruleset versions are immutable, and these changed without a version bump -- give the "
            "edited ruleset a new `version`: " + "; ".join(conflicts))
    return results


async def upsert_binding(conn: Any, scope_model: str, scope_key_alias: Optional[str], ruleset_id: str,
                         version: int, updated_by: str = "seed") -> bool:
    """Point (model, key alias) at a ruleset version. True when it changed."""
    changed = await conn.fetchval(
        "INSERT INTO bindings (scope_model, scope_key_alias, ruleset_id, version, updated_by) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (scope_model, scope_key_alias) DO UPDATE "
        "SET ruleset_id = EXCLUDED.ruleset_id, version = EXCLUDED.version, "
        "    updated_at = now(), updated_by = EXCLUDED.updated_by "
        "WHERE (bindings.ruleset_id, bindings.version) IS DISTINCT FROM (EXCLUDED.ruleset_id, EXCLUDED.version) "
        "RETURNING true",
        scope_model, scope_key_alias, ruleset_id, int(version), updated_by,
    )
    return bool(changed)


async def set_binding_shadows(conn: Any, scope_model: str, scope_key_alias: Optional[str],
                              shadows: Sequence[Tuple[str, int]]) -> bool:
    """Replace the shadow slots of the binding for (model, key alias), in
    order. True when they changed (nothing is rewritten otherwise)."""
    binding_id = await conn.fetchval(
        "SELECT id FROM bindings WHERE scope_model = $1 AND scope_key_alias IS NOT DISTINCT FROM $2",
        scope_model, scope_key_alias)
    if binding_id is None:
        raise LookupError(f"no binding for {scope_model!r} / {scope_key_alias!r}")
    current = [(r["ruleset_id"], r["version"]) for r in await conn.fetch(
        "SELECT ruleset_id, version FROM binding_shadows WHERE binding_id = $1 ORDER BY position", binding_id)]
    wanted = [(rid, int(version)) for rid, version in shadows]
    if current == wanted:
        return False
    await conn.execute("DELETE FROM binding_shadows WHERE binding_id = $1", binding_id)
    await conn.executemany(
        "INSERT INTO binding_shadows (binding_id, position, ruleset_id, version) VALUES ($1, $2, $3, $4)",
        [(binding_id, i, rid, version) for i, (rid, version) in enumerate(wanted)])
    await conn.execute("UPDATE bindings SET updated_at = now() WHERE id = $1", binding_id)
    return True


_BOUND = ("(EXISTS (SELECT 1 FROM bindings b WHERE b.ruleset_id = r.id AND b.version = r.version) "
          " OR EXISTS (SELECT 1 FROM binding_shadows s WHERE s.ruleset_id = r.id AND s.version = r.version))")


async def refresh_active_marks(conn: Any) -> None:
    """``active_from`` = first time any binding -- or shadow slot -- pointed
    at a version (and ``active_to`` cleared while one does); ``active_to`` =
    when none does."""
    await conn.execute(
        "UPDATE rulesets r SET active_from = COALESCE(r.active_from, now()), active_to = NULL "
        f"WHERE {_BOUND} AND (r.active_from IS NULL OR r.active_to IS NOT NULL)")
    await conn.execute(
        "UPDATE rulesets r SET active_to = now() "
        f"WHERE r.active_from IS NOT NULL AND r.active_to IS NULL AND NOT {_BOUND}")


async def list_bindings(conn: Any) -> List[Dict[str, Any]]:
    """Every binding with its shadow slots (``shadows``, in order)."""
    rows = await conn.fetch(
        "SELECT b.scope_model, b.scope_key_alias, b.ruleset_id, b.version, b.updated_at, b.updated_by, "
        "  COALESCE((SELECT jsonb_agg(jsonb_build_object('ruleset_id', s.ruleset_id, 'version', s.version) "
        "                             ORDER BY s.position) "
        "            FROM binding_shadows s WHERE s.binding_id = b.id), '[]'::jsonb) AS shadows "
        "FROM bindings b ORDER BY b.scope_model, b.scope_key_alias NULLS FIRST")
    return [dict(r) for r in rows]


async def list_rulesets(conn: Any) -> List[Dict[str, Any]]:
    rows = await conn.fetch(f"SELECT {_SUMMARY_COLUMNS} FROM rulesets ORDER BY id, version")
    return [dict(r) for r in rows]


async def versions_of(conn: Any, ruleset_id: str) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        f"SELECT {_SUMMARY_COLUMNS} FROM rulesets WHERE id = $1 ORDER BY version", ruleset_id)
    return [dict(r) for r in rows]


async def get(conn: Any, ruleset_id: str, version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """One version with its document; ``version`` None = the highest."""
    if version is None:
        row = await conn.fetchrow(
            f"SELECT {_SUMMARY_COLUMNS}, document FROM rulesets WHERE id = $1 ORDER BY version DESC LIMIT 1",
            ruleset_id)
    else:
        row = await conn.fetchrow(
            f"SELECT {_SUMMARY_COLUMNS}, document FROM rulesets WHERE id = $1 AND version = $2",
            ruleset_id, int(version))
    return dict(row) if row is not None else None
