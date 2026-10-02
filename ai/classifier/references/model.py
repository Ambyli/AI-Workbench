"""The reference row, its id and file-name grammar, and what guides the llm.

    Reference            one row of ``reference_examples``: the editable
                         metadata (title, description, tags), where it came
                         from (a document page, or an /assess job's item), its
                         creation job, its status, and — once ready — the
                         frozen ``record``.
    new_reference_id()   ``r`` + 12 hex.
    is_reference_id()    the id grammar, checked before any lookup or path.
    composite_name()     ``c.<slug>.jpg`` — one criterion's example image.
    FILE_NAME_RE         every file a reference directory may hold; the files
                         route refuses anything else BEFORE touching the disk.
    guides_llm()         whether a criterion is answered by the vision model
                         (an ``llm`` criterion, or a ``cv`` name with no
                         OpenCV detector whose fallback resolves to the llm) —
                         the only criteria a reference is an example FOR.

Statuses: ``pending`` (its creation job has not finished), ``ready`` (the
record is frozen and usable), ``failed`` (``error`` says why). A pending
reference whose job is gone is reconciled to failed at startup
(``references.store.reconcile``).

The record, once ready::

    {"schema": 1,
     "page": {"kind", "filename", "page", "geometry", "working"},
     "criteria": {name: {"input", "slug", "guides_llm", "expected",
                         "observed", "regions", "region_source",
                         "composite", "usable"}},
     "source": {"kind": "document" | "from_job", "job_id", "item"},
     "warnings": [...]}

``expected`` is ``{score, verdict, reason, source: caller | pipeline}`` (None
for a ``score: false`` criterion, or when nothing answered it); ``observed``
is what the pipeline itself answered (None when the caller supplied
everything and it did not run); ``regions`` are in ORIGINAL page pixels;
``region_source`` is ``caller`` | ``pipeline`` | ``whole_page``.

Process flow position: the bottom of the references package — imported by
every other module in it and by ``api.references``.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Optional

from common.vision import slugify_criterion

from api.schemas import CriterionInput

RECORD_SCHEMA = 1

STATUSES: tuple[str, ...] = ("pending", "ready", "failed")

_ID_RE = re.compile(r"^r[0-9a-f]{12}$")

# page.jpg | working.jpg | c.<slug>.jpg | regions.json | record.json. A slug is
# common.vision's lowercase [a-z0-9-] ending in a 4-hex hash, so it can never
# contain a dot or a separator.
FILE_NAME_RE = re.compile(
    r"^(?:page\.jpg|working\.jpg|regions\.json|record\.json|c\.[a-z0-9][a-z0-9-]{0,63}\.jpg)$"
)

PAGE_FILE = "page.jpg"
WORKING_FILE = "working.jpg"
REGIONS_FILE = "regions.json"
RECORD_FILE = "record.json"


def new_reference_id() -> str:
    """A fresh id: ``r`` + 12 hex — never confusable with a 12-hex job id."""
    return "r" + secrets.token_hex(6)


def is_reference_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def composite_name(name: str) -> str:
    """``c.<slug>.jpg`` for criterion ``name``."""
    return f"c.{slugify_criterion(name)}.jpg"


def file_url(reference_id: str, name: str) -> str:
    """Where ``GET /references/{id}/files/{name}`` serves a file — relative,
    for the same reason job artifact URLs are (direct :8005 and the LiteLLM
    pass-through both reach it)."""
    return f"/references/{reference_id}/files/{name}"


def guides_llm(c: CriterionInput) -> bool:
    """True when the vision model answers ``c`` — the only kind of criterion a
    reference is a worked example for.

    ``llm`` always; ``cv`` only when no OpenCV detector matches the name and
    the resolved fallback is the llm (the fallback's answer has the llm
    shape). ``text`` / ``detector`` / an OpenCV-answered ``cv`` never — the
    model is not what scores them, so an example cannot steer them.
    """
    return c.guided_by_llm()  # api.criterion_options.answered_by_llm — one rule


@dataclass
class Reference:
    """One ``reference_examples`` row."""

    id: str
    status: str
    source_kind: str
    created_at: str
    updated_at: str
    title: Optional[str] = None
    description: Optional[str] = None
    description_source: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    job_id: Optional[str] = None
    source_job_id: Optional[str] = None
    source_item: Optional[int] = None
    error: Optional[str] = None
    record: Optional[dict[str, Any]] = None

    def criteria_summary(self) -> list[dict[str, Any]]:
        """``[{name, type, expected verdict / score, region_source, guides_llm}]``
        — what the list endpoint (and, later, the ``auto`` catalogue) shows
        without the full record."""
        entries = ((self.record or {}).get("criteria") or {})
        out = []
        for name, entry in entries.items():
            expected = entry.get("expected") or {}
            out.append({
                "name": name,
                "type": (entry.get("input") or {}).get("type"),
                "guides_llm": bool(entry.get("guides_llm")),
                "usable": bool(entry.get("usable")),
                "verdict": expected.get("verdict"),
                "score": expected.get("score"),
                "region_source": entry.get("region_source"),
            })
        return out

    def summary(self) -> dict[str, Any]:
        """The list endpoint's row: everything but the record body."""
        return {
            "reference_id": self.id,
            "status": self.status,
            "title": self.title,
            "description": self.description,
            "description_source": self.description_source,
            "tags": list(self.tags),
            "job_id": self.job_id,
            "source": {
                "kind": self.source_kind,
                "job_id": self.source_job_id,
                "item": self.source_item,
            },
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "criteria": self.criteria_summary(),
        }
