"""Submit-time resolution of an /assess request's ``references``.

Everything that needs the reference STORE is decided here, at submit, so the
worker never looks a reference up by id and a request that cannot be served
is refused on the POST:

    resolve_references()  the request → ``Resolved``: the criteria to run
                          (the caller's, or the listed references' merged by
                          name) and the PLAN the worker follows — which
                          examples guide which criterion. The plan rides in
                          the job payload as its optional ``references`` key.
    auto_pool()           ``references: "auto"``: the ready references with a
                          usable criterion whose name matches one of the
                          request's llm-answered criteria (case-folded),
                          filtered by tags, newest first, capped at
                          CLASSIFIER_REFERENCE_AUTO_POOL_MAX. The plan then
                          carries, for the worker's per-item selection call,
                          each pool reference's catalogue entry
                          (``references.render.catalogue_entry``) and its
                          examples, keyed by reference id — the worker picks
                          from these and never reads the store's rows.
    ReferenceResolutionError  carries the HTTP status (400 / 409) and the
                          message; ``api.assess`` turns it into the response.

The store-checked rules (the static ones are on ``api.schemas.AssessRequest``):

  * an unknown id — 400, naming every unknown id at once;
  * a reference that is not ``ready`` — 409;
  * ``criteria`` omitted: the references' criteria are inherited, merged by
    name in list order; one name with a different type / options / weight /
    score / depends_on in two references is a 400 naming both, and the merged
    list goes back through ``AssessRequest`` (every request rule re-runs);
  * ``auto`` with ``criteria`` omitted — 400 (there is nothing to match on);
  * an explicit ``options.reference.criterion`` that no listed reference has —
    400;
  * more examples for one criterion than CLASSIFIER_REFERENCE_MAX_PER_CRITERION
    — 400;
  * ``position: "check"`` against an example that is the whole page (no box to
    compare with) — 400.

A criterion is matched to examples by name, case-folded: its own name, or
``options.reference.criterion``. Only reference criteria that are ``usable``
(they guide the llm and have an expected answer) are examples. A criterion
the model does not answer is never guided, and one with no matching example
simply scores without one (``detail.reference.applied: false`` says why).

Process flow position: called by ``api.assess`` between validating the
request and checking its documents. Imports the store and the request
models; never ``analysis`` or ``llm``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

from api.criterion_options import ReferenceOptions
from api.schemas import AssessRequest, CriterionInput, validation_message
from config import REFERENCE_AUTO_POOL_MAX, REFERENCE_MAX_PER_CRITERION
from references.model import Reference
from references.render import catalogue_entry
from references.store import ReferenceRegistry


class ReferenceResolutionError(Exception):
    """A request whose references cannot be served; ``status`` is 400 or 409."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Resolved:
    """What the submit decided: the request to run, and the worker's plan."""

    request: AssessRequest
    plan: Optional[dict[str, Any]]
    reference_ids: list[str]


def _fold(name: str) -> str:
    return name.casefold().strip()


def _same(a: CriterionInput, b: CriterionInput) -> bool:
    return (
        a.type == b.type
        and a.resolved_options() == b.resolved_options()
        and a.weight == b.weight
        and a.score == b.score
        and a.depends_on == b.depends_on
    )


def _inherit(request: AssessRequest, refs: list[Reference]) -> AssessRequest:
    """The request with the references' criteria, merged by name."""
    merged: dict[str, tuple[CriterionInput, str]] = {}
    for ref in refs:
        for name, entry in ((ref.record or {}).get("criteria") or {}).items():
            c = CriterionInput.model_validate(entry["input"])
            if name in merged:
                prior, owner = merged[name]
                if not _same(prior, c):
                    raise ReferenceResolutionError(
                        400,
                        f"criterion {name!r} differs between references {owner} and "
                        f"{ref.id} (type, options, weight, score or depends_on); send "
                        "`criteria` explicitly to choose",
                    )
                continue
            merged[name] = (c, ref.id)
    body = {
        "documents": [d.model_dump() for d in request.documents or []],
        "criteria": [c.model_dump() for c, _ in merged.values()],
        "references": request.reference_ids(),
    }
    try:
        return AssessRequest.model_validate(body)
    except ValidationError as exc:
        raise ReferenceResolutionError(
            400, f"the criteria inherited from the references are not a valid request: "
                 f"{validation_message(exc)}",
        )


