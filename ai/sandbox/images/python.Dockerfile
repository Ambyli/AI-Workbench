# Base image for runtime="python" sandboxes — Python 3.11-slim with the
# common web-app frameworks pre-baked so first-render latency is ~2s in
# the common case. If the caller ships requirements.txt, the entrypoint
# runs `pip install -r requirements.txt` on boot via sandbox-egress; this
# takes 20-90s but subsequent HTTP requests are hot.
#
# Built by:   docker compose -f ai/sandbox/docker-compose.sandbox.yml --profile build build
# Tag:        sandbox-python:latest  (referenced from runtimes.py)

FROM python:3.11-slim

# The base package set — matches what runtimes.py's docstring promises.
# Keep this list in sync with ai/sandbox/SANDBOX.md § "Pre-baked Python packages"
# so a model relying on the docs isn't surprised.
#
# Reasoning for what's pre-baked vs left to requirements.txt:
#   * streamlit, gradio     — the two most common "make me an interactive
#                             preview" frameworks. Both start in <2s.
#   * flask, fastapi        — common enough that not pre-baking would make
#                             the first-render sound broken.
#   * uvicorn               — fastapi's server; skipping it means the model
#                             would have to add it to requirements.txt for
#                             every fastapi demo.
#   * pandas, numpy         — needed by ~every streamlit demo.
#   * matplotlib, plotly    — same, for the charts.
#   * requests, pillow      — misc common.
RUN pip install --no-cache-dir \
    streamlit==1.36.0 \
    gradio==4.44.0 \
    flask==3.0.3 \
    fastapi==0.115.0 \
    uvicorn==0.30.0 \
    pandas==2.2.2 \
    numpy==1.26.4 \
    matplotlib==3.9.2 \
    plotly==5.24.1 \
    requests==2.32.3 \
    pillow==10.4.0

# Entrypoint wrapper — if the caller included requirements.txt, install
# it before running their command. Uses --no-cache-dir so we don't fill
# the read-only rootfs's overlay with pip's cache junk.
COPY <<'ENTRY' /usr/local/bin/sandbox-entrypoint.sh
#!/bin/sh
set -e
cd /app
if [ -f requirements.txt ]; then
    echo "[sandbox] installing requirements.txt via HTTP_PROXY=${HTTP_PROXY}"
    pip install --no-cache-dir --user -r requirements.txt || {
        echo "[sandbox] pip install failed — is the package on the allowlist?" >&2
        exit 1
    }
fi
exec "$@"
ENTRY

RUN chmod +x /usr/local/bin/sandbox-entrypoint.sh

# Non-root user matching what the runner passes as --user 1000:1000.
# HOME must be writable for `pip install --user` above to land somewhere
# valid on a read-only rootfs; /tmp is the tmpfs the runner mounts.
RUN useradd --uid 1000 --user-group --create-home --home-dir /home/sandbox sandbox

USER 1000:1000
ENV HOME=/home/sandbox \
    PATH=/home/sandbox/.local/bin:/usr/local/bin:/usr/bin:/bin

WORKDIR /app

# Not strictly needed (runner passes /bin/sh -c "..."), but keeps the
# image usable for ad-hoc `docker run` sanity checks.
ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint.sh"]
EXPOSE 80
