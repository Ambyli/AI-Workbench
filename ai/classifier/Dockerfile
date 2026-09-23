# Build context is the REPO ROOT (see ai/classifier/docker-compose.classifier.yml:
#   context: ../..) so we can copy the shared workspace package alongside the
# service. Same pattern as ai/interceptor/Dockerfile.interceptor and ai/roofix/Dockerfile.roofix.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    PYTHONPATH=/app/ai/classifier

# Native library the document stack needs on a slim base:
#   libgomp1 — OpenMP runtime required by onnxruntime (rapidocr's engine).
# No libGL: rapidocr declares the GUI `opencv-python`, but the workspace root
# pyproject.toml overrides that dependency away (tool.uv.override-dependencies)
# so only the headless OpenCV wheel this service already uses is installed.
# Both wheels write the same `cv2/` directory, so letting both in would mean
# whichever uninstalls first takes the other's files with it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Workspace bootstrap.
COPY pyproject.toml .
COPY shared/common ./shared/common
COPY ai/classifier/pyproject.toml ./ai/classifier/

RUN uv sync --package classifier --no-dev

# Pre-warm the OCR models. RapidOCR downloads PP-OCRv6 det/rec + the angle
# classifier into its own package directory on first construction; baking them
# in means the container never reaches the network at request time (and a job
# never pays a multi-second download on a cold start).
# The second command fails the build if the download silently produced no
# models, so a broken warm-up can never ship as a runtime surprise.
RUN uv run --no-dev --package classifier python -c "from rapidocr import RapidOCR; RapidOCR()" \
    && uv run --no-dev --package classifier python -c "import pathlib, rapidocr; m=list((pathlib.Path(rapidocr.__file__).parent / 'models').glob('*.onnx')); assert m, 'OCR warm-up produced no .onnx models'; print('OCR models baked in:', [f.name for f in m])"

# Service code: the six top-level modules, then one line per subpackage.
#
# One COPY per subpackage rather than `COPY ai/classifier/ ./ai/classifier/`
# plus a repo-root .dockerignore: there is no .dockerignore in this repo today,
# and every service under ai/ builds with `context: ../..`, so adding one would
# silently change the build context of fifteen other images to fix one. Listing
# the packages instead ships exactly the Python the service imports — no *.md,
# no postman collection, no compose file, no host __pycache__ — and touches
# nothing outside this file.
#
# ADDING A SUBPACKAGE MEANS ADDING A LINE HERE. The import smoke below is what
# makes forgetting it a build failure rather than a crash-looping container.
COPY ai/classifier/*.py ./ai/classifier/
COPY ai/classifier/analysis ./ai/classifier/analysis
COPY ai/classifier/api ./ai/classifier/api
COPY ai/classifier/compare ./ai/classifier/compare
COPY ai/classifier/cv ./ai/classifier/cv
COPY ai/classifier/detector ./ai/classifier/detector
COPY ai/classifier/jobs ./ai/classifier/jobs
COPY ai/classifier/llm ./ai/classifier/llm
COPY ai/classifier/regions ./ai/classifier/regions

# The operator tools. `bin/` is not imported by the service — it holds scripts
# that have to run WHERE THE MODEL IS (grounding_experiment.py talks to
# VISION_LLM_API directly), so they ship in the image and are run with
# `docker compose exec classifier python bin/…`.
COPY ai/classifier/bin ./ai/classifier/bin

# Import the app the way the CMD below will (PYTHONPATH is already
# /app/ai/classifier), so a missing COPY line above, a circular import, or a
# syntax error fails the BUILD instead of shipping a crash-looping container.
# Same spirit as the OCR warm-up assertion. The paths are throwaway: config.py
# freezes DB_PATH / PAYLOAD_DIR / CLASSIFIER_ARTIFACT_DIR at import time, and
# /data is a volume mount at runtime.
RUN DB_PATH=/tmp/build-check.db \
    PAYLOAD_DIR=/tmp/build-check-payloads \
    CLASSIFIER_ARTIFACT_DIR=/tmp/build-check-artifacts \
    CLASSIFIER_OCR_ENGINE=none \
    uv run --no-dev --package classifier python -c "import main; print('import main: ok')"

WORKDIR /app/ai/classifier
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
