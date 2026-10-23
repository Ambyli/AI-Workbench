"""The model registry: ``models.yaml`` → the models a job may select.

Every vision-model (or text-model) request the classifier makes is addressed
to ONE registry entry — its URL, key, timeout, concurrency limit, token
budgets, reasoning setting and request-body extras all come from it. The
registry is a checked-in YAML file (``ai/classifier/models.yaml``,
bind-mounted read-only over the same path in the container), validated here:

    load_registry()   read + validate a file → ``Registry``. Raises
                      ``RegistryError`` for anything that must stop the
                      container: invalid YAML, an unknown key, an unknown
                      ``default``, an ``api_key_env`` on a plain-``http://``
                      URL to a dotted host, a reasoning key set twice, a
                      "Reasoning strength" system line on Muse Glimmer, an
                      unavailable default.
    get_registry()    the process-wide registry, loaded on first use from
                      ``DEFAULT_PATH``. Every caller looks it up at CALL time,
                      so ``use_registry()`` (tests) swaps it everywhere.
    startup_check()   what ``main``'s lifespan runs first: load (so a bad file
                      fails startup with its reason), log the entries, and
                      WARN once about any removed env var still set.
    Registry          the entries by id, the default, ``resolve(id)``,
                      ``check_selectable(id)`` (the 400 messages) and
                      ``public()`` (the GET /models payload — never a key).
    ModelSpec         one loaded entry: everything a prompt builder and the
                      transport need, plus its own ``ConcurrencyLimit``.

**Keys never live in the YAML.** An entry names an environment variable
(``api_key_env``); its value is read once, at load, into the spec's request
headers and nowhere else — ``public()`` reports only whether the entry is
authenticated. A named variable that is unset or empty does not stop the
container: the entry loads with ``available: false`` and a reason, and a
request that selects it is refused at submit. Only an unavailable DEFAULT is
fatal, because every request without a ``model`` would fail.

**Confidence is configuration too.** Each entry's ``confidence:`` block
(``ConfidenceConfig``) says how a scoring call's confidence is MEASURED —
``logprobs`` (the score token's distribution), ``sampling`` (n answers) or
``stated`` (the model's own number; the default, safe on any provider) — and
whether the headline ``confidence`` is that stated number or a fitted
calibration. No code branches on a model's name: every provider difference
(the request keys, where the response keeps the entries, how its tokenizer
writes numbers, how many samples) is a knob with a default. Load refuses a
``headline: calibrated`` without points, a calibration ``signal`` the method
cannot produce, a reserved body key in ``logprobs.request`` /
``sampling.extra_body``, and ``sampling`` via ``n_param`` with ``n`` above
``max_concurrent`` (one such call holds n slots). See ``llm.confidence``.

**Replaced settings.** The registry replaced ``VISION_LLM_API``,
``VISION_LLM_MODEL``, ``VISION_LLM_REASONING_STRENGTH`` /
``CLASSIFIER_REASONING_STRENGTH``, ``VISION_LLM_MAX_TOKENS``,
``VISION_LLM_MAX_IMAGES_PER_PROMPT``, ``CLASSIFIER_MAX_LLM_CALLS``,
``CLASSIFIER_HTTP_TIMEOUT_S``, ``CLASSIFIER_LLM_BBOX_MAX_TOKENS``,
``CLASSIFIER_REFERENCE_SELECT_MAX_TOKENS`` and ``CLASSIFIER_TEXT_CHAR_BUDGET``.
They are not read any more — there is no fallback — and ``startup_check``
logs one WARNING naming whichever are still set, so a stale ``.env`` is
visible rather than silently ignored.

Process flow position: a leaf of the ``llm`` package — it imports no other
classifier module but ``logger``, so ``config``-level code (``db``'s pool
size) can ask it for the total concurrency without an import cycle.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from common.jobs.limits import ConcurrencyLimit

from logger import logger

# The checked-in registry. The container bind-mounts the box's copy over this
# exact path (/app/ai/classifier/models.yaml), so code, tests and the
# container all read "the file next to the package".
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "models.yaml"

# Env vars the registry replaced. Not read; named in one startup WARNING when
# any is still set (see the module docstring).
REMOVED_ENV: tuple[str, ...] = (
    "VISION_LLM_API",
    "VISION_LLM_MODEL",
    "VISION_LLM_REASONING_STRENGTH",
    "CLASSIFIER_REASONING_STRENGTH",
    "VISION_LLM_MAX_TOKENS",
    "VISION_LLM_MAX_IMAGES_PER_PROMPT",
    "CLASSIFIER_MAX_LLM_CALLS",
    "CLASSIFIER_HTTP_TIMEOUT_S",
    "CLASSIFIER_LLM_BBOX_MAX_TOKENS",
    "CLASSIFIER_REFERENCE_SELECT_MAX_TOKENS",
    "CLASSIFIER_TEXT_CHAR_BUDGET",
)

# The call kinds a budget is set per. "box" covers all three box-loop calls
# (ask, refine, verify) — they answer ~60 tokens of JSON each.
TOKEN_KINDS: tuple[str, ...] = ("score", "box", "select", "describe")

# Body keys the prompt builders own. extra_body may not set them: a silent
# override of the model name or the token budget is exactly the kind of drift
# a validated file is for.
_RESERVED_BODY_KEYS = frozenset({
    "model", "messages", "max_tokens", "max_completion_tokens", "response_format",
})

# chat_template_kwargs keys that set a reasoning depth (Muse Glimmer's, the
# qwen family's). A system_line model may not also send one of these.
_REASONING_KWARGS = frozenset({"reasoning_strength", "reasoning_effort"})

# A model id is a metrics label value and a URL-ish token in error messages.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RegistryError(RuntimeError):
    """``models.yaml`` cannot be served — the container must not start."""


# ---------------------------------------------------------------------------
# The file's schema
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReasoningConfig(_Strict):
    """How an entry's reasoning depth reaches the model."""

    style: Literal["chat_template_kwargs", "system_line", "none"] = "none"
    key: Optional[str] = Field(
        default=None, description="The chat_template_kwargs variable (chat_template_kwargs only)."
    )
    value: Optional[str] = Field(
        default=None,
        description="chat_template_kwargs: the variable's value. system_line: the whole line.",
    )

    @model_validator(mode="after")
    def _shape(self) -> "ReasoningConfig":
        if self.style == "chat_template_kwargs":
            if not self.key or not self.value:
                raise ValueError("style chat_template_kwargs needs both `key` and `value`")
        elif self.style == "system_line":
            if self.key is not None:
                raise ValueError("style system_line takes only `value` (the whole line), no `key`")
            if not self.value or not self.value.strip():
                raise ValueError("style system_line needs `value`: the line to append")
        elif self.key is not None or self.value is not None:
            raise ValueError("style none takes no `key` / `value`")
        return self


