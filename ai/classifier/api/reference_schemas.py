"""Pydantic request models for the /references endpoints.

    POST /references
      ReferenceRequest
        ├── document + page        one page: a JPEG/PNG, or page `page` of a PDF
        │   (or from_job + from_job_item — a completed /assess job's item)
        ├── criteria   : list[CriterionInput]   — the same model /assess uses
        ├── breakdown  : {name: BreakdownInput} — the answer key, per criterion
        ├── regions    : {name: [RegionInput]}  — where; [] = the whole page
        ├── region_units : "px" | "grid"
        └── title, description, tags

    PATCH /references/{id}
      ReferencePatch — title / description / tags; nothing else is editable.

Everything checkable without the store or the bytes is checked HERE, so it is
a 400 naming the field: one source, not two; ``page`` only with a document;
``criteria`` required unless ``from_job`` supplies them; the cross-criterion
rules (``api.schemas.check_criteria_rules`` — the same function /assess
runs); a breakdown verdict that disagrees with its score
(``utils.verdict_from_score``); a breakdown for a ``score: false`` criterion;
breakdown / regions keys that name no criterion; a malformed box or polygon;
grid coordinates outside 0–1000. What needs the bytes (the page exists, a
pixel region lies inside it) or the store (``from_job``'s job) is checked by
``api.references`` before anything is queued.

Process flow position: imported by ``api.references`` only.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.schemas import CriterionInput, DocumentInput, check_criteria_rules
from config import (
    LLM_BBOX_GRID,
    REFERENCE_DESCRIPTION_MAX_CHARS,
    REFERENCE_MAX_TAGS,
    REFERENCE_TAG_MAX_CHARS,
    REFERENCE_TITLE_MAX_CHARS,
)
from utils import verdict_from_score

# A job id is common.jobs' 12 hex.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")

# The longest breakdown reason a caller may store; it is pasted into the
# example's caption, so it is kept to a few sentences.
REASON_MAX_CHARS = 1000


class BreakdownInput(BaseModel):
    """One criterion's expected answer: the score, and optionally its verdict
    (which must agree with the score) and a reason."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=1, le=10, description="The expected 1-10 score.")
    verdict: Optional[Literal["PASS", "MARGINAL", "FAIL"]] = Field(
        default=None,
        description="Optional; must be the score's verdict (7-10 PASS, 4-6 MARGINAL, 1-3 FAIL).",
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=REASON_MAX_CHARS,
        description="Why — shown to the model as the example's caption.",
    )

    @model_validator(mode="after")
    def _verdict_agrees(self) -> "BreakdownInput":
        derived = verdict_from_score(self.score)
        if self.verdict is not None and self.verdict != derived:
            raise ValueError(
                f"verdict {self.verdict} does not match score {self.score} "
                f"(a {self.score} is {derived}); send the score alone or the matching verdict"
            )
        self.verdict = derived
        return self


class RegionInput(BaseModel):
    """One region: ``{"box": [x1, y1, x2, y2]}`` or ``{"polygon": [[x, y], ...]}``,
    in ``region_units``."""

    model_config = ConfigDict(extra="forbid")

    box: Optional[list[float]] = Field(default=None, description="[x1, y1, x2, y2]")
    polygon: Optional[list[list[float]]] = Field(
        default=None, description="Three or more [x, y] vertices."
    )

    @model_validator(mode="after")
    def _one_shape(self) -> "RegionInput":
        if (self.box is None) == (self.polygon is None):
            raise ValueError("a region is exactly one of 'box' or 'polygon'")
        if self.box is not None:
            if len(self.box) != 4:
                raise ValueError(f"a box is [x1, y1, x2, y2]; got {len(self.box)} numbers")
            x1, y1, x2, y2 = self.box
            if not (x1 < x2 and y1 < y2):
                raise ValueError(f"a box needs x1 < x2 and y1 < y2; got {self.box}")
        else:
            if len(self.polygon) < 3:
                raise ValueError("a polygon needs at least three vertices")
            if any(len(p) != 2 for p in self.polygon):
                raise ValueError("every polygon vertex is [x, y]")
        if any(v < 0 for v in self.coordinates()):
            raise ValueError("region coordinates cannot be negative")
        return self

    def coordinates(self) -> list[float]:
        if self.box is not None:
            return list(self.box)
        return [v for point in self.polygon or [] for v in point]

    def points(self) -> list[tuple[float, float]]:
        if self.box is not None:
            x1, y1, x2, y2 = self.box
            return [(x1, y1), (x2, y2)]
        return [(p[0], p[1]) for p in self.polygon or []]

    @property
    def kind(self) -> str:
        return "box" if self.box is not None else "polygon"


