"""nearest_examples -- the route of the labelled example prompts nearest to
the latest user message (embedding kNN in the signals sidecar).

Config::

    model: intfloat/multilingual-e5-small   # the embedding model the thresholds were calibrated for (pin)
    examples_ref: examples/routes.yaml       # inlined by the loader as `examples` (below)
    k: 5                                     # neighbours
    min_share: 0.8                           # the winning route's share of the top-k similarity mass
    min_similarity: 0.0                      # ... and its best neighbour's similarity at least this
    max_tokens: 256                          # the sidecar embeds the first max_tokens tokens
    scope: current_message

The example set is a file beside the rulesets (``rulesets/examples/``)::

    name: routes_v1
    examples:
      - {id: code-001, route: local-code, text: "my script throws an error when I run it"}

inlined by the loader as ``config.examples`` (``examples_ref`` -> ``examples``,
like ``patterns_ref`` -> ``rule``), so an edited example is new content: a
new ruleset version. Validated here: ids unique (``[a-z0-9][a-z0-9_-]*``),
every route one of the ruleset's (``check_routes``), each text 1-300
characters, at most 1,000 examples. Examples are curated and PII-free --
never real prompts, never their embeddings
(unit-tests/route-policy/test_default_signals.py runs ``common.pii`` over
every one).

**The vote.** The sidecar embeds the latest user text, takes the ``k``
nearest examples by cosine similarity, and reports each route's ``share``
of their similarity mass and its ``best`` similarity. The top-share route
wins when its share >= ``min_share`` (and its best >= ``min_similarity``);
confidence = the share. A share threshold, not a raw similarity: e5's
similarities are compressed ("ok" and "?" reach 0.82-0.85), and noise
spreads its mass across routes (share ~0.4), so it abstains. The route is
the evaluator's own (``needs_vote_route = False``, like ``llm_classifier``).

**The fingerprint** -- SHA-256 over the sorted (id, route, text) triples and
the pinned model id, ``common.signals.fingerprint_examples`` (the one
definition the sidecar shares) -- names the example set to the sidecar, which embeds a
set once per fingerprint and answers ``warming`` (abstain) until it has;
route-policy warms every bound ruleset's set at startup
(``signal_warmups``). An answer from another embedding model than ``model``
is refused (``model mismatch``): the thresholds belong to the model.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.signals import MAX_EXAMPLE_CHARS, MAX_EXAMPLES, ExampleSet as WireExampleSet
from common.signals import Example as WireExample
from common.signals import fingerprint_examples

from ..signals.model import SignalRequest
from ._signals import lookup, number
from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares

EXAMPLE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# The sidecar's caps (common.signals), so a set that validates here is never a 413.
EXAMPLE_MAX_CHARS = MAX_EXAMPLE_CHARS
TASK = "nearest"


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=EXAMPLE_ID_PATTERN)
    route: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=EXAMPLE_MAX_CHARS)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("an example's text is blank")
        return value


class ExampleSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    examples: List[Example] = Field(min_length=1, max_length=MAX_EXAMPLES)

    @field_validator("examples")
    @classmethod
    def _unique(cls, value: List[Example]) -> List[Example]:
        seen = set()
        for example in value:
            if example.id in seen:
                raise ValueError(f"example id {example.id!r} is used twice")
            seen.add(example.id)
        return value


class NearestExamplesConfig(EvaluatorConfig):
    model: str = Field(min_length=1, max_length=256)
    examples: ExampleSet
    # Provenance only: the file the loader inlined as `examples`.
    examples_ref: Optional[str] = None
    k: int = Field(5, ge=1, le=50)
    min_share: float = Field(0.8, ge=0.0, le=1.0)
    min_similarity: float = Field(0.0, ge=-1.0, le=1.0)
    max_tokens: int = Field(256, ge=16, le=512)
    scope: Literal["current_message"] = "current_message"


def wire_examples(config: NearestExamplesConfig) -> List[WireExample]:
    return [WireExample(e.id, e.route, e.text) for e in config.examples.examples]


def fingerprint(config: NearestExamplesConfig) -> str:
    """SHA-256 (hex) over the sorted (id, route, text) triples and the
    pinned model id (``common.signals.fingerprint_examples``): the
    sidecar's cache key for the embedded set."""
    return fingerprint_examples(wire_examples(config), config.model)


