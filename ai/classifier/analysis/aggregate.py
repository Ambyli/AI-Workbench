"""Two-level aggregation: a criterion's per-item results → one answer.

Every criterion runs once per ITEM (one page of one document), so it has as
many results as the request has pages. ``options.aggregate`` says how they
collapse, in two steps:

    pages      each document's items → that document's answer
    documents  the documents' answers → the criterion's answer

A ``text`` criterion with ``options.scope: "document"`` already has one unit
per document, so only the documents step applies (``aggregate_used`` says
so).

    any          the best member (highest score, the first on a tie)
    worst / all  the lowest member — every member must pass. ``all`` is an
                 alias of ``worst``: the same rule, spelled for a reader who
                 thinks "all pages must pass"
    mean         the mean of the scores; the score is the mean rounded to the
                 nearest integer and clamped to 1-10 (the same rounding the
                 weighted overall score uses) and the verdict comes from it
                 via ``utils.verdict_from_score``
    sum          ``text`` only: the members' hit counts added, then scored
                 against ``min_count`` by ``text_eval.score_text`` — the
                 evaluator's own rubric, so the two cannot drift

Defaults (``api.criterion_options.default_aggregate``): llm presence and
detector ``any`` / ``all``; llm quality / auto and cv ``worst`` / ``worst``;
text ``sum`` / ``sum``.

The rules around the rules:

  * **One member passes straight through.** A level with one member returns
    that member's outcome unchanged — which is why a single single-page
    document reproduces the pre-aggregation result exactly.
  * **Skipped and errored members are excluded.** They have no score to
    count. Any errored member makes the answer INCOMPLETE (``complete:
    False``), which the weighting turns into an incomplete assessment. No ok
    member at all: an error if any member errored, else skipped — so a
    criterion skipped on every item is itself skipped.
  * **Geometry is always the union.** Every member's regions are kept (each
    already carries its own ``page = item``), whatever the rule picked, and
    whether or not the criterion is scored: a ``score: false`` criterion
    aggregates geometry and never a judgement.
  * **The LLM loop's record merges.** One member: its ``localization`` as-is.
    Several: every attempt, tagged with its ``item``, the total ``calls``,
    and ``accepted`` listing ``{"item", "attempt"}`` per accepted box
    (``accepted_attempt`` is null — an attempt number means nothing across
    items).

    aggregate_level()     — one level, over labelled members.
    pages_outcome()       — one document's pages, for per-item gating of a
                            document-scope dependant (``analysis.scheduler``).
    aggregate_criterion() — both levels; the per-document answers, the final
                            answer, and ``aggregate_used``.

Process flow position: called by ``analysis.pipeline`` after the scheduler,
and by the scheduler itself for a mixed-scope dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from analysis.outcome import Outcome
from analysis.result_specs import aggregated_detail, member_value, with_metric
from analysis.text_eval import score_text
from api.schemas import CriterionInput
from utils import verdict_from_score


@dataclass
class Member:
    """One input to a level: a label for the reason, the item it is (pages
    level only), and its outcome."""

    label: str
    item: Optional[int]
    outcome: Outcome


def _rule(rule: str) -> str:
    return "worst" if rule == "all" else rule


def _merged_localization(members: list[Member]) -> Optional[dict]:
    located = [m for m in members if m.outcome.localization is not None]
    if not located:
        return None
    attempts: list[dict] = []
    accepted: list[dict] = []
    calls = 0
    for m in located:
        loc = m.outcome.localization
        calls += int(loc.get("calls") or 0)
        for attempt in loc.get("attempts", []):
            attempts.append(attempt if m.item is None else {"item": m.item, **attempt})
        if m.item is None:
            accepted.extend(loc.get("accepted", []))
        elif loc.get("accepted_attempt") is not None:
            accepted.append({"item": m.item, "attempt": loc["accepted_attempt"]})
    return {"attempts": attempts, "accepted_attempt": None, "accepted": accepted, "calls": calls}


def _union(members: list[Member]) -> list:
    return [r for m in members for r in m.outcome.regions]


def aggregate_level(
    c: CriterionInput, rule: str, members: list[Member], noun: str
) -> Outcome:
    """Collapse one level's members under ``rule``. ``noun`` is "item" or
    "document", for the reason sentence."""
    if len(members) == 1:
        return members[0].outcome

    ok = [m for m in members if m.outcome.status == "ok"]
    errored = [m for m in members if m.outcome.status == "error"]
    complete = not errored and all(m.outcome.complete for m in members)
    regions = _union(members)
    localization = _merged_localization(members)
    first = members[0].outcome

    if not ok:
        if errored:
            e = errored[0]
            return Outcome(
                status="error",
                method=e.outcome.method,
                reason=f"No {noun} could be evaluated; {e.label}: {e.outcome.reason}",
                error=e.outcome.error,
                regions=regions,
                localization=localization,
                complete=False,
            )
        return Outcome(status="skipped", method=first.method, reason=first.reason)

    scored = [m for m in ok if isinstance(m.outcome.score, (int, float))]
    tail = (
        f" ({len(errored)} {noun}(s) errored and are excluded)" if errored else ""
    )
    level = "pages" if noun == "item" else "documents"
    method = ok[0].outcome.method
    if not scored:
        # score: false — geometry only, never a judgement. The detail keeps
        # its shape (the first located member's) with the members' values.
        rep = ok[0].outcome
        return replace(
            rep,
            score=None, verdict=None, confidence=None,
            reason=f"Located on {sum(1 for m in ok if m.outcome.regions)} of "
                   f"{len(ok)} {noun}(s){tail}. {ok[0].label}: {rep.reason or ''}".strip(),
            detail=aggregated_detail(
                method=method, rule=rule, level=level,
                members=[(m.label, m.outcome.detail) for m in ok],
                chosen=(ok[0].label, rep.detail),
            ),
            regions=regions,
            localization=localization,
            text_layer=None,
            complete=complete,
        )

    which = _rule(rule)
    members = [(m.label, m.outcome.detail) for m in scored]
    if which in ("any", "worst"):
        pick = max if which == "any" else min
        chosen = pick(scored, key=lambda m: m.outcome.score)
        rep = chosen.outcome
        return replace(
            rep,
            reason=f"{rule} of {len(scored)} {noun}(s) → {chosen.label}{tail}: {rep.reason or ''}",
            # The chosen member's detail, whole, plus how it was chosen.
            detail=aggregated_detail(
                method=method, rule=rule, level=level, members=members,
                chosen=(chosen.label, rep.detail),
            ),
            regions=regions,
            localization=localization,
            text_layer=None,
            complete=complete,
        )

    if which == "mean":
        scores = [float(m.outcome.score) for m in scored]
        mean = sum(scores) / len(scores)
        score = max(1, min(10, round(mean)))
        confidences = [m.outcome.confidence for m in scored if m.outcome.confidence is not None]
        # `value` is the mean of the members' headline numbers — their
        # measurement (cv), count (text), best box (detector) or score (llm).
        values = [float(v) for v in (member_value(d) for _, d in members)
                  if isinstance(v, (int, float))]
        return Outcome(
            status="ok",
            method=method,
            score=score,
            verdict=verdict_from_score(score),
            confidence=round(sum(confidences) / len(confidences)) if confidences else None,
            reason=f"mean of {len(scored)} {noun} score(s) = {mean:.2f}{tail}.",
            detail=aggregated_detail(
                method=method, rule=rule, level=level, members=members,
                value=round(sum(values) / len(values), 4) if values else None,
            ),
            regions=regions,
            localization=localization,
            complete=complete,
        )

    # sum — text only (refused at submit on every other type).
    return _sum(c, scored, noun, level, rule, regions, localization, complete, tail)


def _sum(c, scored, noun, level, rule, regions, localization, complete, tail) -> Outcome:
    opts = c.resolved_options()
    counts = {m.label: int((m.outcome.detail or {}).get("count") or 0) for m in scored}
    count = sum(counts.values())
    best = max(float((m.outcome.detail or {}).get("best_ratio") or 0.0) for m in scored)
    searched = sum(int((m.outcome.detail or {}).get("searched_chars") or 0) for m in scored)
    score, reason = score_text(count, best, opts, searched)
    snippets: list[dict] = []
    for m in scored:
        for snippet in (m.outcome.detail or {}).get("snippets", []):
            if len(snippets) < 8:
                snippets.append({"from": m.label, **snippet})
    sources = sorted({(m.outcome.detail or {}).get("text_source") for m in scored} - {None})
    confidences = [m.outcome.confidence for m in scored if m.outcome.confidence is not None]
    # The full match record, counts added; the per-member counts are the
    # aggregate block's `values` (result_specs.aggregated_detail).
    detail: dict[str, Any] = {
        "found": count >= max(1, opts["min_count"]),
        "count": count,
        "best_ratio": round(best, 4),
        "mode": opts["match"],
        "pattern": opts["pattern"],
        "snippets": snippets,
        "searched_chars": searched,
        "case_sensitive": opts["case_sensitive"],
        "min_count": opts["min_count"],
        "fuzzy_threshold": opts["fuzzy_threshold"] if opts["match"] == "fuzzy" else None,
        "text_source": sources[0] if len(sources) == 1 else ("mixed" if sources else "none"),
    }
    return Outcome(
        status="ok",
        method="text",
        score=score,
        verdict=verdict_from_score(score),
        confidence=min(confidences) if confidences else None,
        reason=f"sum over {len(scored)} {noun}(s){tail}: {reason}",
        detail=aggregated_detail(
            method="text", rule=rule, level=level,
            members=[(m.label, m.outcome.detail) for m in scored],
            base=with_metric(detail, "text", count),
        ),
        regions=regions,
        localization=localization,
        complete=complete,
    )


def pages_outcome(c: CriterionInput, items: list[int], outcomes: list[Outcome]) -> Outcome:
    """One document's pages collapsed under the criterion's ``pages`` rule."""
    members = [Member(f"item {n}", n, o) for n, o in zip(items, outcomes)]
    return aggregate_level(c, c.aggregate_rules()["pages"], members, "item")


@dataclass
class Aggregated:
    """A criterion after both levels."""

    final: Outcome
    per_document: list[Outcome]
    used: dict[str, Any]


def aggregate_criterion(c: CriterionInput, units, groups) -> Aggregated:
    """Both levels for one criterion's ``scheduler.CriterionUnits``."""
    rules = c.aggregate_rules()
    if units.scope == "document":
        per_document = [units.outcomes[g.index] for g in groups]
        used = {
            "pages": None,
            "documents": rules["documents"],
            "note": "options.scope is 'document': each document is searched as one "
                    "text, so there is no pages level and its rule is ignored",
        }
    else:
        per_document = [
            pages_outcome(c, [ctx.item for ctx in g.items],
                          [units.outcomes[ctx.item] for ctx in g.items])
            for g in groups
        ]
        used = {"pages": rules["pages"], "documents": rules["documents"]}
    members = [Member(f"document {g.index}", None, o) for g, o in zip(groups, per_document)]
    final = aggregate_level(c, rules["documents"], members, "document")
    return Aggregated(final=final, per_document=per_document, used=used)
