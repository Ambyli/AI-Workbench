FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY quality-checker/pyproject.toml .
RUN uv sync --no-dev

COPY quality-checker/quality_checker.py .

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "quality_checker:app", "--host", "0.0.0.0", "--port", "8000"]
