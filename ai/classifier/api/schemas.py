"""Pydantic request models for the classifier API.

One request shape, accepted two ways and parsed into ONE model, so a JSON
caller and a multipart caller can never be treated differently:

  POST /assess
    AssessRequest
      ├── documents : list[DocumentInput]   — each base64 | url | text
      │   (document : DocumentInput         — the one-item shorthand)
      └── criteria  : list[CriterionInput]  — what to evaluate

    JSON body:  {"documents": [{...}, ...], "criteria": [...]}, or
                {"document": {...}, "criteria": [...]} — a single document is
                a one-item list; sending both keys is a 400.
    multipart:  repeated `file` parts (the legacy `image` alias too) and/or
                repeated `text` fields, plus a `criteria` field holding the
                same JSON array. ``api.assess`` turns each upload into a
                base64 DocumentInput and validates the SAME AssessRequest.

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

import re
from typing import Any, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from common.documents import Document, InvalidPatternError, match_text

from api.criterion_options import (
    LEGACY_TOP_LEVEL_KEYS,
    NAME_MAX_CHARS,
    NOT_GUIDED,
    OPTIONS_MODELS,
    TextOptions,
    answered_by_llm,
    can_locate,
)
from config import (
    DEFAULT_CRITERIA,
    REFERENCE_MAX_PER_REQUEST,
    REFERENCE_MAX_TAGS,
    REFERENCE_TAG_MAX_CHARS,
    VISION_LLM_MAX_IMAGES_PER_PROMPT,
)
from cv import get_detector

CriterionType = Literal["llm", "text", "cv", "detector"]


class ClassifierMetadata(BaseModel):
    """Per-job metadata for the classifier, stored in ``jobs.metadata``.

    ``type`` is "assess" (POST /assess) or "reference" (the creation job
    POST /references queues); rows written by an older container may still
    say "compare" or "locate", and the worker refuses those by name.

    ``reference_id`` is set on a "reference" job — the reference it builds.
    ``references`` lists the reference ids an "assess" job reads, which is
    what lets DELETE /references/{id} refuse (409) while one is queued or
    running. Both are left out of the stored JSON when unset, so an assess
    job's metadata is exactly ``{type, request_id}`` as before.
    """

    type: str
    request_id: str
    reference_id: Optional[str] = None
    references: Optional[list[str]] = None

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("exclude_none", True)
        return super().model_dump_json(**kwargs)


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
        if "reference" in raw and self.type in ("text", "detector"):
            raise ValueError(f"options.reference: {NOT_GUIDED} (a {self.type} criterion)")
        try:
            self.options = model.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(_format_errors(exc, "options.")) from None
        if self.type == "text":
            _check_pattern(self.name, self.options)
        if self.type == "cv" and self.options.reference is not None and not self.guided_by_llm():
            raise ValueError(
                f"options.reference: {NOT_GUIDED} — '{self.name}' is answered by "
                + ("an OpenCV detector" if get_detector(self.name) is not None
                   else "the detector service (options.fallback)")
            )
        return self

    # ── Resolved values ───────────────────────────────────────────────────
    def resolved_options(self) -> dict[str, Any]:
        """Options after defaults and caps — echoed as ``options_used``."""
        return self.options.resolve(self.name)

    def can_locate(self) -> bool:
        """Whether this criterion can produce any geometry at all."""
        return can_locate(self.type, self.name, self.resolved_options())

    def guided_by_llm(self) -> bool:
        """Whether the vision model answers it — the only criteria a reference
        can guide (``llm``, or a ``cv`` name answered by the llm fallback)."""
        return answered_by_llm(self.type, self.name, self.resolved_options())

    def reference_options(self) -> Optional[dict[str, Any]]:
        """The resolved ``options.reference``, or None when it was not sent."""
        return self.resolved_options().get("reference")

    def scope(self) -> str:
        """"document" for a text criterion with options.scope "document" — one
        unit per DOCUMENT — and "page" (one unit per item) for everything else."""
        if self.type == "text" and self.options.scope == "document":
            return "document"
        return "page"

    def aggregate_rules(self) -> dict[str, str]:
        """The resolved ``{"pages", "documents"}`` rules (see options.aggregate)."""
        return dict(self.resolved_options()["aggregate"])


class DocumentInput(BaseModel):
    """One document: base64 bytes, a URL to fetch, or inline text.

    The kind (JPEG/PNG, PDF, .txt, .docx) is determined from the BYTES, never
    from a filename or content type. ``text`` is plain text treated as a .txt
    document. A URL is SSRF-checked (``common.net``) before it is fetched, at
    submit time — the item cap needs the bytes (a PDF counts its pages).
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


_REFERENCE_ID_RE = re.compile(r"^r[0-9a-f]{12}$")


class AutoReferences(BaseModel):
    """``references: {"auto": true, "tags": [...], "tags_match": "all" | "any"}``
    — let the service pick examples from the stored references. ``"auto"``
    alone is this with no tag filter."""

    model_config = ConfigDict(extra="forbid")

    auto: Literal[True]
    tags: list[str] = Field(
        default_factory=list,
        description="Only references carrying these tags (lowercased).",
    )
    tags_match: Literal["all", "any"] = Field(
        default="any", description="A reference needs all of the tags, or any one."
    )

    @field_validator("tags")
    @classmethod
    def _tags(cls, tags: list[str]) -> list[str]:
        out: list[str] = []
        for tag in tags:
            value = tag.strip().lower()
            if not value or len(value) > REFERENCE_TAG_MAX_CHARS:
                raise ValueError(f"each tag is 1-{REFERENCE_TAG_MAX_CHARS} characters")
            if value not in out:
                out.append(value)
        if len(out) > REFERENCE_MAX_TAGS:
            raise ValueError(f"at most {REFERENCE_MAX_TAGS} tags")
        return out


