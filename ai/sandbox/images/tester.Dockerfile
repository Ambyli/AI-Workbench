# Base image for the sandbox-tester companion container.
#
# The runner spawns ONE of these on `sandbox_net` per preview_app call
# (after readiness passes, before the response returns) to execute the
# model-supplied behavioral tests against the live preview URL. Same
# security posture as the sandbox itself: cap_drop=ALL, non-root
# 1000:1000, on sandbox_net only. No egress — tests hit
# `http://sandbox-{id}:80/` by Docker DNS.
#
# What's baked in:
#   * python 3.11 (from the base image) — pytest, requests, playwright
#     + chromium browser binary (playwright install --with-deps).
#   * node 20 + npm — jest, vitest, playwright (JS binding).
#   * curl, jq, coreutils — for the static runtime's shell-based tests
#     and for models that prefer a plain "curl | grep" contract.
#
# Built by:   docker compose -f ai/sandbox/docker-compose.sandbox.yml --profile build build
# Tag:        sandbox-tester:latest  (referenced from spawner.spawn_tester)

FROM python:3.11-slim

# System packages: node + npm for JS test runners; curl + jq + ca-certs
# for shell tests and Playwright download step; the Playwright chromium
# `--with-deps` install below needs a handful of shared libs already
# available in bookworm-slim's default set, so no extra apt work here.
# Node 20 is the LTS closest to the sandbox-node runtime; keep the
# major version aligned so the tester's Playwright JS matches what a
# model would write for the app it just spawned.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates jq gnupg \
        # Playwright chromium's runtime deps that aren't in slim by default.
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
        libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 && \
    # Node 20 via NodeSource. Bookworm-slim's node in main is 18 which
    # doesn't ship with the fetch API tests would want by default.
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Python test tooling. Playwright pulls the chromium binary at install
# time (`playwright install chromium`); --with-deps is the flag that
# would install extra apt packages if any were missing — safe to run
# again since the apt install above covered them.
RUN pip install --no-cache-dir \
        pytest==8.3.3 \
        pytest-timeout==2.3.1 \
        requests==2.32.3 \
        playwright==1.47.0 \
        jsonschema==4.23.0 && \
    playwright install chromium

# Node test tooling. Global install so `npx --yes jest` finds them
# without pulling from the network — the tester container is on
# sandbox_net only and has no egress.
#
# playwright (the JS bindings package) reads $PLAYWRIGHT_BROWSERS_PATH
# to find browser binaries; point it at the python-playwright install
# location so we don't download chromium twice.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright && \
    npm install -g \
        jest@29.7.0 \
        vitest@2.1.1 \
        playwright@1.47.0 \
        @playwright/test@1.47.0 && \
    # The npx-based test_command in runtimes.py points at /home/tester
    # for jest's config; write a minimal config that runs .test.js /
    # .test.ts under /tests without pretending to be an ES module.
    mkdir -p /home/tester && \
    printf '{\n  "testEnvironment": "node",\n  "testMatch": ["**/*.test.js", "**/*.test.ts"]\n}\n' \
        > /home/tester/jest.config.json && \
    chown -R 1000:1000 /home/tester /ms-playwright

# Non-root user matching the runner's --user 1000:1000. HOME must be
# writable because pytest / npx write cache dirs under it and node's
# playwright package looks up $HOME for its config lookup.
RUN useradd --uid 1000 --user-group --create-home --home-dir /home/sandbox tester && \
    chown -R 1000:1000 /home/sandbox

USER 1000:1000
ENV HOME=/home/sandbox \
    PATH=/home/sandbox/.local/bin:/usr/local/bin:/usr/bin:/bin

# /tests is where the runner injects the model-supplied test files via
# put_archive. It exists in the image so the mount point is present
# even if the tarball is empty (which the runner shouldn't send, but
# the entrypoint contract stays clean either way).
WORKDIR /tests

# Entrypoint is the runner-supplied test_command from runtimes.py —
# there is no wrapper script. The runner sets `command=[...]` on
# container.create and this ENTRYPOINT is bypassed. Kept here so
# `docker run --rm sandbox-tester:latest` still gives an operator
# something sensible for ad-hoc smoke checks.
ENTRYPOINT ["/bin/sh", "-c"]
CMD ["python -c 'import pytest, playwright, requests; print(\"tester ready\")'"]
