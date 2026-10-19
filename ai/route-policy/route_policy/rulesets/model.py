"""A ruleset: the YAML / JSON document (pydantic) and its compiled form.

The document (``ai/route-policy/rulesets/default.yaml`` is the reference)::

    id: default                      # [a-z0-9][a-z0-9_-]*
    version: 1                       # immutable: new content = a new version
    description: ...
    mode: shadow                     # shadow | enforce
    routes: [local-general, local-code, local-reasoning, local-private]
    default_route: local-general     # one of routes
    evaluators:                      # in order; each votes or abstains
      - id: privacy
        type: regex_text             # GET /evaluator-types
        role: force                  # vote (default) | force | veto
        config: {patterns_ref: patterns/privacy.yaml}
        vote: {route: local-private, weight: 1.0}
    combiner: {type: priority}       # priority | weighted (+ its options)

Validation beyond the shapes below (routes, references, each type's config)
is ``rulesets.validate``; refs (``patterns_ref``) are inlined by
``rulesets.loader`` before either.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..combiners.base import Combiner
from ..evaluators.base import Evaluator

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ROUTE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"


def canonical_json(document: Any) -> str:
    """The document as one canonical JSON string: sorted keys, no
    whitespace. What the content hash is computed over."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(document: Any) -> str:
    """The identity of a ruleset version's CONTENT. Computed over the document
    as written (refs inlined) -- never over a pydantic dump, whose defaults
    would change the hash whenever the code gains a field."""
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


class VoteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Optional[str] = Field(None, pattern=ROUTE_PATTERN)
    weight: float = Field(1.0, ge=0.0, le=1000.0)


class EvaluatorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    type: str = Field(min_length=1, max_length=64)
    role: Literal["vote", "force", "veto"] = "vote"
    description: Optional[str] = Field(None, max_length=2000)
    config: Dict[str, Any] = Field(default_factory=dict)
    vote: VoteSpec = Field(default_factory=VoteSpec)


class CombinerSpec(BaseModel):
    """``{type: <name>, ...options}`` -- the options are validated by the
    combiner type's own Config model."""

    model_config = ConfigDict(extra="allow")

    type: str = "priority"

    def options(self) -> Dict[str, Any]:
        return dict(self.model_extra or {})


class RulesetDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    version: int = Field(ge=1)
    description: str = Field("", max_length=4000)
    mode: Literal["shadow", "enforce"]
    routes: List[str] = Field(min_length=1)
    default_route: str
    evaluators: List[EvaluatorEntry] = Field(default_factory=list)
    combiner: CombinerSpec = Field(default_factory=CombinerSpec)


@dataclass(frozen=True)
class CompiledEntry:
    id: str
    type: str
    role: str
    route: Optional[str]      # vote.route
    weight: float
    evaluator: Evaluator


@dataclass(frozen=True)
class CompiledRuleset:
    """A validated ruleset, ready to run: evaluators built and bound, the
    combiner built. Immutable, so one instance serves every request."""

    id: str
    version: int
    mode: str
    description: str
    routes: List[str]
    default_route: str
    entries: List[CompiledEntry]
    combiner: Combiner
    document: Dict[str, Any] = field(repr=False)   # the resolved document as stored
    sha256: str = ""

    def ref(self) -> Dict[str, Any]:
        return {"id": self.id, "version": self.version}