class AssessRequest(BaseModel):
    """The whole /assess request — JSON and multipart both land here.

    After validation ``documents`` is ALWAYS the list and ``document`` is
    None: the single-document shorthand is normalised into a one-item list,
    so nothing downstream has two shapes to handle.
    """

    model_config = ConfigDict(extra="forbid")

    documents: Optional[list[DocumentInput]] = Field(
        default=None,
        min_length=1,
        description=(
            "The documents, in order. Every page of every document is one item; "
            "the total is capped at CLASSIFIER_MAX_ITEMS."
        ),
    )
    document: Optional[DocumentInput] = Field(
        default=None,
        description="Shorthand for a one-item 'documents' list. Not with 'documents'.",
    )
    criteria: list[CriterionInput] = Field(
        default_factory=lambda: [CriterionInput.model_validate(c) for c in DEFAULT_CRITERIA],
        min_length=1,
        description=(
            "What to evaluate. Omitted: the criteria of the listed `references`, "
            "merged by name — or, without references, the four default quality criteria."
        ),
    )
    references: Optional[Union[list[str], Literal["auto"], AutoReferences]] = Field(
        default=None,
        description=(
            "Stored worked examples (POST /references) to show the vision model beside "
            "each llm-answered criterion: a list of reference ids, \"auto\", or "
            "{\"auto\": true, \"tags\": [...], \"tags_match\": \"all\" | \"any\"}."
        ),
    )

    def criteria_given(self) -> bool:
        """Whether the caller sent ``criteria`` (False = the default, or —
        with explicit references — theirs, inherited at submit)."""
        return "criteria" in self.model_fields_set

    def reference_ids(self) -> list[str]:
        """The explicit reference ids, or [] for none / auto."""
        return list(self.references) if isinstance(self.references, list) else []

    def auto_references(self) -> Optional[AutoReferences]:
        return self.references if isinstance(self.references, AutoReferences) else None

    @field_validator("references", mode="after")
    @classmethod
    def _reference_shape(cls, value: Any) -> Any:
        if value is None:
            return None
        if value == "auto":
            return AutoReferences(auto=True)
        if isinstance(value, list):
            if not value:
                raise ValueError("references is an empty list; omit it, or list reference ids")
            bad = [v for v in value if not _REFERENCE_ID_RE.match(v)]
            if bad:
                raise ValueError(f"not reference ids (r + 12 hex): {bad}")
            dupes = sorted({v for v in value if value.count(v) > 1})
            if dupes:
                raise ValueError(f"duplicate reference ids: {dupes}")
            if len(value) > REFERENCE_MAX_PER_REQUEST:
                raise ValueError(
                    f"{len(value)} references exceeds CLASSIFIER_REFERENCE_MAX_PER_REQUEST="
                    f"{REFERENCE_MAX_PER_REQUEST}"
                )
        return value

    @model_validator(mode="after")
    def _one_document_list(self) -> "AssessRequest":
        if self.document is not None and self.documents is not None:
            raise ValueError(
                "send either 'document' (one) or 'documents' (a list), not both"
            )
        if self.document is None and self.documents is None:
            raise ValueError(
                "no document: send 'documents' (a list of {type, data}) or 'document'"
            )
        if self.document is not None:
            self.documents = [self.document]
            self.document = None
        return self

    @model_validator(mode="after")
    def _cross_criterion_rules(self) -> "AssessRequest":
        check_criteria_rules(self.criteria)
        return self

    @model_validator(mode="after")
    def _reference_rules(self) -> "AssessRequest":
        """The reference rules that need no store lookup (``references.resolve``
        does the rest, at submit)."""
        guided = [c.name for c in self.criteria if c.reference_options() is not None]
        if self.references is None:
            if guided:
                raise ValueError(
                    f"options.reference on {guided} but the request lists no `references`"
                )
            return self
        if VISION_LLM_MAX_IMAGES_PER_PROMPT < 2:
            raise ValueError(
                "this container's vision model takes one image per request "
                f"(VISION_LLM_MAX_IMAGES_PER_PROMPT={VISION_LLM_MAX_IMAGES_PER_PROMPT}), so an "
                "example cannot be shown beside the candidate; drop `references`"
            )
        return self


def check_criteria_rules(criteria: list[CriterionInput]) -> None:
    """The cross-criterion rules, for any request that carries a criteria list.

    Unique names, ``score: false`` only where there is geometry to return,
    dependencies that exist, are scored and are acyclic. Raises
    ``ValueError`` (a 400 through the request model that calls it) — shared
    by ``AssessRequest`` and POST /references' request model so the two can
    never accept different lists.
    """
    by_name: dict[str, CriterionInput] = {}
    for c in criteria:
        if c.name in by_name:
            raise ValueError(f"duplicate criterion name {c.name!r}; names must be unique")
        by_name[c.name] = c

    for c in criteria:
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
    for c in criteria:
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
