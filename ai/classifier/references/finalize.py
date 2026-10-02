"""Caller answers + pipeline answers → the reference's frozen record.

The creation job (``jobs.runners.run_reference``) runs the pipeline on the
example page for whatever the caller did not supply, then hands this module
plain data — the stored criteria, what was supplied, what the pipeline
observed — and gets back the per-criterion record and its warnings. The
merge rules, all here:

  * **Expected answer.** A supplied breakdown wins (``source: "caller"``, or
    ``"pipeline"`` when ``from_job`` supplied the reviewed job's own answer);
    otherwise the pipeline's answer, when it gave one (``"pipeline"``);
    otherwise none. A ``score: false`` criterion never has one.
  * **Regions.** Supplied regions win (``region_source: "caller"`` — drawn
    with the ``manual`` stroke — or ``"pipeline"`` for a job's regions);
    ``[]`` supplied means "the whole page is the example"; otherwise the
    pipeline's ACCEPTED regions; otherwise ``whole_page``.
  * **Usable.** A criterion is an example for the vision model only when it
    guides the llm (``references.model.guides_llm``) and has an expected
    answer. Text / detector / OpenCV criteria are stored all the same — a
    reference's criteria are inherited by an /assess that lists it — but get
    no composite.
  * **Warnings, not failures**, for answers that disagree ("you said PASS,
    the model scored the example 3 and located nothing"): the caller's
    review is the answer key, and the warning is how they find out the model
    sees it differently. The one hard failure is a reference where NO scored
    criterion ended with an expected answer — every model call failed and
    nothing was supplied, so there is nothing to keep.

Process flow position: called by ``jobs.runners.run_reference`` after the
pipeline, before ``references.render``. Pure: no I/O.
"""

from __future__ import annotations

from typing import Any, Optional

from common.vision import PageGeometry, Region, clamp_points, slugify_criterion

from api.schemas import CriterionInput
from references.model import composite_name, guides_llm


class ReferenceIncomplete(ValueError):
    """Nothing answered the reference's criteria — the reference fails."""


def _region(data: dict[str, Any], name: str, source: str, geometry: PageGeometry) -> Region:
    """A supplied region dict (``kind`` + ``points`` in page pixels) → Region.

    Clamped to the page: submit already refused anything more than a pixel
    outside it, and that pixel of rounding (a PDF page's render size) must
    not survive into the stored geometry.
    """
    region = Region.from_dict({**data, "page": 0, "label": name})
    if source == "caller":
        region.source = "manual"
    region.points = clamp_points(region.points, geometry.width, geometry.height)
    return region


def merge(
    criteria: list[CriterionInput],
    supplied: dict[str, dict[str, Any]],
    observed: dict[str, dict[str, Any]],
    observed_regions: dict[str, list[Region]],
    geometry: PageGeometry,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The record's ``criteria`` block and the warnings.

    Args:
        criteria:         The reference's criteria, as stored (depends_on and
                          all — not the copies the pipeline ran).
        supplied:         ``{name: {"breakdown": {score, verdict, reason,
                          source} | absent, "regions": [region dict] | absent,
                          "regions_source": "caller" | "pipeline",
                          "observed": {...} | absent}}`` — what the submit
                          resolved: the caller's answers over a from_job's.
        observed:         ``{name: {status, method, score, verdict,
                          confidence, reason, error}}`` from THIS creation
                          job's pipeline run (absent for a criterion that did
                          not run).
        observed_regions: ``{name: [Region]}`` — the run's ACCEPTED regions,
                          in page pixels.
        geometry:         The example page's frame (page 0).

    Raises:
        ReferenceIncomplete: No scored criterion has an expected answer.
    """
    out: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for c in criteria:
        name = c.name
        sup = supplied.get(name) or {}
        obs = observed.get(name) or sup.get("observed")
        llm = guides_llm(c)

        # ── The expected answer ───────────────────────────────────────────
        expected: Optional[dict[str, Any]] = None
        breakdown = sup.get("breakdown")
        if not c.score:
            expected = None
        elif breakdown:
            expected = {
                "score": int(breakdown["score"]),
                "verdict": breakdown["verdict"],
                "reason": breakdown.get("reason") or "",
                "source": breakdown.get("source", "caller"),
            }
        elif obs and obs.get("status") == "ok" and obs.get("score") is not None:
            expected = {
                "score": int(obs["score"]),
                "verdict": obs["verdict"],
                "reason": obs.get("reason") or "",
                "source": "pipeline",
            }
        elif obs is not None:
            warnings.append(
                f"{name!r}: no expected answer — the pipeline returned status "
                f"{obs.get('status')!r}" + (f" ({obs['error']})" if obs.get("error") else "")
                + " and no breakdown was supplied"
            )

        # ── Where ─────────────────────────────────────────────────────────
        if "regions" in sup and sup["regions"] is not None:
            source = sup.get("regions_source", "caller")
            regions = [_region(r, name, source, geometry) for r in sup["regions"]]
            region_source = source if regions else "whole_page"
        else:
            regions = [_relabel(r, name) for r in observed_regions.get(name, [])]
            region_source = "pipeline" if regions else "whole_page"

        if (
            expected is not None
            and breakdown
            and obs is not None
            and obs.get("status") == "ok"
            and obs.get("verdict") is not None
            and obs.get("verdict") != expected["verdict"]
        ):
            located = "" if region_source != "whole_page" else " and located nothing"
            warnings.append(
                f"{name!r}: you said {expected['verdict']}, the model scored the "
                f"example {obs.get('score')}{located}"
            )
        elif (
            llm and expected is not None and expected["verdict"] == "PASS"
            and region_source == "whole_page" and "regions" not in sup
            and _could_locate(c)
        ):
            warnings.append(
                f"{name!r}: no region was supplied or located, so the whole page is "
                "the example"
            )

        out[name] = {
            "input": c.model_dump(mode="json"),
            "slug": slugify_criterion(name),
            "guides_llm": llm,
            "expected": expected,
            "observed": _observed_view(obs),
            "regions": [r.as_dict() for r in regions],
            "region_source": region_source,
            "composite": composite_name(name) if llm else None,
            "usable": bool(llm and expected is not None),
        }

    scored = [c for c in criteria if c.score]
    if scored and all(out[c.name]["expected"] is None for c in scored):
        raise ReferenceIncomplete(
            "no criterion of this reference has an expected answer: nothing was "
            "supplied in `breakdown` and the pipeline answered none of them — "
            + "; ".join(warnings or ["see the creation job"])
        )
    return out, warnings


def _relabel(region: Region, name: str) -> Region:
    copy = Region.from_dict(region.as_dict())
    copy.page = 0
    copy.label = name
    return copy


def _could_locate(c: CriterionInput) -> bool:
    """Whether the creation job could have boxed it (it forces ``boxes`` on
    presence / auto llm criteria) — so "located nothing" is worth saying."""
    return c.type == "llm" and c.options.hint in ("presence", "auto")


def _observed_view(obs: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if obs is None:
        return None
    keys = ("status", "method", "score", "verdict", "confidence", "reason", "error", "job_id")
    return {k: obs.get(k) for k in keys if k in obs}
