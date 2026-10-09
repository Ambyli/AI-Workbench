# Production Dockerfile for the Zeo Energy Service Chatbot API.
FROM python:3.11-slim AS base

# No .pyc files, unbuffered logs. Runtime data lives on a volume at /app/data (S-M6).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATA_DIR=/app/data \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only (tests/docs/tooling are excluded via .dockerignore — S-M7).
# frontend/ is the internal staff/demo portal; it is only served when ENABLE_STAFF_API=true.
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Least privilege: code stays root-owned (read-only to the app user); only the data
# volume is writable.
RUN useradd -u 1000 -m appuser && \
    mkdir -p /app/data/uploads && \
    chown -R appuser:appuser /app/data

USER appuser
VOLUME ["/app/data"]
EXPOSE 8000

# LIVENESS, not readiness: the process answers (200 = ready, 503 = model required but
# down). A 503 must not mark the container unhealthy — the website then falls back to
# the classic form instead of the whole page disappearing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/health); \
        [ "$code" = "200" ] || [ "$code" = "503" ] || exit 1

# --proxy-headers: take the real client IP from X-Forwarded-For, but ONLY when the
# request comes from an address in $FORWARDED_ALLOW_IPS (the nginx proxy) — S-H2.
# Single worker: sessions, queue and rate-limit state are in-process memory.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "1"]
