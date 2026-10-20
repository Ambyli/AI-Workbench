# Build context is the REPO ROOT (see ai/route-policy/docker-compose.route-policy.yml:
#   context: ../..) so the shared workspace package can be copied alongside
# the service. Same pattern as ai/classifier/Dockerfile.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    PYTHONPATH=/app/ai/route-policy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Workspace bootstrap. Only common's stdlib-only subpackages are imported
# (common.conversation, common.keywordmatch, common.net's validate-side
# DEFAULT_BLOCKED_NETWORKS), but the package is installed whole, as every
# service here does.
COPY pyproject.toml .
COPY shared/common ./shared/common
COPY ai/route-policy/pyproject.toml ./ai/route-policy/

RUN uv sync --package route-policy --no-dev

# Service code: the package (its migrations/*.sql ride along inside it), the
# rulesets it seeds at startup, and the classifier benchmark (bench/, run as
# a one-off container from this image: ai/route-policy/bench/BENCH.md). No
# *.md beyond what bench/ carries, no Postman collection, no compose file --
# listing the directories ships what runs, without a repo-root .dockerignore
# (which would change the build context of every other image). Same
# approach as ai/classifier/Dockerfile.
COPY ai/route-policy/route_policy ./ai/route-policy/route_policy
COPY ai/route-policy/rulesets ./ai/route-policy/rulesets
COPY ai/route-policy/bench ./ai/route-policy/bench

# Import the app the way the CMD will, and load + VALIDATE the baked-in
# rulesets and bindings (rulesets.loader.load_seed compiles every one, and
# refuses a shadow ruleset as a binding's primary or an enforce one in a
# shadow slot), so a missing module, a broken pattern or an invalid ruleset
# fails the BUILD instead of shipping a crash-looping container. No database
# and no network are needed: ROUTE_POLICY_DB_HOST is unset here, the pool
# opens only in the lifespan, and an llm_classifier evaluator compiles
# without reaching the classifier. The benchmark's modules are imported too,
# so a bench/ that no longer matches the service fails here as well.
RUN uv run --no-sync python -c "\
from route_policy.app import app; \
from route_policy.config import RULESETS_DIR; \
from route_policy.db.migrations import migrations; \
from route_policy.rulesets.loader import load_seed; \
import bench.run, bench.points, bench.report, bench.convert, bench.metrics; \
s = load_seed(RULESETS_DIR); \
print('rulesets:', [f'{r.id}@{r.version} {r.mode} {r.sha256[:12]}' for r in s.compiled]); \
print('bindings:', [f'{b.scope_model} -> {b.ruleset_id}@{b.version} shadow {list(b.shadows)}' for b in s.bindings]); \
print('migrations:', [m.name for m in migrations()])"

WORKDIR /app/ai/route-policy
EXPOSE 8000

CMD ["uv", "run", "--no-sync", "uvicorn", "route_policy.app:app", "--host", "0.0.0.0", "--port", "8000"]