class MaxTokens(_Strict):
    """The completion budget per call kind."""

    score: int = Field(ge=16)
    box: int = Field(ge=16)
    select: int = Field(ge=16)
    describe: int = Field(ge=16)


# ── confidence: how an entry's confidence signal is measured ─────────────
#
# Per-model CONFIGURATION, never per-model code: token probabilities are not
# universal (Claude and OpenAI reasoning models have none; some providers
# ignore the parameter, cap it, or 400 on it), so each entry declares how its
# signal is acquired and every knob has a default. An entry without the block
# is `method: stated` — nothing extra is requested, which is safe on any
# provider. What each method produces is `llm.confidence`'s job; the request
# body changes (score calls only) are `ModelSpec.body`'s.

CONFIDENCE_METHODS: tuple[str, ...] = ("logprobs", "sampling", "stated")

# The calibration signals each method produces — a calibration's `signal`
# must be one its entry's method can feed (llm.confidence computes them):
#   stated               the model's own 0-100 number / 100
#   verdict_probability  the probability mass on the reported verdict's band
#   certainty            1 - entropy / log2(10) of the score distribution
#   agreement            the share of samples with the majority verdict
SIGNALS_BY_METHOD: dict[str, tuple[str, ...]] = {
    "stated": ("stated",),
    "logprobs": ("stated", "verdict_probability", "certainty"),
    "sampling": ("stated", "verdict_probability", "certainty", "agreement"),
}

