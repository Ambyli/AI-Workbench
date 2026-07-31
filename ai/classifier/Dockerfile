# Build context is the REPO ROOT (see ai/docker-compose.classifier.yml:
#   context: ..) so we can copy the shared workspace package alongside the
# service. Same pattern as ai/Dockerfile.interceptor and ai/Dockerfile.roofix.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    PYTHONPATH=/app/ai/classifier

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Workspace bootstrap.
COPY pyproject.toml .
COPY shared/common ./shared/common
COPY ai/classifier/pyproject.toml ./ai/classifier/

RUN uv sync --package classifier --no-dev

# Service code.
COPY ai/classifier/*.py ./ai/classifier/

WORKDIR /app/ai/classifier
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
