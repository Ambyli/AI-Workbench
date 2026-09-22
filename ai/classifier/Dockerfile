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

# Service code.
COPY ai/classifier/*.py ./ai/classifier/

WORKDIR /app/ai/classifier
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