def _clean_tags(tags: Any) -> list[str]:
    """Stripped, lowercased, de-duplicated (first wins), bounded."""
    if tags is None:
        return []
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("every tag is a string")
        value = tag.strip().lower()
        if not value:
            raise ValueError("a tag cannot be empty")
        if len(value) > REFERENCE_TAG_MAX_CHARS:
            raise ValueError(f"tag {value[:20]!r}… is over {REFERENCE_TAG_MAX_CHARS} characters")
        if value not in out:
            out.append(value)
    if len(out) > REFERENCE_MAX_TAGS:
        raise ValueError(f"at most {REFERENCE_MAX_TAGS} tags; got {len(out)}")
    return out


class _Meta(BaseModel):
    """The editable metadata, shared by POST and PATCH."""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=REFERENCE_TITLE_MAX_CHARS)
    description: Optional[str] = Field(
        default=None, min_length=1, max_length=REFERENCE_DESCRIPTION_MAX_CHARS,
        description="Omitted: generated from the page (CLASSIFIER_REFERENCE_DESCRIBE).",
    )
    tags: Optional[list[str]] = Field(
        default=None, description="Lowercased; filter GET /references and `auto` by them.",
    )

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> Any:
        return value if value is None else _clean_tags(value)


class ReferenceRequest(_Meta):
    """The whole POST /references body — JSON and multipart both land here."""

    document: Optional[DocumentInput] = Field(
        default=None, description="The example: a JPEG/PNG, or a PDF (one page of it)."
    )
    page: Optional[int] = Field(
        default=None, ge=0, description="Which PDF page (0-based). Default 0; 0 for an image."
    )
    from_job: Optional[str] = Field(
        default=None, description="A completed /assess job whose item to save instead."
    )
    from_job_item: Optional[int] = Field(
        default=None, ge=0, description="That job's global item index. Default 0."
    )
    criteria: Optional[list[CriterionInput]] = Field(
        default=None,
        min_length=1,
        description="Required with `document`; with `from_job`, omitted means the job's own.",
    )
    breakdown: dict[str, BreakdownInput] = Field(
        default_factory=dict,
        description="{criterion name: {score, verdict?, reason?}} — the answer key. "
                    "Missing names are classified by the creation job.",
    )
    regions: dict[str, list[RegionInput]] = Field(
        default_factory=dict,
        description="{criterion name: [{box} | {polygon}]} — where. A missing name is "
                    "located automatically; [] means the whole page is the example.",
    )
    region_units: Literal["px", "grid"] = Field(
        default="px",
        description="'px': original page pixels. 'grid': 0-1000 on both axes "
                    "(recommended for a PDF page, whose pixel size depends on the render DPI).",
    )

    @model_validator(mode="after")
    def _shape(self) -> "ReferenceRequest":
        if (self.document is None) == (self.from_job is None):
            raise ValueError("send exactly one of 'document' or 'from_job'")
        if self.document is not None:
            if self.from_job_item is not None:
                raise ValueError("'from_job_item' goes with 'from_job', not 'document'")
            if self.criteria is None:
                raise ValueError("'criteria' is required with 'document'")
        else:
            if self.page is not None:
                raise ValueError("'page' goes with 'document'; use 'from_job_item' with 'from_job'")
            if not _JOB_ID_RE.match(self.from_job or ""):
                raise ValueError(f"from_job {self.from_job!r} is not a job id (12 hex)")
        if self.criteria is not None:
            check_criteria_rules(self.criteria)
            guided = [c.name for c in self.criteria if c.reference_options() is not None]
            if guided:
                raise ValueError(
                    f"options.reference on {guided}: a reference's own criteria are "
                    "not guided by other references; drop it"
                )
            self.check_keys([c.name for c in self.criteria], {c.name: c for c in self.criteria})
        if self.region_units == "grid":
            for name, regions in self.regions.items():
                for region in regions:
                    if any(v > LLM_BBOX_GRID for v in region.coordinates()):
                        raise ValueError(
                            f"regions[{name!r}]: grid coordinates run 0-{int(LLM_BBOX_GRID)}"
                        )
        return self

    def check_keys(self, names: list[str], by_name: dict[str, CriterionInput]) -> None:
        """Every breakdown / regions key names a criterion; no breakdown for a
        ``score: false`` criterion. Raises ValueError. Called again by the
        endpoint once ``from_job`` has supplied the criteria."""
        known = set(names)
        for field, keys in (("breakdown", self.breakdown), ("regions", self.regions)):
            stray = sorted(k for k in keys if k not in known)
            if stray:
                raise ValueError(
                    f"{field} names {stray}, which are not criteria of this reference "
                    f"(known: {sorted(known)})"
                )
        unscored = sorted(k for k in self.breakdown if not by_name[k].score)
        if unscored:
            raise ValueError(
                f"breakdown for {unscored}: a score: false criterion has no expected answer"
            )


class ReferencePatch(_Meta):
    """PATCH /references/{id}: the metadata only. An explicit null clears a
    title or description; the content (criteria, answers, regions, page) is
    immutable — a new answer key is a new reference."""

    @model_validator(mode="after")
    def _something(self) -> "ReferencePatch":
        if not self.model_fields_set:
            raise ValueError("send at least one of 'title', 'description', 'tags'")
        return self