@declares(OutputSpec(
    type="nearest_examples",
    summary="The route of the curated example prompts nearest to the latest user message: embedding kNN "
            "in the signals sidecar (`nearest` task), over an example set versioned with the ruleset.",
    votes="The top-k route (chosen by the evaluator; vote.route is refused) when its share of the top-k "
          "similarity mass is >= min_share and its best similarity >= min_similarity; confidence = the "
          "share. Abstains below that, when the latest user message has no text, while the sidecar is "
          "still embedding the set (warming), on a timeout or any other missing answer, and on an "
          "answer from another model than `model`.",
    fields={
        "model": FieldSpec("The embedding model the sidecar answered with (null when it did not)", "string",
                           nullable=True),
        "status": FieldSpec("The nearest task's status (evaluators/_signals.py)", "string"),
        "fingerprint": FieldSpec("SHA-256 of the example set + model pin", "string"),
        "neighbours": FieldSpec("The k nearest examples [{id, route, similarity}], nearest first", "array"),
        "route_shares": FieldSpec("{route: share of the top-k similarity mass}", "object"),
        "best_similarity": FieldSpec("The nearest example's similarity; null when unscored", "number",
                                     nullable=True),
        "signals_ms": FieldSpec("The signals call's time; null when there was none", "number", nullable=True),
    },
    notes=(
        "examples: {name, examples: [{id, route, text}]} -- usually `examples_ref: examples/<file>.yaml`, "
        f"inlined by the loader. Ids unique, routes among the ruleset's, texts <= {EXAMPLE_MAX_CHARS} "
        f"characters, <= {MAX_EXAMPLES} examples. Curated and PII-free: never real prompts.",
    ),
))
class NearestExamples(Evaluator):
    Config = NearestExamplesConfig
    needs_vote_route = False

    def __init__(self, evaluator_id: str, config: NearestExamplesConfig) -> None:
        super().__init__(evaluator_id, config)
        self.example_set = WireExampleSet.build(wire_examples(config), model=config.model)
        self.fingerprint = self.example_set.fingerprint
        self.order: List[str] = list(dict.fromkeys(e.route for e in config.examples.examples))

    def check_routes(self, routes: Sequence[str]) -> None:
        unknown = sorted({e.route for e in self.config.examples.examples} - set(routes))
        if unknown:
            raise ValueError(f"examples name {unknown}, which are not routes of this ruleset")
        self.order = list(routes)

    def _params(self) -> Dict[str, Any]:
        return {"k": self.config.k, "max_tokens": self.config.max_tokens, "examples": self.example_set}

    def _request(self, ctx) -> Optional[SignalRequest]:
        latest = ctx.user_items[-1] if ctx.user_items else None
        if latest is None or not latest.text:
            return None
        return SignalRequest(TASK, (latest,), self._params(), identity=self._identity())

    def _identity(self) -> str:
        return f"{self.fingerprint}:{self.config.k}:{self.config.max_tokens}"

    def signal_requests(self, ctx) -> List[SignalRequest]:
        request = self._request(ctx)
        return [request] if request is not None else []

    def signal_warmups(self) -> List[SignalRequest]:
        return [SignalRequest(TASK, (), self._params(), identity=self._identity())]

    def signal_tasks(self) -> Dict[str, str]:
        return {TASK: self.signal_warmups()[0].key}

    def _detail(self, status: str, model: Optional[str], neighbours: List[dict], shares: Dict[str, float],
                best: Optional[float], ctx) -> dict:
        return {"model": model, "status": status, "fingerprint": self.fingerprint, "neighbours": neighbours,
                "route_shares": shares, "best_similarity": best, "signals_ms": ctx.signals_ms}

    @staticmethod
    def _neighbours(features: dict) -> List[dict]:
        out = []
        for n in features.get("neighbours") or []:
            if isinstance(n, dict) and isinstance(n.get("id"), str) and isinstance(n.get("route"), str):
                similarity = number(n.get("similarity"))
                if similarity is not None:
                    out.append({"id": n["id"], "route": n["route"], "similarity": round(similarity, 6)})
        return out

    @staticmethod
    def _shares(features: dict, neighbours: List[dict]) -> Dict[str, Dict[str, float]]:
        """{route: {share, best}} -- the sidecar's, else computed from the
        neighbours (similarity mass, the negatives counted as nothing)."""
        routes = features.get("routes")
        out: Dict[str, Dict[str, float]] = {}
        if isinstance(routes, dict) and routes:
            for route, entry in routes.items():
                share = number(entry.get("share")) if isinstance(entry, dict) else None
                if share is not None:
                    out[route] = {"share": share, "best": number(entry.get("best")) or 0.0}
            return out
        mass = {}
        for n in neighbours:
            mass[n["route"]] = mass.get(n["route"], 0.0) + max(0.0, n["similarity"])
            out.setdefault(n["route"], {"share": 0.0, "best": n["similarity"]})
            out[n["route"]]["best"] = max(out[n["route"]]["best"], n["similarity"])
        total = sum(mass.values())
        for route in out:
            out[route]["share"] = mass.get(route, 0.0) / total if total > 0 else 0.0
        return out

    async def evaluate(self, ctx) -> Outcome:
        request = self._request(ctx)
        if request is None:
            return Outcome.abstain("no user text", self._detail("not_sent", None, [], {}, None, ctx))
        found = lookup(ctx, request, pin=self.config.model)
        features = found.task.texts.get(request.texts[0].id) if found.usable else None
        if not isinstance(features, dict):
            return Outcome.abstain(found.why() if not found.usable else "the latest user text was not scored",
                                   self._detail(found.status, found.model, [], {}, None, ctx))
        neighbours = self._neighbours(features)
        shares = self._shares(features, neighbours)
        route_shares = {r: round(v["share"], 6) for r, v in shares.items()}
        best = neighbours[0]["similarity"] if neighbours else None
        detail = self._detail(found.status, found.model, neighbours, route_shares, best, ctx)
        if not shares:
            return Outcome.abstain("no neighbours", detail)
        rank = {r: i for i, r in enumerate(self.order)}
        winner = min(shares, key=lambda r: (-shares[r]["share"], rank.get(r, len(rank))))
        share, top = shares[winner]["share"], shares[winner]["best"]
        if winner not in rank:
            return Outcome.abstain(f"nearest examples are {winner}, not a route of this ruleset", detail)
        if share < self.config.min_share:
            return Outcome.abstain(f"{winner} has {share:.2f} of the top {self.config.k}, below "
                                   f"{self.config.min_share:g}", detail)
        if top < self.config.min_similarity:
            return Outcome.abstain(f"{winner}'s nearest example is {top:.3f} similar, below "
                                   f"{self.config.min_similarity:g}", detail)
        return Outcome.vote(f"{winner}: {share:.2f} of the top {self.config.k} nearest examples", detail,
                            confidence=round(share, 6), route=winner)
