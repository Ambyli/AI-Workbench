"""Pydantic request models for the classifier API.

One request shape, accepted two ways and parsed into ONE model, so a JSON
caller and a multipart caller can never be treated differently:

  POST /assess
    AssessRequest
      ├── document : DocumentInput          — base64 | url | text
      └── criteria : list[CriterionInput]   — what to evaluate

    JSON body:  {"document": {...}, "criteria": [...]}
    multipart:  a `file` part (or the legacy `image` alias) or a `text`
                field, plus a `criteria` field holding the same JSON array.
                ``api.assess`` turns the upload into a base64 DocumentInput
                and validates the SAME AssessRequest.

A criterion's top level is only what every type shares — ``name``, ``type``,
``weight``, ``depends_on``, ``score``; everything type-specific is in
``options``, validated per type by ``api.criterion_options``. The
cross-criterion rules (unique names, dependencies that exist and are scored,
no cycles, ``score: false`` only where there is geometry to return) are
checked here, on the request, so every one of them is a 400 at submit.

``ClassifierMetadata`` is the other half of the shape: not a request body, but
the per-job blob stored in ``jobs.metadata`` and echoed by GET /jobs.

Process flow position: the bottom of the ``api`` package and the one module in
it that everything else imports — ``analysis``, ``llm``, ``regions`` and
``jobs`` all validate against these models, which is why ``api/__init__.py``
imports nothing (a router import there would close the cycle).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from common.documents import Document, InvalidPatternError, match_text

from api.criterion_options import (
    LEGACY_TOP_LEVEL_KEYS,
    NAME_MAX_CHARS,
    OPTIONS_MODELS,
    TextOptions,
    can_locate,
)
from config import DEFAULT_CRITERIA

CriterionType = Literal["llm", "text", "cv", "detector"]


class ClassifierMetadata(BaseModel):
    """Per-job metadata for the classifier, stored in ``jobs.metadata``.

    ``type`` is always "assess" now; rows written by an older container may
    still say "compare" or "locate", and the worker refuses those by name.
    """

    type: str
    request_id: str


def _format_errors(exc: ValidationError, prefix: str = "") -> str:
    """A pydantic ValidationError as one readable sentence per problem."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        where = f"{prefix}{loc}" if loc else prefix.rstrip(".")
        msg = err.get("msg", "invalid")
        # "Value error, x" → "x": the prefix is pydantic's, not ours.
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        parts.append(f"{where}: {msg}" if where else msg)
    return "; ".join(parts)


class CriterionInput(BaseModel):
    """One criterion: shared fields at the top, type-specific ones in options.

    ``options`` arrives as a plain object and is replaced, during validation,
    by the options model for ``type`` — so every consumer reads
    ``c.options.hint`` on an ``llm`` criterion and never a dict.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=NAME_MAX_CHARS,
        description="The criterion to evaluate; unique within the request.",
    )
    type: CriterionType = Field(
        default="llm",
        description=(
            "'llm': the vision model scores it (one call per criterion). "
            "'text': deterministic search of the text layer. 'cv': a registered "
            "OpenCV detector by name (GET /cv-detectors), with options.fallback "
            "when none matches. 'detector': the open-vocabulary detector "
            "service, using the name as its text prompt."
        ),
    )
    weight: float = Field(
        default=1.0,
        gt=0.0,
        description="Relative weight in the overall score. Default 1.",
    )
    depends_on: Optional[str] = Field(
        default=None,
        description=(
            "Name of another SCORED criterion that must PASS first. Until it "
            "does, this criterion is not evaluated at all; if it does not, this "
            "one is SKIPPED (no model call spent). Chains propagate."
        ),
    )
    score: bool = Field(
        default=True,
        description=(
            "false = locate without judging: the criterion still produces "
            "regions but is excluded from the weighted score and the verdict, "
            "and its score / verdict / confidence are null. Refused for a "
            "criterion that cannot produce geometry."
        ),
    )
    options: Any = Field(
        default=None,
        description="Type-specific options; see GET /criterion-types.",
    )

    @model_validator(mode="before")
    @classmethod
    def _legacy_keys(cls, data: Any) -> Any:
        """Name the old top-level keys instead of a bare 'extra inputs' error."""
        if isinstance(data, dict):
            stray = [k for k in data if k in LEGACY_TOP_LEVEL_KEYS]
            if stray:
                raise ValueError(
                    "these keys moved into 'options': "
                    + ", ".join(
                        f"{k} (options.{k} on a {LEGACY_TOP_LEVEL_KEYS[k]} criterion)"
                        for k in stray
                    )
                )
        return data

    @model_validator(mode="after")
    def _typed_options(self) -> "CriterionInput":
        model = OPTIONS_MODELS[self.type]
        raw = self.options
        if isinstance(raw, model):
            return self
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError(f"options must be an object, got {type(raw).__name__}")
        try:
            self.options = model.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(_format_errors(exc, "options.")) from None
        if self.type == "text":
            _check_pattern(self.name, self.options)
        return self

    # ── Resolved values ───────────────────────────────────────────────────
    def resolved_options(self) -> dict[str, Any]:
        """Options after defaults and caps — echoed as ``options_used``."""
        return self.options.resolve(self.name)

    def can_locate(self) -> bool:
        """Whether this criterion can produce any geometry at all."""
        return can_locate(self.type, self.name, self.resolved_options())


class DocumentInput(BaseModel):
    """The document: base64 bytes, a URL to fetch, or inline text.

    The kind (JPEG/PNG, PDF, .txt, .docx) is determined from the BYTES, never
    from a filename or content type. ``text`` is plain text treated as a .txt
    document. A URL is SSRF-checked (``common.net``) before it is fetched, at
    submit time — the single-page check needs the bytes.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["base64", "url", "text"] = Field(
        description="'base64': data is the file base64-encoded. 'url': data is "
                    "an http(s) URL. 'text': data is the document text itself."
    )
    data: str = Field(min_length=1, description="The payload for `type`.")
    filename: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Recorded in document_info; never used to detect the kind.",
    )


