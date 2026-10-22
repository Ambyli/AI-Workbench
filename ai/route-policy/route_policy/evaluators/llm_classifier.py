"""llm_classifier -- a vote from a model's reading of the conversation,
classified in the background and applied from the next turn on.

Config::

    model: qwen3.5-4b            # a document-classifier registry id (GET /models on the classifier)
    criteria:                    # route -> the question /assess scores 1-10 (hint: presence);
                                 # a tie goes to the route listed first in the ruleset's `routes`
      local-code: "The latest user request asks to write, fix, change or explain source code, ..."
      local-reasoning: "..."
    otherwise: local-general     # no criterion reaches min_score -> this route; null = abstain
    min_score: 7                 # the classifier's PASS line
    history: {exchanges: 4, max_chars: 6000}   # what the model reads; max_chars <= the model's text_char_budget
    max_age_exchanges: 2         # a verdict may be this many user messages old

**How a verdict is made** (never on the request path): ``/decide`` asks this
evaluator for :meth:`analysis_requests` AFTER answering, and the analysis
queue (``analysis.queue``) sends the conversation -- rendered by
``common.conversation.render_transcript`` up to the latest user message, the
last ``history.exchanges`` exchanges, at most ``history.max_chars``
characters -- through the document classifier's ``POST /assess``, one
``llm`` criterion per route. ``analysis.verdict.choose`` turns the scores
into a route, and the result is one ``analyses`` row.

**How it is used**: at ``/decide`` for user message n, :meth:`analysis_lookups`
names the verdict keys of the prefixes ending at user messages n, n-1, ...
n - ``max_age_exchanges``; the service reads them in ONE query (within
``DB_READ_BUDGET_S``) into ``ctx.analyses``, and :meth:`evaluate` votes the
NEWEST usable verdict -- usually the previous message's, since this
message's classification starts only after this answer. Its route is the
verdict's (``needs_vote_route = False``), its confidence the verdict's.

**The key is the conversation itself**: ``input_key = sha256(fingerprint,
transcript)``, the fingerprint covering everything that changes a verdict
(model, criteria, otherwise, min_score, history). So:

* tool rounds of one user message render the same prefix -- one
  classification, however many ``/decide`` calls;
* an edited or regenerated earlier message changes every later prefix, and
  an old verdict can never be read for the new text;
* a caller without an Open WebUI chat id (Claude Code on ``/v1/messages``)
  works the same way;
* editing the criteria or the model (a new ruleset version) starts from
  scratch rather than reading verdicts made under the old questions.

**Background tasks are never classified.** Open WebUI's title / tags /
follow-up requests reach ``auto`` too (``background_task`` on /decide, the
LiteLLM hook's verdict); each would cost a whole classification of a prompt
that is not a chat turn. For one, :meth:`analysis_lookups` and
:meth:`analysis_requests` return nothing and :meth:`evaluate` abstains
(``background task``).

**Nothing depends on the order of the ``criteria`` mapping**: a ruleset
stored in route-policy-db is jsonb, which keeps object keys in its own order,
not the file's. So the fingerprint sorts the criteria by route, and the tie
order is the ruleset's ``routes`` list (an array, kept as written) -- the
weighted combiner's tie rule too. A copy compiling the file and one
compiling the stored document make the same keys and the same choices.

Pure apart from the hashing: no network, no database -- so a ruleset that
uses it compiles at image build time, and the pipeline stays a function of
the request (plus the verdicts the service put on it).
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.conversation import render_transcript, user_message_count

from ..analysis.model import AnalysisRequest, Verdict
from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares

CRITERION_MAX_CHARS = 200


class HistoryConfig(BaseModel):
    """What the classifier reads: the last ``exchanges`` exchanges up to the
    user message being classified, cut to ``max_chars`` (oldest first; the
    latest user message is always kept)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exchanges: int = Field(4, ge=1, le=100)
    max_chars: int = Field(6000, ge=200, le=200_000)


class LlmClassifierConfig(EvaluatorConfig):
    model: str = Field(min_length=1, max_length=128)
    # route -> criterion text. The tie order is the ruleset's `routes`
    # (LlmClassifier.order), never this mapping's order.
    criteria: Dict[str, str] = Field(min_length=1, max_length=16)
    otherwise: Optional[str] = None
    min_score: int = Field(7, ge=1, le=10)
    history: HistoryConfig = HistoryConfig()
    max_age_exchanges: int = Field(2, ge=0, le=100)

    @field_validator("criteria")
    @classmethod
    def _criteria(cls, value: Dict[str, str]) -> Dict[str, str]:
        texts = []
        for route, text in value.items():
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"criterion for {route!r} is empty")
            if len(text) > CRITERION_MAX_CHARS:
                raise ValueError(f"criterion for {route!r} is {len(text)} characters; "
                                 f"at most {CRITERION_MAX_CHARS} (the classifier's criterion name cap)")
            texts.append(text)
        if len(set(texts)) != len(texts):
            raise ValueError("two routes have the same criterion text")
        return value

    @model_validator(mode="after")
    def _otherwise(self) -> "LlmClassifierConfig":
        if self.otherwise is not None and self.otherwise in self.criteria:
            raise ValueError(f"otherwise {self.otherwise!r} is also a criterion route")
        return self