# Body keys a confidence block may not set on top of the reserved ones: the
# merged chat_template_kwargs (reasoning lives there), streaming (the client
# reads one JSON body), and `n` (only sampling sets it, from `sampling.n`).
_CONFIDENCE_RESERVED = _RESERVED_BODY_KEYS | {"chat_template_kwargs", "stream", "n"}


def _refuse_reserved(where: str, body: dict[str, Any]) -> None:
    reserved = sorted(_CONFIDENCE_RESERVED & set(body))
    if reserved:
        raise ValueError(
            f"{where} may not set {reserved} — the prompt builders own model / messages / "
            "the token field / response_format / chat_template_kwargs, the client reads "
            "one non-streamed body, and `n` comes from confidence.sampling.n"
        )


class LogprobsConfig(_Strict):
    """``confidence.logprobs`` — reading the score's token distribution."""

    request: dict[str, Any] = Field(
        default_factory=lambda: {"logprobs": True, "top_logprobs": 20},
        description="Merged into the SCORE request body only — whatever this provider's API wants.",
    )
    entries_path: str = Field(
        default="choices.0.logprobs.content", min_length=1,
        description="Dotted path to the [{token, logprob, <top_key>}] list in the response.",
    )
    top_key: str = Field(default="top_logprobs", min_length=1)
    number_tokens: Literal["auto", "single", "digits"] = "auto"
    min_coverage: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _body(self) -> "LogprobsConfig":
        _refuse_reserved("confidence.logprobs.request", self.request)
        return self


class SamplingConfig(_Strict):
    """``confidence.sampling`` — the empirical distribution of n answers."""

    n: int = Field(default=5, ge=2, le=32)
    via: Literal["n_param", "repeat"] = "n_param"
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _body(self) -> "SamplingConfig":
        _refuse_reserved("confidence.sampling.extra_body", self.extra_body)
        return self


class CalibrationConfig(_Strict):
    """``confidence.calibration`` — a fitted curve from one signal to a 0-100
    "right this often" number (``bin/confidence_calibration.py`` prints it)."""

    signal: Literal["stated", "verdict_probability", "certainty", "agreement"]
    points: list[tuple[float, float]] = Field(min_length=1)
    n: Optional[int] = Field(default=None, ge=0)
    fitted_on: Optional[str] = None
    ground_truth: Optional[str] = None

    @field_validator("points")
    @classmethod
    def _monotone(cls, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        for x, y in points:
            if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 100.0):
                raise ValueError(
                    f"calibration points are [x, y] with x in [0, 1] and y in [0, 100]; "
                    f"got [{x}, {y}]"
                )
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x1 < x0 or y1 < y0:
                raise ValueError(
                    "calibration points must be non-decreasing in both x and y "
                    f"(isotonic); [{x0}, {y0}] is followed by [{x1}, {y1}]"
                )
        return points


class ConfidenceConfig(_Strict):
    """An entry's ``confidence:`` block. Every knob has a default; no block at
    all is ``method: stated``."""

    method: Literal["logprobs", "sampling", "stated"] = "stated"
    logprobs: LogprobsConfig = Field(default_factory=LogprobsConfig)
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    fallback: Literal["stated"] = "stated"
    headline: Literal["stated", "calibrated"] = "stated"
    calibration: Optional[CalibrationConfig] = None

    @model_validator(mode="after")
    def _consistent(self) -> "ConfidenceConfig":
        if self.headline == "calibrated" and self.calibration is None:
            raise ValueError(
                "confidence.headline: calibrated needs confidence.calibration (signal + "
                "points) — fit one with bin/confidence_calibration.py; until then keep "
                "headline: stated"
            )
        if self.calibration is not None:
            produced = SIGNALS_BY_METHOD[self.method]
            if self.calibration.signal not in produced:
                raise ValueError(
                    f"confidence.calibration.signal {self.calibration.signal!r} is not one "
                    f"method {self.method!r} produces ({list(produced)})"
                )
        return self

    def score_body(self) -> dict[str, Any]:
        """What a SCORE request adds for this method (the other kinds add nothing)."""
        if self.method == "logprobs":
            return dict(self.logprobs.request)
        if self.method == "sampling":
            out = dict(self.sampling.extra_body)
            if self.sampling.via == "n_param":
                out["n"] = int(self.sampling.n)
            return out
        return {}

    @property
    def slots(self) -> int:
        """Slots of the model's limit ONE score request holds: n for
        ``sampling`` via ``n_param`` (n sequences on the server), else 1."""
        if self.method == "sampling" and self.sampling.via == "n_param":
            return int(self.sampling.n)
        return 1