class AssessRequest(BaseModel):
    """The whole /assess request — JSON and multipart both land here."""

    model_config = ConfigDict(extra="forbid")

    document: DocumentInput
    criteria: list[CriterionInput] = Field(
        default_factory=lambda: [CriterionInput.model_validate(c) for c in DEFAULT_CRITERIA],
        min_length=1,
        description="What to evaluate. Omitted: the four default quality criteria.",
    )

    @model_validator(mode="after")
    def _cross_criterion_rules(self) -> "AssessRequest":
        by_name: dict[str, CriterionInput] = {}
        for c in self.criteria:
            if c.name in by_name:
                raise ValueError(f"duplicate criterion name {c.name!r}; names must be unique")
            by_name[c.name] = c

        for c in self.criteria:
            if not c.score and not c.can_locate():
                raise ValueError(
                    f"criterion {c.name!r} has score: false but cannot produce "
                    "any geometry, so it would return nothing — "
                    + _locate_hint(c)
                )
            if c.depends_on is None:
                continue
            if c.depends_on == c.name:
                raise ValueError(f"criterion {c.name!r} depends on itself")
            dep = by_name.get(c.depends_on)
            if dep is None:
                raise ValueError(
                    f"criterion {c.name!r} depends_on {c.depends_on!r}, which is not "
                    f"a criterion in this request (known: {sorted(by_name)})"
                )
            if not dep.score:
                raise ValueError(
                    f"criterion {c.name!r} depends_on {c.depends_on!r}, which has "
                    "score: false — it produces no verdict, so nothing could ever "
                    "satisfy the dependency"
                )

        # Cycles: follow each chain; a revisit within one walk is a loop.
        for c in self.criteria:
            seen = [c.name]
            current = c
            while current.depends_on is not None:
                nxt = current.depends_on
                if nxt in seen:
                    raise ValueError(
                        "dependency cycle: " + " -> ".join(seen + [nxt])
                    )
                seen.append(nxt)
                current = by_name[nxt]
        return self


def _check_pattern(name: str, options: TextOptions) -> None:
    """Compile a text pattern through the matcher's own path, at submit.

    Run against an empty document, so the rules (length cap, regex syntax)
    can never drift from ``common.documents.match_text`` — a bad regular
    expression is a 400 on the POST, not a job that fails in a worker.
    """
    resolved = options.resolve(name)
    try:
        match_text(
            Document(kind="txt", pages=[]),
            resolved["pattern"],
            resolved["match"],
            case_sensitive=resolved["case_sensitive"],
            fuzzy_threshold=resolved["fuzzy_threshold"],
            min_count=resolved["min_count"],
        )
    except InvalidPatternError as exc:
        raise ValueError(f"options.pattern: {exc}") from None


def _locate_hint(c: CriterionInput) -> str:
    """What to change so a ``score: false`` criterion can locate something."""
    if c.type == "llm":
        return (
            'set options.boxes: true with options.hint "presence" or "auto" '
            "(a quality criterion has no location)"
        )
    if c.type == "cv":
        return (
            "this cv name is a whole-page measurement or falls back to the llm "
            "without boxes; use a feature detector name, options.fallback "
            '"detector", or an llm criterion with options.boxes: true'
        )
    return "set score: true"


def validation_message(exc: ValidationError) -> str:
    """The 400 body for a request that failed ``AssessRequest`` validation."""
    return _format_errors(exc)
