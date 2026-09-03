"""Pydantic request/response models + shared validators for the runner.

The constants referenced by these models (payload caps, reserved env
keys, the session-id regex) live here rather than in ``app.py`` so
``models.py`` sits at a leaf in the import graph:

    app.py  ────►  operations.py  ────►  models.py, state.py, ...
           ────►  models.py
           ────►  tool_server.py  ────►  operations.py, models.py

Both ``app.py`` and ``operations.py`` import the constants back from
here for their own logic.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Optional, Union

from pydantic import BaseModel, Field, field_validator


log = logging.getLogger("sandbox-runner.models")

# ── Constants referenced by the models ────────────────────────────────────
# Per-file and total-payload caps for update_files / run / POST /run.
# Enforced at pydantic-validation time so a hostile payload never touches
# the tarball builder. Base64 files count their DECODED length so a client
# can't smuggle a huge blob past the cap by encoding it.
MAX_FILE_BYTES = int(os.environ.get("SANDBOX_MAX_FILE_BYTES", "1000000"))
MAX_PAYLOAD_BYTES = int(os.environ.get("SANDBOX_MAX_PAYLOAD_BYTES", "10000000"))

# Sessions are the durable identity of a preview across turns. Regex is
# both a validation surface (reject anything that could path-inject when
# a caller later uses the id in a URL or filesystem context) and a hint
# to the model that the id is a short opaque string, not free text.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Env vars we refuse to accept from the caller — these are runner-controlled
# invariants (egress proxy, log buffering) and must not be overridable.
# The spawner reapplies them on top anyway; refusing at the request layer
# gives the caller a clear error instead of silently discarding their value.
RESERVED_ENV_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "PYTHONUNBUFFERED", "NPM_CONFIG_LOGLEVEL", "FORCE_COLOR", "TERM",
})


# Type alias for the discriminated file map. On the wire it's:
#   {"path/to/file.txt": "utf-8 text"}
# or
#   {"path/to/image.png": {"encoding": "base64", "content": "iVBOR..."}}
# Pydantic doesn't do discriminated Unions of primitives cleanly, so we
# validate manually in `_validate_files_map`.
FileContent = Union[str, dict]


def _validate_files_map(files: dict) -> dict:
    """Pydantic ``field_validator`` body for the ``files`` map.

    Rejects non-str/non-dict values, enforces per-file and total payload
    size caps. Base64 files are decoded once here so the decoded byte-
    length is what counts against the caps — no bypass by encoding.
    Returns the ORIGINAL map (untouched — the spawner handles decoding
    on the write side) so downstream code sees exactly what the caller
    sent.
    """
    if not isinstance(files, dict):
        raise ValueError("files must be a mapping of path → content")
    total = 0
    per_file_over: list[tuple[str, int]] = []
    for path, value in files.items():
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
        elif isinstance(value, dict):
            encoding = value.get("encoding")
            content = value.get("content", "")
            if encoding == "base64":
                # Length of the decoded payload. Use base64.b64decode's
                # own reject-on-noise mode so a garbage value fails at
                # validation, not deep inside the spawner.
                try:
                    decoded = base64.b64decode(content, validate=True)
                except Exception as exc:
                    raise ValueError(
                        f"invalid base64 in files[{path!r}]: {exc}"
                    ) from exc
                size = len(decoded)
            elif encoding in (None, "utf-8", "utf8", "text"):
                size = len(str(content).encode("utf-8"))
            else:
                raise ValueError(
                    f"unknown encoding {encoding!r} in files[{path!r}]; "
                    "expected 'base64' or 'utf-8'"
                )
        else:
            raise ValueError(
                f"files[{path!r}] must be str or "
                f"{{encoding, content}} dict, not {type(value).__name__}"
            )
        if size > MAX_FILE_BYTES:
            per_file_over.append((path, size))
        total += size
    if per_file_over or total > MAX_PAYLOAD_BYTES:
        parts = []
        parts.append(
            f"Rejected: files payload exceeded limits."
        )
        parts.append(
            f"  Individual file cap: {MAX_FILE_BYTES:,} bytes"
        )
        if per_file_over:
            biggest_path, biggest_size = max(per_file_over, key=lambda t: t[1])
            parts.append(
                f"  (largest oversized: {biggest_path!r} at {biggest_size:,} bytes; "
                f"{len(per_file_over)} file(s) over the cap)"
            )
        parts.append(
            f"  Total payload cap: {MAX_PAYLOAD_BYTES:,} bytes "
            f"(submitted: {total:,} bytes)"
        )
        parts.append(
            "Shrink files, drop non-essential assets, or split across "
            "multiple update_files calls."
        )
        raise ValueError("\n".join(parts))
    return files


def _validate_env(env: Optional[dict]) -> Optional[dict[str, str]]:
    """Reject non-string values and reserved keys. Kept in one place so
    ``create``, ``run``, and ``POST /run`` all enforce the same rules."""
    if env is None:
        return None
    if not isinstance(env, dict):
        raise ValueError("env must be a mapping of str → str")
    out: dict[str, str] = {}
    for k, v in env.items():
        if not isinstance(k, str):
            raise ValueError(f"env key must be str, got {type(k).__name__}")
        if k in RESERVED_ENV_KEYS:
            raise ValueError(
                f"env key {k!r} is reserved by the runner and cannot be set "
                "by the caller (proxy + buffering invariants)"
            )
        if not isinstance(v, (str, int, float, bool)):
            raise ValueError(
                f"env value for {k!r} must be a scalar, got {type(v).__name__}"
            )
        out[k] = str(v)
    return out


class RunRequest(BaseModel):
    runtime: str = Field(description="One of the keys in runtimes.RUNTIMES")
    files: dict[str, FileContent] = Field(
        default_factory=dict,
        description=(
            "Path → content map. On the first call for a session this is "
            "the initial file set. On a follow-up call (same session_id) "
            "it is an overlay — paths given here overwrite files in the "
            "running container, unlisted files are left alone. Values are "
            "either raw UTF-8 strings, or {\"encoding\": \"base64\", "
            "\"content\": \"...\"} for binary payloads (images, PDFs, "
            "wheels). Per-file cap: SANDBOX_MAX_FILE_BYTES; total cap: "
            "SANDBOX_MAX_PAYLOAD_BYTES."
        ),
    )
    entrypoint: Optional[str] = Field(
        default=None,
        description="Shell command inside the sandbox; must bind to port 80",
    )
    ttl_seconds: Optional[int] = Field(
        default=None,
        description="Idle TTL. Server clamps to SANDBOX_HARD_TTL_SECONDS.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Persistent identifier for a preview across turns. Omit on "
            "the first call; the server generates one and returns it. "
            "Pass the same value back on follow-up calls to update files "
            "in place — no respawn, same URL, dev server hot-reloads."
        ),
    )
    deletes: list[str] = Field(
        default_factory=list,
        description=(
            "Relative paths (under /app) to delete on a follow-up call. "
            "Ignored on the first call. Absolute paths and '..' are "
            "rejected."
        ),
    )
    env: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "Process env vars set inside the container. Immutable after "
            "spawn (respawned self-heal replays the same env). Reserved "
            "keys HTTP_PROXY/HTTPS_PROXY/PYTHONUNBUFFERED/TERM/etc. are "
            "rejected — those are runner-controlled invariants."
        ),
    )
    recreate_if_gone: bool = Field(
        default=True,
        description=(
            "Backward-compat default for POST /run: silently respawn if "
            "the session's container is gone. Set false to require the "
            "caller to reason about self-heal explicitly."
        ),
    )

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not SESSION_ID_RE.match(v):
            raise ValueError(
                "session_id must match ^[A-Za-z0-9_-]{1,64}$"
            )
        return v

    @field_validator("files")
    @classmethod
    def _validate_files(cls, v: dict) -> dict:
        return _validate_files_map(v)

    @field_validator("env")
    @classmethod
    def _validate_env_field(cls, v: Optional[dict]) -> Optional[dict[str, str]]:
        return _validate_env(v)


class RunResponse(BaseModel):
    sandbox_id: str
    session_id: str
    url: str
    expires_at: str
    reused: bool = False
    # Present on every successful spawn/update. `runtime` reflects the
    # runtime originally spawned under this session (needed by clients
    # that want to render startup output correctly per runtime).
    # `startup_output` is a tail of /tmp/sandbox.log — empty when the
    # container just spawned and has not printed anything yet.
    runtime: Optional[str] = None
    startup_output: str = ""
    # Health probe result, populated by the update path. Present on
    # every response so callers have a consistent shape; the fresh-spawn
    # response inlines the readiness probe result.
    app_status: Optional[dict] = None
    # Self-heal notice. Populated when the update path had to respawn
    # a container that was gone. None on the plain reuse or fresh spawn
    # paths.
    recreated: bool = False


class UpdateFilesRequest(BaseModel):
    files: dict[str, FileContent] = Field(
        default_factory=dict,
        description=(
            "Path → content overlay. Absent paths are preserved. Values "
            "are str (UTF-8) or {encoding: 'base64', content: '...'}."
        ),
    )
    deletes: list[str] = Field(
        default_factory=list,
        description="Relative paths under /app to delete.",
    )
    recreate_if_gone: bool = Field(
        default=False,
        description=(
            "If true and the session's container is gone, respawn a fresh "
            "container under the same session_id (files and packages "
            "installed via exec are LOST; env is preserved). Default "
            "false — caller must opt-in explicitly."
        ),
    )

    @field_validator("files")
    @classmethod
    def _validate_files(cls, v: dict) -> dict:
        return _validate_files_map(v)


class CreateRequest(BaseModel):
    runtime: str
    entrypoint: Optional[str] = None
    ttl_seconds: Optional[int] = None
    env: Optional[dict[str, str]] = None

    @field_validator("env")
    @classmethod
    def _validate_env_field(cls, v: Optional[dict]) -> Optional[dict[str, str]]:
        return _validate_env(v)


class ExecRequest(BaseModel):
    command: str = Field(description="Shell command (sh -c). Non-interactive.")
    timeout_seconds: Optional[int] = Field(default=None)
    working_dir: Optional[str] = Field(default=None)


class PatchSpec(BaseModel):
    """One hunk in a ``patch_files`` call — line-range strict replacement."""

    path: str = Field(
        description=(
            "Relative path under /app. Same validation as `deletes` — "
            "absolute paths and '..' rejected."
        ),
    )
    start_line: int = Field(
        description="1-indexed, inclusive start of the range to replace.",
        ge=1,
    )
    end_line: int = Field(
        description="1-indexed, inclusive end of the range to replace.",
        ge=1,
    )
    expected: str = Field(
        description=(
            "The EXACT current content of lines [start_line, end_line] "
            "joined with '\\n'. Byte-for-byte match required — no "
            "whitespace or trailing-newline lenience. If your view is "
            "stale, call get_files first."
        ),
    )
    replacement: str = Field(
        description=(
            "What to write in place of `expected`. May be shorter, "
            "longer, or the same length."
        ),
    )
    note: str = Field(
        default="",
        description=(
            "Optional free-text note logged for operator debugging. "
            "Not applied to the file."
        ),
    )


class PatchFilesRequest(BaseModel):
    patches: list[PatchSpec] = Field(
        description=(
            "List of patches. All-or-nothing: every patch validates first "
            "(byte-for-byte + no overlap on the same file); only then are "
            "any files modified."
        ),
    )
    recreate_if_gone: bool = Field(
        default=False,
        description=(
            "Accepted for interface consistency but has NO effect on this "
            "tool. patch_files depends on file content that would not "
            "exist in a fresh container, so a dead container always "
            "returns 409 regardless. Call update_files first to establish "
            "file state before reissuing patches."
        ),
    )

    @field_validator("patches")
    @classmethod
    def _validate_patches(cls, v: list[PatchSpec]) -> list[PatchSpec]:
        if not v:
            raise ValueError("patches must contain at least one patch")
        total = 0
        per_patch_over: list[tuple[int, str, int]] = []
        for i, p in enumerate(v):
            if p.end_line < p.start_line:
                raise ValueError(
                    f"patches[{i}]: end_line ({p.end_line}) must be >= "
                    f"start_line ({p.start_line})"
                )
            # Each patch contributes both expected and replacement to the
            # payload budget. Kept symmetric with `files`: bytes are UTF-8.
            expected_bytes = len(p.expected.encode("utf-8"))
            replacement_bytes = len(p.replacement.encode("utf-8"))
            patch_size = expected_bytes + replacement_bytes
            if patch_size > MAX_FILE_BYTES:
                per_patch_over.append((i, p.path, patch_size))
            total += patch_size
        if per_patch_over or total > MAX_PAYLOAD_BYTES:
            parts = ["Rejected: patch_files payload exceeded limits."]
            parts.append(f"  Individual patch cap: {MAX_FILE_BYTES:,} bytes")
            if per_patch_over:
                biggest = max(per_patch_over, key=lambda t: t[2])
                parts.append(
                    f"  (largest oversized: patch #{biggest[0] + 1} on "
                    f"{biggest[1]!r} at {biggest[2]:,} bytes; "
                    f"{len(per_patch_over)} patch(es) over the cap)"
                )
            parts.append(
                f"  Total payload cap: {MAX_PAYLOAD_BYTES:,} bytes "
                f"(submitted: {total:,} bytes)"
            )
            parts.append(
                "Shrink or split patches into multiple patch_files calls."
            )
            raise ValueError("\n".join(parts))
        return v


class ToolRunRequest(BaseModel):
    """Request shape for the OpenWebUI Tool Server ``/tool/run`` route.

    Kept separate from ``RunRequest`` because the Tool Server surface has
    slightly different defaults (no ``recreate_if_gone`` — the OpenWebUI
    path always self-heals) and doesn't need the same field descriptions.
    """

    runtime: str = Field(
        description="One of 'static', 'python', 'node'."
    )
    files: dict[str, FileContent] = Field(
        default_factory=dict,
        description=(
            "Map of relative path → content. Values are UTF-8 str or "
            "{encoding:'base64', content:'...'} for binary. On follow-up "
            "with the same session_id, only send changed files."
        ),
    )
    entrypoint: Optional[str] = Field(default=None)
    ttl_seconds: Optional[int] = Field(default=None)
    session_id: Optional[str] = Field(default=None)
    deletes: list[str] = Field(default_factory=list)
    env: Optional[dict[str, str]] = Field(default=None)

    @field_validator("files")
    @classmethod
    def _validate_files(cls, v: dict) -> dict:
        return _validate_files_map(v)

    @field_validator("env")
    @classmethod
    def _validate_env_field(cls, v: Optional[dict]) -> Optional[dict[str, str]]:
        return _validate_env(v)