def _example(ref: Reference, name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """One example as the worker needs it — plain data, no store handle."""
    expected = entry["expected"]
    verdict = expected["verdict"]
    page = (ref.record or {}).get("page") or {}
    return {
        "reference_id": ref.id,
        "title": ref.title,
        "criterion": name,
        "expected": dict(expected),
        "polarity": {"PASS": "pass", "FAIL": "fail"}.get(verdict, "marginal"),
        "region_source": entry.get("region_source"),
        "regions": list(entry.get("regions") or []),
        "composite": entry.get("composite"),
        "geometry": page.get("geometry"),
    }


def _examples_for(
    c: CriterionInput, refs: list[Reference]
) -> tuple[list[dict[str, Any]], str]:
    """Every usable example for ``c`` across ``refs``, in list order, and
    the reference-criterion name it matched by."""
    opts = c.reference_options() or {}
    target = opts.get("criterion") or c.name
    found = []
    for ref in refs:
        for name, entry in ((ref.record or {}).get("criteria") or {}).items():
            if _fold(name) == _fold(target) and entry.get("usable"):
                found.append(_example(ref, name, entry))
    return found, target


async def auto_pool(
    request: AssessRequest, registry: ReferenceRegistry
) -> tuple[list[Reference], bool]:
    """The ``auto`` pool, fixed at submit: ``(references, truncated)``."""
    auto = request.auto_references()
    assert auto is not None
    wanted = {_fold(c.name) for c in request.criteria if c.guided_by_llm()} | {
        _fold((c.reference_options() or {}).get("criterion") or c.name)
        for c in request.criteria if c.guided_by_llm()
    }
    ready, _ = await registry.list(status="ready", limit=100_000)
    pool: list[Reference] = []
    for ref in ready:  # newest first
        tags = set(ref.tags)
        if auto.tags:
            if auto.tags_match == "all" and not set(auto.tags) <= tags:
                continue
            if auto.tags_match == "any" and not set(auto.tags) & tags:
                continue
        names = {
            _fold(n) for n, e in ((ref.record or {}).get("criteria") or {}).items()
            if e.get("usable")
        }
        if names & wanted:
            pool.append(ref)
    return pool[:REFERENCE_AUTO_POOL_MAX], len(pool) > REFERENCE_AUTO_POOL_MAX


async def resolve_references(
    request: AssessRequest, registry: ReferenceRegistry
) -> Resolved:
    """Validate ``request.references`` against the store and build the plan.

    Raises:
        ReferenceResolutionError: A store-checked rule failed (module docstring).
    """
    if request.references is None:
        return Resolved(request=request, plan=None, reference_ids=[])

    if request.auto_references() is not None:
        if not request.criteria_given():
            raise ReferenceResolutionError(
                400,
                "references \"auto\" picks examples for the request's criteria, so it "
                "needs `criteria`; to inherit a reference's criteria, list its id",
            )
        pool, truncated = await auto_pool(request, registry)
        criteria_plan = {}
        for c in request.criteria:
            if not c.guided_by_llm():
                continue
            opts = c.reference_options() or ReferenceOptions().resolve(c.name)
            criteria_plan[c.name] = {"matched": opts["criterion"], "options": opts}
        examples = {
            ref.id: {
                _fold(name): _example(ref, name, entry)
                for name, entry in ((ref.record or {}).get("criteria") or {}).items()
                if entry.get("usable")
            }
            for ref in pool
        }
        return Resolved(
            request=request,
            plan={
                "mode": "auto",
                "requested": request.auto_references().model_dump(),
                "pool": [r.id for r in pool],
                "pool_truncated": truncated,
                "resolved": [],
                "inherited": False,
                "criteria": criteria_plan,
                "catalogue": [catalogue_entry(ref) for ref in pool],
                "examples": examples,
            },
            reference_ids=[r.id for r in pool],
        )

    ids = request.reference_ids()
    found = {i: await registry.get(i) for i in ids}
    unknown = [i for i, r in found.items() if r is None]
    if unknown:
        raise ReferenceResolutionError(400, f"unknown reference id(s): {unknown}")
    not_ready = {i: r.status for i, r in found.items() if r.status != "ready"}
    if not_ready:
        raise ReferenceResolutionError(
            409,
            "reference(s) not ready: "
            + ", ".join(f"{i} is {status}" for i, status in not_ready.items())
            + " — poll GET /references/{id} until ready",
        )
    refs = [found[i] for i in ids]
    inherited = not request.criteria_given()
    if inherited:
        request = _inherit(request, refs)

    criteria_plan: dict[str, Any] = {}
    for c in request.criteria:
        if not c.guided_by_llm():
            continue
        opts = c.reference_options()
        examples, target = _examples_for(c, refs)
        explicit = opts is not None and opts["criterion"] != c.name
        if explicit and not examples:
            raise ReferenceResolutionError(
                400,
                f"criterion {c.name!r}: options.reference.criterion {target!r} is not a "
                f"usable criterion of any listed reference ({ids})",
            )
        if len(examples) > REFERENCE_MAX_PER_CRITERION:
            raise ReferenceResolutionError(
                400,
                f"criterion {c.name!r} matches {len(examples)} examples across the listed "
                f"references; CLASSIFIER_REFERENCE_MAX_PER_CRITERION is "
                f"{REFERENCE_MAX_PER_CRITERION} — list fewer references",
            )
        if opts and opts["position"] == "check":
            whole = [e["reference_id"] for e in examples if e["region_source"] == "whole_page"]
            if whole:
                raise ReferenceResolutionError(
                    400,
                    f"criterion {c.name!r}: position 'check' needs a box to compare with, "
                    f"and the example in {whole} is the whole page",
                )
        criteria_plan[c.name] = {
            "matched": target,
            "options": opts or ReferenceOptions().resolve(c.name),
            "examples": examples,
        }
    return Resolved(
        request=request,
        plan={
            "mode": "explicit",
            "requested": ids,
            "pool": None,
            "pool_truncated": False,
            "resolved": ids,
            "inherited": inherited,
            "criteria": criteria_plan,
        },
        reference_ids=ids,
    )
