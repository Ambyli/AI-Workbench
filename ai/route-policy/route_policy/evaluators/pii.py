"""pii -- sensitive data in the conversation forces local-private.

The rule (the user's, ROUTE_POLICY_PHASE5_PLAN § 10): a turn goes to
``local-private`` when the conversation carries SENSITIVE data -- an email,
a phone number, a street address, a government id, a card / bank / account
number (``force``) -- in any user message, or in the result of a DATA tool
(a Phoenix or Trino query carries customers' names and addresses into every
later turn's history). A person's name forces only together with a
``with_person`` type in the SAME text (a date of birth); a bare name ("my
name is Sam") never does, and neither does a place.

Config::

    scope:
      user_messages: all             # all: once sensitive data appears, the chat stays private
                                     # latest: only the latest user message
      tool_results: '(?i)(?:.*[-_])?(?:phoenix|trino)[-_].+'   # a FULL-match regex over tool names; null = none
    force: [email, phone, street_address, gov_id, card_number, bank_account, account_number]
    with_person: [date_of_birth]     # forces only beside a `person` in the same text
    ner: true                        # also read the sidecar's NER (the `pii` task)
    min_score: 0.5                   # an NER entity counts at max_score >= this
    model: gravitee-io/bert-small-pii-detection   # optional pin on the NER model

**Two layers, merged per text** (a type's count in a text is the larger of
the two):

* **patterns** -- ``common.pii``, in-process, ALWAYS: validated recognisers
  (Luhn for cards, SSN rules, IBAN mod-97, the ABA checksum, context words
  for account numbers and ids, US street suffixes and the Utah grid, PO
  boxes, obfuscated emails). So ``pii`` forces on every pattern type while
  the sidecar is down, slow, or cut by the budget. They scan at most
  :data:`PATTERN_MAX_CHARS` per decision: the user texts first, newest
  first, then the tool results newest first.
* **NER** -- the sidecar's ``pii`` task over the same texts (newest first,
  within the request caps): free-form addresses, ids with no fixed format,
  and the names the ``with_person`` rule needs. The sidecar maps its model's
  labels onto ``common.pii``'s vocabulary itself (a ``LOCATION`` span with a
  digit is a ``street_address``, one without is a ``location``) and never
  returns a span. A sidecar PII job finishes late and caches its texts, so a
  history cut on one call is complete on the next.

``person`` and ``location`` can be in neither list: alone they are never
sensitive. ``with_person`` therefore needs the NER (patterns find no names).

``detail`` holds types, counts, sources and statuses -- never a span or a
character of the text.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.pii import ENTITY_TYPES
from common.pii import counts as pattern_counts

from ..signals.model import SignalRequest, SignalText
from ._signals import lookup, number
from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares

# At most this many characters go through the patterns per decision (100 KB
# take well under 20 ms). A history past it: the newest user texts first,
# then the newest tool results.
PATTERN_MAX_CHARS = 200_000
# Never sensitive alone, so never a `force` or `with_person` type.
NEVER_FORCE = ("person", "location")
PERSON = "person"


class PiiScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_messages: Literal["all", "latest"] = "all"
    tool_results: Optional[str] = Field(None, min_length=1, max_length=2000)

    @field_validator("tool_results")
    @classmethod
    def _compiles(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"tool_results does not compile: {exc}") from exc
        return value


class PiiConfig(EvaluatorConfig):
    scope: PiiScope = PiiScope()
    force: List[str] = Field(min_length=1)
    with_person: List[str] = Field(default_factory=list)
    ner: bool = True
    min_score: float = Field(0.5, ge=0.0, le=1.0)
    model: Optional[str] = Field(None, min_length=1, max_length=256)

    @field_validator("force", "with_person")
    @classmethod
    def _types(cls, value: List[str]) -> List[str]:
        unknown = sorted(set(value) - set(ENTITY_TYPES))
        if unknown:
            raise ValueError(f"unknown entity type(s) {unknown}; expected some of {list(ENTITY_TYPES)}")
        never = sorted(set(value) & set(NEVER_FORCE))
        if never:
            raise ValueError(f"{never} is never sensitive alone and cannot force")
        if len(set(value)) != len(value):
            raise ValueError("an entity type is listed twice")
        return value

    @model_validator(mode="after")
    def _disjoint(self) -> "PiiConfig":
        both = sorted(set(self.force) & set(self.with_person))
        if both:
            raise ValueError(f"{both} is in both force and with_person")
        return self


def _add(into: Dict[str, int], counts: Dict[str, int]) -> None:
    for kind, n in counts.items():
        into[kind] = into.get(kind, 0) + n


@declares(OutputSpec(
    type="pii",
    summary="Sensitive data (email, phone, street address, government id, card / bank / account number) in "
            "the conversation's user messages or data-tool results: common.pii's patterns in-process, plus "
            "the signals sidecar's NER.",
    votes="vote.route (role force -> local-private) when a `force` type is found in a text in scope -- by "
          "the patterns or by the NER at max_score >= min_score (or with no score) -- or a `with_person` type "
          "in the same text "
          "as a person. A person or a place alone never votes. With the sidecar down, slow or cut it votes "
          "from the patterns alone. Abstains when nothing sensitive is found, or no text is in scope.",
    fields={
        "types": FieldSpec("{type: count} found in scope, both layers (per text the larger count)", "object"),
        "by_source": FieldSpec("{patterns: {type: count}, ner: {type: count}}", "object"),
        "forced_by": FieldSpec("The types that voted, sorted (`person+<type>` for a with_person pair)",
                               "array"),
        "forced_in": FieldSpec("Where they were found: latest_user | earlier_user | tool:<tool name>",
                               "array"),
        "texts_in_scope": FieldSpec("Non-empty texts in scope", "integer"),
        "texts_scanned": FieldSpec("Texts the patterns scanned (all of them unless past "
                                   f"{PATTERN_MAX_CHARS} characters)", "integer"),
        "chars_scanned": FieldSpec("Characters the patterns scanned", "integer"),
        "ner_status": FieldSpec("The NER task's status (evaluators/_signals.py): ok | partial | deadline | "
                                "skipped | warming | error | unavailable | not_sent | missing | timed out | "
                                "failed | not local | not fetched | conflict | model mismatch; off with "
                                "`ner: false`", "string"),
        "ner_unscanned_texts": FieldSpec("Texts in scope the NER did not scan completely; null with "
                                         "`ner: false`", "integer", nullable=True),
        "ner_model": FieldSpec("The NER model id the sidecar answered with", "string", nullable=True),
        "signals_ms": FieldSpec("The signals call's time; null when there was none", "number", nullable=True),
    },
    notes=(
        "`force` and `with_person` are common.pii entity types (email, phone, street_address, gov_id, "
        "card_number, bank_account, account_number, date_of_birth, other); person and location are refused.",
        "scope.tool_results matches the WHOLE tool name (re.fullmatch); a tool result is the text of an "
        "OpenAI `tool` message or an Anthropic `tool_result` block, named through its tool_call_id.",
        "No span or character of the text is ever returned: only types, counts, sources and statuses.",
    ),
))
class Pii(Evaluator):
    Config = PiiConfig

    def __init__(self, evaluator_id: str, config: PiiConfig) -> None:
        super().__init__(evaluator_id, config)
        self.tools = re.compile(config.scope.tool_results) if config.scope.tool_results else None
        self.force = set(config.force)
        self.with_person = list(config.with_person)

    # -- the texts ---------------------------------------------------------------------

    def _scope(self, ctx) -> List[Tuple[SignalText, str]]:
        """(text, where) for every non-empty text in scope: user texts
        newest first, then tool results newest first."""
        memo_key = ("pii", "scope", self.config.scope)
        cached = ctx.memo.get(memo_key)
        if cached is not None:
            return cached
        users = ctx.user_items if self.config.scope.user_messages == "all" else ctx.user_items[-1:]
        latest = ctx.user_items[-1].id if ctx.user_items else None
        out = [(item, "latest_user" if item.id == latest else "earlier_user")
               for item in reversed(users) if item.text]
        if self.tools is not None:
            out += [(item, f"tool:{name}") for item, name in reversed(ctx.tool_items)
                    if item.text and self.tools.fullmatch(name)]
        ctx.memo[memo_key] = out
        return out

    def _request(self, ctx) -> SignalRequest:
        return SignalRequest("pii", tuple(item for item, _ in self._scope(ctx)))

    def signal_tasks(self) -> Dict[str, str]:
        return {"pii": SignalRequest("pii").key} if self.config.ner else {}

    def signal_requests(self, ctx) -> List[SignalRequest]:
        if not self.config.ner or not self._scope(ctx):
            return []
        return [self._request(ctx)]

    # -- the vote ----------------------------------------------------------------------------

    def _ner_counts(self, features: Any) -> Dict[str, int]:
        entities = features.get("entities") if isinstance(features, dict) else None
        out: Dict[str, int] = {}
        if not isinstance(entities, dict):
            return out
        for kind, entity in entities.items():
            if not isinstance(entity, dict) or kind not in ENTITY_TYPES:
                continue
            # A model that gives no score (max_score absent) is not held to
            # min_score: on the safe side, its entity counts.
            count, score = entity.get("count"), number(entity.get("max_score"))
            if isinstance(count, int) and not isinstance(count, bool) and count > 0 \
                    and (score is None or score >= self.config.min_score):
                out[kind] = count
        return out

    async def evaluate(self, ctx) -> Outcome:
        scope = self._scope(ctx)

        # Patterns: always, in-process, within PATTERN_MAX_CHARS.
        patterns: Dict[str, Dict[str, int]] = {}
        budget, chars = PATTERN_MAX_CHARS, 0
        for item, _ in scope:
            if budget <= 0:
                break
            text = item.text[:budget]
            patterns[item.id] = {k: n for k, n in pattern_counts(text).items() if n}
            budget -= len(text)
            chars += len(text)

        # NER: what the sidecar answered, if it did.
        ner: Dict[str, Dict[str, int]] = {}
        ner_status, ner_model, unscanned, why_not = "off", None, None, None
        if self.config.ner:
            found = lookup(ctx, self._request(ctx), pin=self.config.model)
            ner_status, ner_model = found.status, found.model
            unscanned = 0
            for item, _ in scope:
                features = found.task.texts.get(item.id) if found.usable else None
                if isinstance(features, dict):
                    ner[item.id] = self._ner_counts(features)
                complete = (isinstance(features, dict) and features.get("complete", True) is not False
                            and item.id not in found.task.truncated)
                if not complete:
                    unscanned += 1
            if not found.usable and scope:
                why_not = f"patterns only: {found.why()}"
            elif unscanned:
                why_not = f"{unscanned} text(s) not scanned by NER"

        totals: Dict[str, int] = {}
        by_patterns: Dict[str, int] = {}
        by_ner: Dict[str, int] = {}
        forced, where = set(), set()
        person_seen = False
        for item, place in scope:
            pat, ent = patterns.get(item.id, {}), ner.get(item.id, {})
            _add(by_patterns, pat)
            _add(by_ner, ent)
            types = {k: max(pat.get(k, 0), ent.get(k, 0)) for k in set(pat) | set(ent)}
            _add(totals, {k: n for k, n in types.items() if n})
            hits = [k for k in types if types[k] and k in self.force]
            if types.get(PERSON):
                person_seen = True
                hits += [f"{PERSON}+{k}" for k in self.with_person if types.get(k)]
            if hits:
                forced.update(hits)
                where.add(place)

        detail = {
            "types": dict(sorted(totals.items())),
            "by_source": {"patterns": dict(sorted(by_patterns.items())), "ner": dict(sorted(by_ner.items()))},
            "forced_by": sorted(forced),
            "forced_in": sorted(where),
            "texts_in_scope": len(scope),
            "texts_scanned": len(patterns),
            "chars_scanned": chars,
            "ner_status": ner_status,
            "ner_unscanned_texts": unscanned,
            "ner_model": ner_model,
            "signals_ms": ctx.signals_ms,
        }
        tail = f" ({why_not})" if why_not else ""
        if not scope:
            return Outcome.abstain("no text in scope", detail)
        if forced:
            return Outcome.vote(f"sensitive data: {', '.join(sorted(forced))}{tail}", detail)
        if person_seen:
            return Outcome.abstain(f"a name without sensitive data{tail}", detail)
        return Outcome.abstain(f"no sensitive data{tail}", detail)
