#!/usr/bin/env python3
"""One-shot preparation for the semantic-router stack (vllm-sr-models-init).

Runs to completion and exits 0, or fails the whole stack before the router
boots. Three jobs, in order:

  1. Validate /work/config.yaml with the pinned CLI's own schema.
  2. Re-render the Envoy config from it and prove the checked-in
     /work/envoy.yaml still matches, byte for byte below its header.
  3. Substitute the listener bearer token into the render and publish it at
     /out/envoy.yaml, which is what Envoy actually loads.

Plus: make sure /app/models exists so the router's Hugging Face download has
somewhere to land. vllm-sr 0.3.0 has no model-download subcommand -- see the
comments in docker-compose.semantic-router.yml.

Everything here talks to vllm-sr's own modules rather than reimplementing
them, so a version bump that changes the template or the schema surfaces as
a failure here instead of as a subtly wrong Envoy config in production.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG_IN = Path("/work/config.yaml")
ENVOY_IN = Path("/work/envoy.yaml")
ENVOY_OUT = Path("/out/envoy.yaml")
MODELS_DIR = Path("/app/models")

# The checked-in envoy.yaml is generator output with a hand-written comment
# header prepended. Everything from the first `admin:` line onward is the
# part that must match the render exactly.
BODY_MARKER = "admin:"

# Placeholders that stand in for the real bearer tokens in both config.yaml
# and envoy.yaml, so neither ever carries a secret into git. Two, because
# the Envoy Lua filter must accept BOTH the key LiteLLM's `auto` alias sends
# AND the LiteLLM virtual key the router's looper client puts on its own
# sub-requests (pkg/looper/client.go sends `Bearer <model access key>`, not
# the listener key) -- see the WHY TWO KEYS comment in config.yaml.
KEY_PLACEHOLDERS = {
    "__SEMANTIC_ROUTER_LISTENER_KEY__": "SEMANTIC_ROUTER_LISTENER_KEY",
    "__SEMANTIC_ROUTER_LITELLM_KEY__": "SEMANTIC_ROUTER_LITELLM_KEY",
}


def fail(message: str) -> None:
    print(f"[models-init] FATAL: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def info(message: str) -> None:
    print(f"[models-init] {message}", flush=True)


def validate_config() -> None:
    """Step 1 -- `vllm-sr validate`, the release's own schema check.

    Note the subcommand: `vllm-sr validate --config <path>`. There is no
    `vllm-sr config validate` in 0.3.0; `vllm-sr config` only has envoy /
    router / migrate / import.

    Invoked as `python -m cli.main` rather than the `vllm-sr` console
    script so it does not depend on PATH -- same code either way
    (entry_points.txt: vllm-sr = cli.main:main).
    """
    info(f"validating {CONFIG_IN} with the pinned vllm-sr CLI")
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "validate", "--config", str(CONFIG_IN)],
        check=False,
    )
    if result.returncode != 0:
        fail(
            "config.yaml failed schema validation (see the CLI output above). "
            "Every field must exist in this release's pydantic models -- "
            "AlgorithmConfig in particular is extra=forbid, so an algorithm "
            "block this version does not ship is a hard error, not an "
            "ignored key."
        )


def render_envoy() -> str:
    """Step 2a -- render the Envoy config with the release's own generator."""
    from cli.config_generator import generate_envoy_config_from_user_config
    from cli.parser import parse_user_config

    info("rendering envoy config from config.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "envoy.yaml"
        generate_envoy_config_from_user_config(parse_user_config(str(CONFIG_IN)), str(out))
        return out.read_text(encoding="utf-8")


def checked_in_body() -> str:
    """The checked-in envoy.yaml with its comment header stripped."""
    text = ENVOY_IN.read_text(encoding="utf-8")
    index = text.find(f"\n{BODY_MARKER}")
    if text.startswith(BODY_MARKER):
        return text
    if index == -1:
        fail(
            f"{ENVOY_IN} has no `{BODY_MARKER}` line -- it does not look like "
            "generator output. Re-render it, do not hand-write it."
        )
    return text[index + 1 :]


def normalise(text: str) -> list[str]:
    """Compare on content, not on line endings.

    .gitattributes forces LF on *.yaml, but a checkout on a host with an
    odd core.autocrlf, or an editor that rewrites the file, should not fail
    the stack over invisible bytes. Trailing whitespace is stripped for the
    same reason -- the Jinja template emits plenty of it.
    """
    return [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]


def compare(rendered: str) -> None:
    """Step 2b -- fail loudly if the checked-in render is stale."""
    expected = normalise(checked_in_body())
    actual = normalise(rendered)
    if expected == actual:
        info("checked-in envoy.yaml matches the render")
        return

    diff = difflib.unified_diff(
        expected, actual, fromfile="repo ai/semantic-router/envoy.yaml",
        tofile="freshly rendered from config.yaml", lineterm="",
    )
    print("\n".join(diff), file=sys.stderr, flush=True)
    fail(
        "ai/semantic-router/envoy.yaml is out of date with config.yaml and/or "
        "this vllm-sr release. Envoy config is GENERATED upstream -- re-render "
        "it (SEMANTIC_ROUTER.md > Re-rendering envoy.yaml) and commit the "
        "result. Never hand-edit it, and never work around this check."
    )


def publish(rendered: str) -> None:
    """Step 3 -- substitute the real bearer tokens and publish for Envoy."""
    out = rendered
    for placeholder, env_name in KEY_PLACEHOLDERS.items():
        key = os.environ.get(env_name, "")
        if not key:
            fail(f"{env_name} is empty")
        if placeholder not in out:
            fail(
                f"{placeholder} is not in the rendered Envoy config. "
                "listeners[0].api_keys in config.yaml must carry exactly that "
                "placeholder -- without it Envoy rejects either every caller "
                "or every looper sub-request (see WHY TWO KEYS in config.yaml)."
            )
        out = out.replace(placeholder, key)

    ENVOY_OUT.parent.mkdir(parents=True, exist_ok=True)
    ENVOY_OUT.write_text(out, encoding="utf-8")
    info(f"wrote {ENVOY_OUT} with {len(KEY_PLACEHOLDERS)} bearer keys substituted")


def seed_models_dir() -> None:
    """The router downloads into here on first start; just make it exist."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    info(f"{MODELS_DIR} ready for the router's Hugging Face download")


def main() -> None:
    for path in (CONFIG_IN, ENVOY_IN):
        if not path.is_file():
            fail(f"{path} is missing -- check the bind mounts in the compose file")

    validate_config()
    rendered = render_envoy()
    compare(rendered)
    publish(rendered)
    seed_models_dir()
    info("done")


if __name__ == "__main__":
    main()