class ModelEntry(_Strict):
    """One ``models:`` entry, as written in the file."""

    description: str = Field(min_length=1)
    url: str
    served_name: str = Field(min_length=1)
    api_key_env: Optional[str] = None
    api_key_header: str = Field(default="Authorization", min_length=1)
    vision: bool
    max_images_per_prompt: Optional[int] = Field(default=None, ge=0)
    reasoning: Optional[ReasoningConfig] = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    json_mode: bool = True
    token_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    max_tokens: MaxTokens
    timeout_s: float = Field(gt=0)
    max_concurrent: int = Field(ge=1)
    text_char_budget: int = Field(ge=0)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"url must be a full http:// or https:// URL, got {value!r}")
        return value.strip()

    @field_validator("api_key_env")
    @classmethod
    def _env_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _ENV_NAME_RE.match(value):
            raise ValueError(f"api_key_env must be an environment variable NAME, got {value!r}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ModelEntry":
        if self.vision:
            if self.max_images_per_prompt is None:
                self.max_images_per_prompt = 1
            if self.max_images_per_prompt < 1:
                raise ValueError("a vision model takes at least one image per prompt")
        else:
            if self.max_images_per_prompt not in (None, 0):
                raise ValueError("a text-only model (vision: false) takes 0 images per prompt")
            self.max_images_per_prompt = 0
        reserved = sorted(_RESERVED_BODY_KEYS & set(self.extra_body))
        if reserved:
            raise ValueError(
                f"extra_body may not set {reserved} — model is served_name, the token "
                "field is token_param / max_tokens, response_format is json_mode"
            )
        kwargs = self.extra_body.get("chat_template_kwargs")
        if kwargs is not None and not isinstance(kwargs, dict):
            raise ValueError("extra_body.chat_template_kwargs must be a mapping")
        kwargs = kwargs or {}
        reasoning = self.reasoning or ReasoningConfig()
        if reasoning.style == "chat_template_kwargs" and reasoning.key in kwargs:
            raise ValueError(
                f"the reasoning key {reasoning.key!r} is set twice — in `reasoning` and in "
                "extra_body.chat_template_kwargs; keep it in `reasoning` only"
            )
        if reasoning.style == "system_line":
            clash = sorted(_REASONING_KWARGS & set(kwargs))
            if clash:
                raise ValueError(
                    f"reasoning style system_line together with chat_template_kwargs {clash}: "
                    "a model gets its reasoning depth one way, not both"
                )
        if self.confidence.slots > self.max_concurrent:
            raise ValueError(
                f"confidence.sampling.n {self.confidence.slots} via n_param holds "
                f"{self.confidence.slots} slots of max_concurrent {self.max_concurrent} per "
                "score call (n sequences on the server) — raise max_concurrent, lower n, "
                "or use via: repeat"
            )
        return self


class RegistryFile(_Strict):
    """The whole file."""

    default: str = Field(min_length=1)
    models: dict[str, ModelEntry] = Field(min_length=1)

    @field_validator("models")
    @classmethod
    def _ids(cls, models: dict[str, ModelEntry]) -> dict[str, ModelEntry]:
        bad = [m for m in models if not _ID_RE.match(m)]
        if bad:
            raise ValueError(
                f"model ids must be 1-64 of letters, digits, '.', '_', '-' (starting with a "
                f"letter or digit); got {bad}"
            )
        return models

    @model_validator(mode="after")
    def _known_default(self) -> "RegistryFile":
        if self.default not in self.models:
            raise ValueError(
                f"default {self.default!r} is not one of the models ({sorted(self.models)})"
            )
        return self


# ---------------------------------------------------------------------------
# The runtime objects
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class ModelSpec:
    """One loaded registry entry.

    ``_headers`` carries the resolved API key and is the ONLY place it lives;
    it is excluded from ``repr`` and never part of ``public()``.
    """

    id: str
    description: str
    url: str
    served_name: str
    vision: bool
    max_images_per_prompt: int
    reasoning: dict[str, Any]
    extra_body: dict[str, Any]
    json_mode: bool
    token_param: str
    max_tokens: dict[str, int]
    timeout_s: float
    max_concurrent: int
    text_char_budget: int
    api_key_env: Optional[str] = None
    api_key_header: str = "Authorization"
    available: bool = True
    unavailable_reason: Optional[str] = None
    # The parsed `confidence:` block (defaults = method stated). Read by
    # body() for score calls, by llm.client (what to extract) and by
    # llm.confidence (the signals, the headline).
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    _headers: dict[str, str] = field(default_factory=dict, repr=False)
    limit: ConcurrencyLimit = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.limit = ConcurrencyLimit(self.max_concurrent, name=f"llm:{self.id}")

    # ── What a request needs ──────────────────────────────────────────────
    def headers(self) -> dict[str, str]:
        """The request headers (the auth header when the entry has a key)."""
        return dict(self._headers)

    @property
    def authenticated(self) -> bool:
        return self.api_key_env is not None

    @property
    def reasoning_line(self) -> Optional[str]:
        """The system-prompt line a ``system_line`` model gets, else None."""
        if self.reasoning.get("style") == "system_line":
            return self.reasoning.get("value")
        return None

    def body(self, messages: list[dict], kind: str) -> dict[str, Any]:
        """A chat-completion request body for one call of ``kind``.

        Key order is model, messages, the token field, response_format, then
        the extras — the order the builders always produced, so a request is
        recognisable in a log. ``chat_template_kwargs`` is the extras' own
        merged with the reasoning variable (a clash was refused at load).

        A ``score`` call also carries what the entry's ``confidence.method``
        asks for (``ConfidenceConfig.score_body``: the logprobs request, or
        ``n`` plus the sampling extras), after ``extra_body`` so a sampling
        override wins for the score call alone. Box, select and describe
        bodies never change.
        """
        out: dict[str, Any] = {
            "model": self.served_name,
            "messages": messages,
            self.token_param: int(self.max_tokens[kind]),
        }
        if self.json_mode:
            out["response_format"] = {"type": "json_object"}
        extra = {k: v for k, v in self.extra_body.items() if k != "chat_template_kwargs"}
        out.update(extra)
        if kind == "score":
            out.update(self.confidence.score_body())
        kwargs = dict(self.extra_body.get("chat_template_kwargs") or {})
        if self.reasoning.get("style") == "chat_template_kwargs":
            kwargs[self.reasoning["key"]] = self.reasoning["value"]
        if kwargs:
            out["chat_template_kwargs"] = kwargs
        return out

    # ── What it supports ──────────────────────────────────────────────────
    @property
    def supports_boxes(self) -> bool:
        return self.vision

    @property
    def supports_references(self) -> bool:
        return self.vision and self.max_images_per_prompt >= 2

    @property
    def supports_contrastive(self) -> bool:
        return self.vision and self.max_images_per_prompt >= 3

    def public(self) -> dict[str, Any]:
        """The GET /models entry. Never a key value — only whether there is one."""
        parts = urlsplit(self.url)
        return {
            "id": self.id,
            "description": self.description,
            "served_name": self.served_name,
            "endpoint": {"scheme": parts.scheme, "host": parts.hostname, "port": parts.port},
            "authenticated": self.authenticated,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "vision": self.vision,
            "max_images_per_prompt": self.max_images_per_prompt,
            "supports": {
                "boxes": self.supports_boxes,
                "references": self.supports_references,
                "contrastive_references": self.supports_contrastive,
                "text_only_scoring": not self.vision,
            },
            "reasoning": dict(self.reasoning),
            "text_char_budget": self.text_char_budget,
            "max_tokens": dict(self.max_tokens),
            "timeout_s": self.timeout_s,
            "max_concurrent": self.max_concurrent,
            "in_flight": self.limit.in_flight,
            # How detail.confidence is measured and whether the headline is
            # calibrated — not the points, not the request body.
            "confidence": {
                "method": self.confidence.method,
                "headline": self.confidence.headline,
                "calibrated": self.confidence.calibration is not None,
            },
        }


class Registry:
    """The loaded entries, by id, and the default."""

    def __init__(self, models: dict[str, ModelSpec], default: str, *, path: Optional[Path] = None):
        self.models = dict(models)
        self.default_id = default
        self.path = path

    @property
    def default(self) -> ModelSpec:
        return self.models[self.default_id]

    def ids(self) -> list[str]:
        return list(self.models)

    def get(self, model_id: str) -> ModelSpec:
        """The entry; KeyError for an unknown id."""
        return self.models[model_id]

    def resolve(self, model_id: Optional[str]) -> ModelSpec:
        """``model_id``'s entry, or the default for None.

        An id that is no longer in the registry (the file changed between a
        job's submit and its run) falls back to nothing: KeyError, which the
        caller's unit turns into ``status: "error"``.
        """
        if model_id is None:
            return self.default
        try:
            return self.models[model_id]
        except KeyError:
            raise KeyError(
                f"model {model_id!r} is not in the registry any more "
                f"(models: {self.ids()})"
            ) from None

    def check_selectable(self, model_id: str) -> ModelSpec:
        """The entry a request may select. Raises ValueError (a 400) for an
        unknown or unavailable id, naming the valid ones."""
        spec = self.models.get(model_id)
        if spec is None:
            raise ValueError(
                f"unknown model {model_id!r}; valid models: {self.ids()} (GET /models)"
            )
        if not spec.available:
            raise ValueError(f"model {model_id!r} is unavailable: {spec.unavailable_reason}")
        return spec

    def total_concurrency(self) -> int:
        """Σ max_concurrent — every request slot the registry can hold at once."""
        return sum(spec.max_concurrent for spec in self.models.values())

    def public(self) -> dict[str, Any]:
        """The GET /models payload."""
        return {
            "default": self.default_id,
            "models": [spec.public() for spec in self.models.values()],
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _dotted_host(host: str) -> bool:
    """A host that is an IP literal or a dotted (public / LAN) name — anything
    but a single-label service name on the Docker network or localhost."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return "." in host


def _format(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = str(err.get("msg", "invalid"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _build(model_id: str, entry: ModelEntry, env: Mapping[str, str]) -> ModelSpec:
    reasoning = (entry.reasoning or ReasoningConfig()).model_dump(exclude_none=True)
    looks_muse = "muse" in model_id.lower() or "muse" in entry.served_name.lower()
    if reasoning.get("style") == "system_line" and looks_muse:
        raise RegistryError(
            f"models.yaml: {model_id}: reasoning style system_line on Muse Glimmer — its "
            "chat template reads `reasoning_strength`, and a reasoning line in the system "
            "prompt overrides that variable. Use {style: chat_template_kwargs, key: "
            "reasoning_strength, value: <low|medium|high|xhigh>}"
        )
    headers: dict[str, str] = {}
    available, reason = True, None
    if entry.api_key_env is not None:
        host = urlsplit(entry.url).hostname or ""
        if urlsplit(entry.url).scheme == "http" and _dotted_host(host):
            raise RegistryError(
                f"models.yaml: {model_id}: api_key_env {entry.api_key_env} on plain "
                f"http:// to {host!r} would send the key in clear text — use https, or a "
                "service name on ai_shared"
            )
        key = (env.get(entry.api_key_env) or "").strip()
        if not key:
            available = False
            reason = (
                f"its API key variable {entry.api_key_env} is not set in the container "
                f"environment (list it in docker-compose.classifier.yml as "
                f"`- {entry.api_key_env}=${{{entry.api_key_env}:-}}` and set it in .env)"
            )
        elif entry.api_key_header.lower() == "authorization":
            headers[entry.api_key_header] = f"Bearer {key}"
        else:
            headers[entry.api_key_header] = key
    return ModelSpec(
        id=model_id,
        description=" ".join(entry.description.split()),
        url=entry.url,
        served_name=entry.served_name,
        vision=entry.vision,
        max_images_per_prompt=int(entry.max_images_per_prompt or 0),
        reasoning=reasoning,
        extra_body=dict(entry.extra_body),
        json_mode=entry.json_mode,
        token_param=entry.token_param,
        max_tokens=entry.max_tokens.model_dump(),
        timeout_s=float(entry.timeout_s),
        max_concurrent=int(entry.max_concurrent),
        text_char_budget=int(entry.text_char_budget),
        api_key_env=entry.api_key_env,
        api_key_header=entry.api_key_header,
        available=available,
        unavailable_reason=reason,
        confidence=entry.confidence,
        _headers=headers,
    )


def parse_registry(
    data: Any, *, env: Optional[Mapping[str, str]] = None, path: Optional[Path] = None
) -> Registry:
    """A parsed YAML document → ``Registry``. Raises ``RegistryError``."""
    env = os.environ if env is None else env
    where = str(path) if path else "models.yaml"
    try:
        parsed = RegistryFile.model_validate(data)
    except ValidationError as exc:
        raise RegistryError(f"{where} is not a valid model registry: {_format(exc)}") from None
    specs = {mid: _build(mid, entry, env) for mid, entry in parsed.models.items()}
    registry = Registry(specs, parsed.default, path=path)
    if not registry.default.available:
        raise RegistryError(
            f"{where}: the default model {parsed.default!r} is unavailable — "
            f"{registry.default.unavailable_reason}; every request without a `model` "
            "would fail"
        )
    return registry


def load_registry(
    path: Optional[Path | str] = None, *, env: Optional[Mapping[str, str]] = None
) -> Registry:
    """Read and validate ``path`` (default: ``DEFAULT_PATH``). Raises ``RegistryError``."""
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read the model registry {path}: {exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path} is not valid YAML: {exc}") from None
    return parse_registry(data, env=env, path=path)


_registry: Optional[Registry] = None


def get_registry() -> Registry:
    """The process-wide registry, loaded from ``DEFAULT_PATH`` on first use."""
    global _registry
    if _registry is None:
        _registry = load_registry()
    return _registry


def use_registry(registry: Optional[Registry]) -> Optional[Registry]:
    """Swap the process-wide registry (tests); returns the previous one.
    ``None`` makes the next ``get_registry()`` reload the file."""
    global _registry
    previous, _registry = _registry, registry
    return previous


def removed_env_set(env: Optional[Mapping[str, str]] = None) -> list[str]:
    """The replaced env vars still present in ``env`` (default: os.environ)."""
    env = os.environ if env is None else env
    return [name for name in REMOVED_ENV if name in env]


def startup_check() -> Registry:
    """Load the registry (raising ``RegistryError`` for a bad file), log what
    loaded, and warn once about removed env vars still set."""
    registry = get_registry()
    stale = removed_env_set()
    if stale:
        logger.warning(
            "models: %s %s set but no longer read — the model registry (%s) replaced "
            "them; remove them from .env and the compose file",
            ", ".join(stale), "is" if len(stale) == 1 else "are", registry.path or DEFAULT_PATH,
        )
    for spec in registry.models.values():
        logger.info(
            "models: %s%s → %s (%s) vision=%s images=%d concurrent=%d timeout=%gs "
            "confidence=%s/%s%s",
            spec.id, " [default]" if spec.id == registry.default_id else "",
            urlsplit(spec.url).hostname, spec.served_name, spec.vision,
            spec.max_images_per_prompt, spec.max_concurrent, spec.timeout_s,
            spec.confidence.method, spec.confidence.headline,
            "" if spec.available else f" UNAVAILABLE: {spec.unavailable_reason}",
        )
    return registry
