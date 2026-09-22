"""What a layer shows, and what a job result carries inline.

The three decisions about regions that are not about files:

    visible_regions()    — the subset a combined layer shows when the caller
                           filtered nothing: every region EXCEPT the LLM
                           enforcement loop's rejected attempts, which stay
                           reachable at ``?attempt=n``.
    diff_criterion_name() — the synthetic criterion a change-detection run
                           against example ``i`` is filed under. It is not a
                           criterion; regions.json is keyed by criterion name
                           and layer files are named after slugs, so it needs
                           one.
    attach_regions()     — the bounded inline copy, capped at
                           CLASSIFIER_INLINE_REGIONS_MAX per criterion, plus
                           the (uncapped, tiny) ``localization`` record.

Process flow position: ``visible_regions`` is read by ``regions.artifacts``
when it renders; ``attach_regions`` is step 9 of
``analysis.pipeline.analyze_document``; ``diff_criterion_name`` is used by
``jobs.runners.run_compare``.
"""

from common.vision import Region

from config import DIFF_CRITERION_PREFIX, INLINE_REGIONS_MAX


def visible_regions(regions) -> list[Region]:
    """The subset a layer shows when the caller filtered nothing.

    Every LLM enforcement-loop attempt is stored — that is the point of the
    loop — but a combined overlay showing three boxes for one criterion,
    two of which the loop itself rejected, is not a picture of anything. So
    the stored combined layers, and the ``?criterion=`` renders that carry no
    explicit ``attempt``, both show the ACCEPTED box only. The rejected ones
    are reachable at ``?attempt=n``.

    Non-LLM regions are never filtered: they have no notion of an attempt.
    """
    return [
        r for r in regions
        if r.source != "llm" or r.attrs.get("accepted") is not False
    ]


def diff_criterion_name(index: int) -> str:
    """The synthetic criterion a diff against example ``index`` is filed under.

    It is not a criterion — nothing scores it and it never reaches the
    weighted average — but regions.json is keyed by criterion name and layer
    files are named after slugs, so it needs one.
    """
    return f"{DIFF_CRITERION_PREFIX}{index}"


def attach_regions(
    per_criterion_scores: dict,
    region_map: dict[str, list[Region]],
    per_criterion_artifacts: dict[str, dict | None],
    localizations: dict[str, dict] | None = None,
) -> None:
    """Copy a bounded view of the regions into the scored results, in place.

    The complete list is always in ``regions.json``; this is the copy that
    makes a single poll self-contained for the common case. Past
    CLASSIFIER_INLINE_REGIONS_MAX the list is cut and ``regions_truncated``
    says so, rather than a 5 MB result blob that makes ``GET /jobs`` slow.

    ``localization`` is the exception to the cap: an enforcement-loop record
    is at most LLM_BBOX_MAX_ATTEMPTS small objects, so it is copied whole. An
    ``llm`` criterion the loop did not run on still gets the empty shape, so a
    consumer can read ``localization.accepted_attempt`` without first checking
    whether the key is there.

    SKIPPED criteria are left alone — a criterion that was never evaluated
    has nothing to be located.
    """
    for name, regions in region_map.items():
        entry = per_criterion_scores.get(name)
        if not isinstance(entry, dict) or entry.get("verdict") == "SKIPPED":
            continue
        entry["regions"] = [r.as_dict() for r in regions[:INLINE_REGIONS_MAX]]
        entry["regions_truncated"] = len(regions) > INLINE_REGIONS_MAX
        entry["artifacts"] = per_criterion_artifacts.get(name)
        if entry.get("method") == "llm":
            entry["localization"] = (localizations or {}).get(name) or {
                "attempts": [], "accepted_attempt": None, "calls": 0
            }