def fingerprint(config: LlmClassifierConfig) -> str:
    """SHA-256 (hex) over everything that changes a verdict: the model, the
    criteria (sorted by route, so the mapping's order -- which jsonb does not
    keep -- never matters), ``otherwise``, ``min_score`` and ``history``.
    ``max_age_exchanges`` is not in it -- it only decides which verdicts are
    read, not what one says."""
    payload = {
        "model": config.model,
        "criteria": sorted([route, text] for route, text in config.criteria.items()),
        "otherwise": config.otherwise,
        "min_score": config.min_score,
        "history": {"exchanges": config.history.exchanges, "max_chars": config.history.max_chars},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def input_key(fingerprint: str, transcript: str) -> str:
    """The verdict key of one rendered transcript (SHA-256 hex)."""
    return hashlib.sha256(f"{fingerprint}\n{transcript}".encode("utf-8")).hexdigest()


@declares(OutputSpec(
    type="llm_classifier",
    summary="Votes the route a local model chose for the conversation -- classified in the background "
            "through the document classifier's POST /assess (one presence criterion per route), and "
            "applied from the next turn on.",
    votes="The route of the newest usable verdict among the prefixes ending at the latest user message "
          "and the `max_age_exchanges` before it (confidence: the verdict's). Abstains when no such "
          "verdict exists yet, when the newest found failed and no older one is usable, when the newest "
          "usable one has no route (nothing reached min_score and no `otherwise`), when the lookup "
          "timed out, or for an Open WebUI background task (`background_task` on /decide: reason "
          "`background task`, nothing looked up or classified).",
    fields={
        "model": FieldSpec("The classifier registry id the verdicts come from", "string"),
        "user_messages": FieldSpec("User messages in the conversation (n)", "integer"),
        "looked_up": FieldSpec("Verdict keys looked up: the prefixes ending at user messages n, n-1, ... "
                               "n - max_age_exchanges", "integer"),
        "lookup_error": FieldSpec("Why the verdicts could not be read (timed out | failed | "
                                  "unavailable (no database)); null when they were", "string", nullable=True),
        "input_key": FieldSpec("The key of the verdict reported (used, or the failed / routeless one "
                               "that made it abstain); null when none was found", "string", nullable=True),
        "exchange_index": FieldSpec("Which user message that verdict's prefix ends at (1-based)",
                                    "integer", nullable=True),
        "age_exchanges": FieldSpec("How many user messages old that verdict is (0 = this one)",
                                   "integer", nullable=True),
        "status": FieldSpec("That verdict's status: ok | incomplete | unavailable | refused | failed | "
                            "timeout | not_local", "string", nullable=True),
        "scores": FieldSpec("That verdict's {route: 1-10 score} (null for an errored criterion)",
                            "object", nullable=True),
    },
    notes=(
        "criteria keys and `otherwise` must be routes of the ruleset; criterion texts are at most 200 "
        "characters and unique; a tie between criteria goes to the route listed first in the ruleset's "
        "`routes`; `vote.route` is not used (the verdict picks the route).",
        "The model must be LOCAL: the analysis queue submits nothing to a model whose GET /models entry "
        "is authenticated or whose endpoint host resolves to anything outside the private / loopback / "
        "link-local networks (status not_local).",
        "No conversation text is stored: verdicts are keyed by a hash of the transcript, and an analyses "
        "row keeps the scores, the route, usage and the winning criterion's reason (<= 200 characters).",
    ),
))
class LlmClassifier(Evaluator):
    Config = LlmClassifierConfig
    needs_vote_route = False

    def __init__(self, evaluator_id: str, config: LlmClassifierConfig) -> None:
        super().__init__(evaluator_id, config)
        self.fingerprint = fingerprint(config)
        # The tie order: the ruleset's `routes` once compiled (check_routes);
        # the mapping's own order for an evaluator built on its own.
        self.order: List[str] = list(config.criteria)

    def check_routes(self, routes: Sequence[str]) -> None:
        unknown = [r for r in self.config.criteria if r not in routes]
        if self.config.otherwise is not None and self.config.otherwise not in routes:
            unknown.append(self.config.otherwise)
        if unknown:
            raise ValueError(f"criteria / otherwise name {unknown}, which are not routes of this ruleset")
        self.order = [r for r in routes if r in self.config.criteria]

    # -- the prefixes ----------------------------------------------------------------

    def _prefixes(self, ctx) -> List[Tuple[int, str, int]]:
        """(k, input key, transcript length) for k = n, n-1, ... n - max_age
        (k >= 1), newest first; prefixes that render empty are left out.
        Computed once per request (``ctx.memo``)."""
        memo_key = ("llm_classifier", self.fingerprint, self.config.max_age_exchanges)
        cached = ctx.memo.get(memo_key)
        if cached is not None:
            return cached
        n = user_message_count(ctx.messages)
        history = self.config.history
        out: List[Tuple[int, str, int]] = []
        for k in range(n, max(1, n - self.config.max_age_exchanges) - 1, -1):
            text = render_transcript(ctx.messages, exchanges=history.exchanges,
                                     max_chars=history.max_chars, upto_user=k)
            if text:
                out.append((k, input_key(self.fingerprint, text), len(text)))
        ctx.memo[memo_key] = out
        return out

    def analysis_lookups(self, ctx) -> List[str]:
        if ctx.background_task:
            return []
        return [key for _, key, _ in self._prefixes(ctx)]

    def analysis_requests(self, ctx) -> List[AnalysisRequest]:
        """One request: the prefix ending at the latest user message --
        unless its verdict is already on the context, or this is a
        background task."""
        if ctx.background_task:
            return []
        prefixes = self._prefixes(ctx)
        n = user_message_count(ctx.messages)
        if not prefixes or prefixes[0][0] != n or prefixes[0][1] in ctx.analyses:
            return []
        history = self.config.history
        text = render_transcript(ctx.messages, exchanges=history.exchanges, max_chars=history.max_chars,
                                 upto_user=n)
        return [AnalysisRequest(
            input_key=prefixes[0][1],
            fingerprint=self.fingerprint,
            model=self.config.model,
            criteria={route: self.config.criteria[route] for route in self.order},
            otherwise=self.config.otherwise,
            min_score=self.config.min_score,
            exchange_index=n,
            input_chars=len(text),
            evaluator_id=self.id,
            text=text,
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
        )]

    # -- the vote -------------------------------------------------------------------------

    def _detail(self, n: int, looked_up: int, lookup_error: Optional[str],
                verdict: Optional[Verdict]) -> Dict[str, object]:
        return {
            "model": self.config.model,
            "user_messages": n,
            "looked_up": looked_up,
            "lookup_error": lookup_error,
            "input_key": verdict.input_key if verdict else None,
            "exchange_index": verdict.exchange_index if verdict else None,
            "age_exchanges": n - verdict.exchange_index if verdict else None,
            "status": verdict.status if verdict else None,
            "scores": dict(verdict.scores) if verdict else None,
        }

    @staticmethod
    def _age(age: int) -> str:
        return "this message" if age == 0 else f"{age} message(s) ago"

    async def evaluate(self, ctx) -> Outcome:
        n = user_message_count(ctx.messages)
        if ctx.background_task:
            return Outcome.abstain("background task", self._detail(n, 0, None, None))
        prefixes = self._prefixes(ctx)
        if n == 0 or not prefixes:
            return Outcome.abstain("no user message", self._detail(n, 0, None, None))
        if ctx.analysis_lookup_error is not None:
            return Outcome.abstain(f"lookup {ctx.analysis_lookup_error}",
                                   self._detail(n, len(prefixes), ctx.analysis_lookup_error, None))
        analyses: Mapping[str, Verdict] = ctx.analyses
        failed: Optional[Verdict] = None
        for k, key, _ in prefixes:
            verdict = analyses.get(key)
            if verdict is None:
                continue
            if not verdict.usable:
                failed = failed or verdict
                continue
            detail = self._detail(n, len(prefixes), None, verdict)
            if verdict.route is None:
                return Outcome.abstain(f"verdict {self._age(n - k)}: no criterion reached "
                                       f"{self.config.min_score}/10", detail)
            score = verdict.scores.get(verdict.route)
            how = (f"{verdict.route} scored {score}/10" if score is not None and verdict.route in self.config.criteria
                   else f"no criterion reached {self.config.min_score}/10, otherwise {verdict.route}")
            return Outcome.vote(f"{how} ({self.config.model}, {self._age(n - k)})", detail,
                                confidence=verdict.confidence if verdict.confidence is not None else 0.0,
                                route=verdict.route)
        if failed is not None:
            return Outcome.abstain(f"verdict failed: {failed.status}",
                                   self._detail(n, len(prefixes), None, failed))
        return Outcome.abstain(f"no verdict in the last {len(prefixes)} message(s)",
                               self._detail(n, len(prefixes), None, None))
