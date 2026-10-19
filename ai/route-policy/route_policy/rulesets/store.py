"""Ruleset versions and bindings: seeding at startup, and the in-memory cache
every /decide reads.

**Seeding** (``seed_and_load``, once per startup): the baked-in files are
read and validated (``loader.load_seed`` -- an invalid file stops startup),
then in ONE transaction under an advisory lock, so two copies starting
together during a swap cannot race:

1. each ruleset version is inserted unless it exists; an existing one must
   have the same content hash, or startup fails (``SeedConflict``) --
   versions are immutable;
2. each seed binding is pointed at its version (``auto`` -> ``default`` @ the
   highest version baked in);
3. the ``active_from`` / ``active_to`` marks are refreshed.

**The cache** (``RulesetStore``): compiled rulesets by (id, version) --
immutable, so cached for the life of the process -- and a snapshot of the
bindings, re-read when older than ROUTE_POLICY_BINDING_REFRESH_S. A copy
that cannot reach the database keeps deciding from its last snapshot; a
version this copy has not compiled is fetched and compiled on first use.
"""

from __future__ import annotations

import logging
import pathlib
import time
from typing import Callable, Dict, List, Optional, Tuple

from .. import metrics
from ..db.repositories import rulesets as rulesets_repo
from .loader import Binding, Seed, load_seed
from .model import CompiledRuleset
from .validate import RulesetInvalid, compile_ruleset

logger = logging.getLogger("route_policy.rulesets")


class RulesetStore:
    def __init__(self, database=None, *, refresh_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.database = database
        self.refresh_s = refresh_s
        self._clock = clock
        self._compiled: Dict[Tuple[str, int], CompiledRuleset] = {}
        self._bindings: List[Binding] = []
        self._bindings_at: Optional[float] = None

    # -- filling the cache ---------------------------------------------------

    def add(self, ruleset: CompiledRuleset) -> None:
        self._compiled[(ruleset.id, ruleset.version)] = ruleset

    def set_bindings(self, bindings: List[Binding]) -> None:
        self._bindings = list(bindings)
        self._bindings_at = self._clock()

    @property
    def compiled(self) -> List[CompiledRuleset]:
        return [self._compiled[k] for k in sorted(self._compiled)]

    @property
    def bindings(self) -> List[Binding]:
        return list(self._bindings)

    async def refresh_bindings(self) -> bool:
        """Re-read the bindings table. False (and the old snapshot kept) when
        there is no database or it cannot be read."""
        if self.database is None:
            return False
        try:
            async with self.database.acquire() as conn:
                rows = await rulesets_repo.list_bindings(conn)
        except Exception as exc:  # noqa: BLE001 -- keep deciding from the last snapshot
            metrics.errors_total.labels("binding_refresh").inc()
            logger.warning("rulesets: binding refresh failed, keeping the last snapshot: %r", exc)
            self._bindings_at = self._clock()  # do not retry on every request
            return False
        self.set_bindings([Binding(r["scope_model"], r["scope_key_alias"], r["ruleset_id"], r["version"])
                           for r in rows])
        return True

    # -- lookups ----------------------------------------------------------------

    def binding_for(self, model: str, key_alias: Optional[str]) -> Optional[Binding]:
        """The binding for (model, key alias), else the model's any-key binding."""
        exact = any_key = None
        for b in self._bindings:
            if b.scope_model != model:
                continue
            if key_alias is not None and b.scope_key_alias == key_alias:
                exact = b
            elif b.scope_key_alias is None:
                any_key = b
        return exact or any_key

    async def resolve(self, model: str, key_alias: Optional[str]) -> Optional[CompiledRuleset]:
        """The ruleset bound to (model, key alias), or None when nothing is bound."""
        stale = self._bindings_at is None or self._clock() - self._bindings_at >= self.refresh_s
        if stale and self.database is not None:
            await self.refresh_bindings()
        binding = self.binding_for(model, key_alias)
        if binding is None:
            return None
        return await self.get(binding.ruleset_id, binding.version)

    async def get(self, ruleset_id: str, version: Optional[int] = None) -> Optional[CompiledRuleset]:
        """A compiled version (``None`` = the highest), from the cache or the
        database. None when it does not exist or this copy cannot compile it."""
        if version is not None and (ruleset_id, version) in self._compiled:
            return self._compiled[(ruleset_id, version)]
        if self.database is None:
            if version is not None:
                return None
            cached = [v for (rid, v) in self._compiled if rid == ruleset_id]
            return self._compiled[(ruleset_id, max(cached))] if cached else None
        try:
            async with self.database.acquire() as conn:
                row = await rulesets_repo.get(conn, ruleset_id, version)
        except Exception as exc:  # noqa: BLE001
            logger.warning("rulesets: cannot read %s@%s: %r", ruleset_id, version, exc)
            if version is None:
                cached = [v for (rid, v) in self._compiled if rid == ruleset_id]
                return self._compiled[(ruleset_id, max(cached))] if cached else None
            return None
        if row is None:
            return None
        key = (row["id"], row["version"])
        if key in self._compiled:
            return self._compiled[key]
        try:
            compiled = compile_ruleset(row["document"], sha256=row["content_sha256"])
        except RulesetInvalid as exc:
            metrics.errors_total.labels("ruleset_compile").inc()
            logger.error("rulesets: stored %s@%s cannot be compiled by this copy: %s", key[0], key[1], exc)
            return None
        self.add(compiled)
        return compiled


async def seed_and_load(store: RulesetStore, database, rulesets_dir: pathlib.Path) -> Seed:
    """Seed the baked-in rulesets and bindings, then fill ``store``.

    Raises (and so stops startup) on an invalid file, a ruleset version whose
    content changed (``rulesets_repo.SeedConflict``), or an unreachable
    database."""
    seed = load_seed(rulesets_dir)
    async with database.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", rulesets_repo.SEED_LOCK)
            results = await rulesets_repo.seed(conn, seed.rulesets)
            for b in seed.bindings:
                if await rulesets_repo.upsert_binding(conn, b.scope_model, b.scope_key_alias,
                                                      b.ruleset_id, b.version):
                    logger.info("rulesets: binding %s%s -> %s@%d", b.scope_model,
                                f" (key {b.scope_key_alias})" if b.scope_key_alias else "", b.ruleset_id, b.version)
            await rulesets_repo.refresh_active_marks(conn)
        rows = await rulesets_repo.list_bindings(conn)
    for r in results:
        if r["inserted"]:
            logger.info("rulesets: seeded %s@%d", r["id"], r["version"])
    for compiled in seed.compiled:
        store.add(compiled)
    store.set_bindings([Binding(r["scope_model"], r["scope_key_alias"], r["ruleset_id"], r["version"])
                        for r in rows])
    return seed
