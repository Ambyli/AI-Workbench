# Build context is the REPO ROOT (see ai/signals/docker-compose.signals.yml:
#   context: ../..) so the shared workspace package can be copied alongside
# the service. Same pattern as ai/sandbox/runner/Dockerfile.
#
# CPU only: onnxruntime's CPU provider, raw tokenizers, numpy and the
# fastText predict wheel. No torch, no transformers, no CUDA. The models are
# BAKED IN at build time (~1.2 GB under /models) and the container never
# downloads anything (HF_HUB_OFFLINE=1; nothing imports huggingface_hub).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONPATH=/app/ai/signals \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    SIGNALS_MODELS_DIR=/models \
    TOKENIZERS_PARALLELISM=false \
    HF_HUB_OFFLINE=1 \
    # read_only root filesystem at run time: anything that wants a home
    # directory gets the tmpfs.
    HOME=/tmp

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Workspace bootstrap: the root pyproject declares the members; uv.lock
# ships so the image gets the host's resolution, and --frozen fails the
# build if pyproject.toml drifted from it instead of re-resolving.
COPY pyproject.toml uv.lock ./
COPY shared/common ./shared/common
COPY ai/signals/pyproject.toml ./ai/signals/

RUN uv sync --package signals --no-dev --frozen

# The pinned models, in a layer only models.yaml and the fetcher invalidate
# (a code edit must not re-download 1.2 GB). fetch_models.py checks every
# file's sha256 against models.yaml and writes /models/<lane>/NOTICE.txt
# with the licence (and fastText's CC-BY-SA attribution); each Hugging Face
# lane also keeps its model card, README.md, which carries the authors'
# licence declaration.
COPY ai/signals/models.yaml ./ai/signals/
COPY ai/signals/signals/fetch_models.py /tmp/fetch_models.py
RUN python /tmp/fetch_models.py --models-file ai/signals/models.yaml --dest /models \
    && rm /tmp/fetch_models.py

# Service code.
COPY ai/signals/signals ./ai/signals/signals

# The build's guard, so a broken pin or a broken image never ships:
#   * --check re-verifies every file's sha256, loads every model with the
#     default threads, runs one warm-up inference per model and a fixed set
#     of synthetic sentences with known answers (signals.runtime.smoke) --
#     exit 1 on any failure;
#   * the app module imports the way the CMD will (a missing module fails
#     here, not as a crash-looping container).
RUN python -m signals.runtime --check \
    && python -c "from signals.app import app; print('app ok:', [r.path for r in app.routes][:8])"

# Runs as an unprivileged user; /app and /models stay root-owned and
# read-only (the compose file also mounts the root filesystem read_only).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin signals
USER signals

WORKDIR /app/ai/signals
EXPOSE 8000

CMD ["uvicorn", "signals.app:app", "--host", "0.0.0.0", "--port", "8000"]
