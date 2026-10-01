"""What a layer shows, and what a job result carries inline.

The two decisions about regions that are not about files:

    visible_regions() — the subset a layer shows when the caller filtered
                        nothing: every region EXCEPT the LLM enforcement
                        loop's rejected attempts, which stay reachable at
                        ``?attempt=n``.
    inline_regions()  — the bounded copy a criterion result carries, capped
                        at CLASSIFIER_INLINE_REGIONS_MAX, and whether it was
                        cut.

Process flow position: ``visible_regions`` is read by ``api.artifacts`` when
it renders a layer; ``inline_regions`` by ``analysis.pipeline`` when it
assembles each criterion's result.
"""

from common.vision import Region

from config import INLINE_REGIONS_MAX


def visible_regions(regions) -> list[Region]:
    """The subset a layer shows when the caller filtered nothing.

    Every LLM enforcement-loop attempt is stored — that is the point of the
    loop — but a combined overlay showing three boxes for one criterion,
    two of which the loop itself rejected, is not a picture of anything. So
    the combined layers, and the ``?criterion=`` renders that carry no
    explicit ``attempt``, both show the ACCEPTED box only.

    Non-LLM regions are never filtered: they have no notion of an attempt.
    """
    return [
        r for r in regions
        if r.source != "llm" or r.attrs.get("accepted") is not False
    ]


def inline_regions(regions: list[Region]) -> tuple[list[dict], bool]:
    """``(the first INLINE_REGIONS_MAX regions as dicts, truncated?)``.

    The complete list is always in ``regions.json``; this is the copy that
    makes a single poll self-contained for the common case, rather than a
    5 MB result blob that makes ``GET /jobs`` slow.
    """
    return (
        [r.as_dict() for r in regions[:INLINE_REGIONS_MAX]],
        len(regions) > INLINE_REGIONS_MAX,
    )
