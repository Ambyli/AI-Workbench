"""A criteria set: one ``llm_classifier`` entry of a ruleset file.

``run.py`` compares candidate MODELS on candidate CRITERIA SETS. A criteria
set is named by a ruleset file and an evaluator id in it
(``--ruleset rulesets/default-llm.yaml --evaluator topic-llm``); the file is
loaded and compiled with route-policy's own loader and validator (so the
benchmark can only ever run a criteria set route-policy would accept), and
the entry's validated ``LlmClassifierConfig`` supplies:

* ``criteria`` -- route -> the question ``/assess`` scores 1-10. The tie
  order ``verdict.choose`` uses is NOT this mapping's order but the
  compiled evaluator's ``order`` (the ruleset's ``routes``), exactly as in
  production, where the stored jsonb document does not keep key order;
* ``otherwise`` and ``min_score`` -- how scores become a route;
* ``history`` -- how a point is rendered (``render_transcript``'s
  ``exchanges`` / ``max_chars``).

Its ``model`` is the ruleset's production choice and is ignored: the model is
the benchmark's other axis (``--models``).

**Identity.** ``id`` is ``<ruleset>@<version>/<evaluator>``, what the report
prints. ``fingerprint`` is a SHA-256 over what decides a classification --
the criteria (in tie order), ``otherwise``, ``min_score`` and ``history`` --
so an edited file that kept its version is still a different criteria set:
``run.py`` resumes by (point, model, fingerprint), and the report keeps the
two apart.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from route_policy.evaluators.llm_classifier import LlmClassifierConfig

from .baseline import load_ruleset

LLM_CLASSIFIER_TYPE = "llm_classifier"


@dataclass(frozen=True)
class CriteriaSet:
    id: str                         # "<ruleset>@<version>/<evaluator>"
    fingerprint: str                # sha256 hex
    config: LlmClassifierConfig
    order: Tuple[str, ...]          # the criteria routes in tie order (LlmClassifier.order)

    @property
    def routes(self) -> List[str]:
        """The routes asked about, in tie order."""
        return list(self.order)

    @property
    def texts(self) -> List[str]:
        """The criterion texts, in the same order -- what /assess is sent."""
        return [self.config.criteria[route] for route in self.routes]

    @property
    def otherwise(self) -> Optional[str]:
        return self.config.otherwise

    @property
    def min_score(self) -> int:
        return self.config.min_score

    @property
    def exchanges(self) -> int:
        return self.config.history.exchanges

    @property
    def max_chars(self) -> int:
        return self.config.history.max_chars


def fingerprint(config: LlmClassifierConfig, order: Optional[Sequence[str]] = None) -> str:
    """``order`` defaults to the mapping's own order (an evaluator built on
    its own does the same)."""
    order = list(config.criteria) if order is None else list(order)
    history: Dict[str, Any] = config.history.model_dump(mode="json")
    body = {
        "criteria": [[route, config.criteria[route]] for route in order],
        "otherwise": config.otherwise,
        "min_score": config.min_score,
        "history": history,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def from_config(set_id: str, config: LlmClassifierConfig,
                order: Optional[Sequence[str]] = None) -> CriteriaSet:
    order = tuple(config.criteria) if order is None else tuple(order)
    return CriteriaSet(set_id, fingerprint(config, order), config, order)


def load_criteria_set(ruleset_path: str, evaluator_id: str) -> CriteriaSet:
    """The ``evaluator_id`` entry of the ruleset at ``ruleset_path``; raises
    ``ValueError`` when it is missing or not an ``llm_classifier``."""
    ruleset = load_ruleset(ruleset_path)
    entries = {e.id: e for e in ruleset.entries}
    entry = entries.get(evaluator_id)
    if entry is None:
        raise ValueError(f"{ruleset.id}@{ruleset.version}: no evaluator {evaluator_id!r}; "
                         f"it has {sorted(entries)}")
    if entry.type != LLM_CLASSIFIER_TYPE:
        raise ValueError(f"{ruleset.id}@{ruleset.version}: evaluator {evaluator_id!r} is a {entry.type}, "
                         f"not an {LLM_CLASSIFIER_TYPE}")
    config = entry.evaluator.config
    if not isinstance(config, LlmClassifierConfig):
        config = LlmClassifierConfig.model_validate(config)
    # The compiled evaluator's tie order (the ruleset's `routes`), not the
    # mapping's -- what production's analysis queue sends and chooses with.
    return from_config(f"{ruleset.id}@{ruleset.version}/{evaluator_id}", config, entry.evaluator.order)
