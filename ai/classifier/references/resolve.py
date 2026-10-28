"""Submit-time resolution of an /assess request's ``references``.

Everything that needs the reference STORE is decided here, at submit, so the
worker never looks a reference up by id and a request that cannot be served
is refused on the POST:

    resolve_references()  the request → ``Resolved``: the criteria to run
                          (the caller's, or the listed references' merged by
                          name) and the PLAN the worker follows — which
                          examples guide which criterion. The plan rides in
                          the job payload as its optional ``references`` key.
    match_target()        how ONE criterion finds its examples — the matching
                          order below, as ``(matched_by, value)``.
    auto_pool()           ``references: "auto"``: the ready references with a
                          usable criterion that one of the request's
                          llm-answered criteria matches (the same order),
                          filtered by tags, newest first, capped at
                          CLASSIFIER_REFERENCE_AUTO_POOL_MAX. The plan then
                          carries, for the worker's per-item selection call,
                          each pool reference's catalogue entry
                          (``references.render.catalogue_entry``) and its
                          examples by reference id — ``examples`` by folded
                          criterion name and ``examples_by_key`` by folded
                          key — so the worker picks from these and never
                          reads the store's rows.
    ReferenceResolutionError  carries the HTTP status (400 / 409) and the
                          message; ``api.assess`` turns it into the response.

The store-checked rules (the static ones are on ``api.schemas.AssessRequest``):

  * an unknown id — 400, naming every unknown id at once;
  * a reference that is not ``ready`` — 409;
  * ``criteria`` omitted: the references' criteria are inherited, merged by
    name in list order; one name with a different type / options / weight /
    score / depends_on / key in two references is a 400 naming both, and the
    merged list goes back through ``AssessRequest`` (every request rule
    re-runs);
  * ``auto`` with ``criteria`` omitted — 400 (there is nothing to match on);
  * an explicit ``options.reference.criterion`` that no listed reference has —
    400 (an unmatched ``key`` or name is NOT an error: that criterion scores
    unguided, with a note);
  * more examples for one criterion than CLASSIFIER_REFERENCE_MAX_PER_CRITERION
    — 400;
  * ``position: "check"`` against an example that is the whole page (no box to
    compare with) — 400.

The matching order (``match_target``; API.md § How a criterion finds its
examples), the first that is set deciding, every comparison case-folded:

  1. ``options.reference.criterion`` → the reference criterion with that NAME
     (``matched_by: "criterion"``);
  2. the criterion's ``key`` → the reference criteria with that KEY
     (``"key"``) — a keyed criterion never falls back to its name;
  3. its own ``name`` → the reference criteria with that NAME (``"name"``).

Only reference criteria that are ``usable`` (they guide the llm and have an
expected answer) are examples. A criterion the model does not answer is
never guided, and one with no matching example simply scores without one
(``detail.reference.applied: false`` says why). The plan records the rule
per criterion (``matched_by``); a plan written before keys has none, and the
worker reads that as ``"name"``.

Process flow position: called by ``api.assess`` between validating the
request and checking its documents. Imports the store and the request
models; never ``analysis`` or ``llm``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import ValidationError

from api.criterion_options import ReferenceOptions, without_model
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


def _fold_key(key: Optional[str]) -> Optional[str]:
    return None if key is None else _fold(key)


def match_target(c: CriterionInput) -> tuple[str, str]:
    """How ``c`` finds its examples: ``(matched_by, value)``, the first rule
    that is set — ``("criterion", options.reference.criterion)``, else
    ``("key", c.key)``, else ``("name", c.name)``.

    Reads the RAW ``options.reference.criterion``: the resolved option always
    defaults to the name, which would hide the key."""
    reference = getattr(c.options, "reference", None)
    if reference is not None and reference.criterion is not None:
        return "criterion", reference.criterion
    if c.key is not None:
        return "key", c.key
    return "name", c.name


def _matches(by: str, value: str, ref_name: str, entry: dict[str, Any]) -> bool:
    """Whether the reference criterion ``ref_name`` (its record ``entry``) is
    what ``(by, value)`` asks for: its KEY for ``key``, its NAME otherwise.
    A record written before keys has no ``key`` in its input — key-less."""
    if by == "key":
        return _fold_key((entry.get("input") or {}).get("key")) == _fold(value)
    return _fold(ref_name) == _fold(value)


def _same(a: CriterionInput, b: CriterionInput) -> bool:
    """Whether two references hold the SAME criterion. ``options.model`` is
    not part of a criterion's identity (``without_model``): the same question
    answered on two models is one criterion — the first reference's options
    (model included) are the ones inherited. The ``key`` is part of it
    (compared case-folded): one name keyed two ways would be found two ways."""
    return (
        a.type == b.type
        and without_model(a.resolved_options()) == without_model(b.resolved_options())
        and a.weight == b.weight
        and a.score == b.score
        and a.depends_on == b.depends_on
        and _fold_key(a.key) == _fold_key(b.key)
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
                        f"{ref.id} (type, options, weight, score, depends_on or key); "
                        "send `criteria` explicitly to choose",
                    )
                continue
            merged[name] = (c, ref.id)
    body = {
        "documents": [d.model_dump() for d in request.documents or []],
        "criteria": [c.model_dump() for c, _ in merged.values()],
        "references": request.reference_ids(),
    }
    # The job's model rides through the rebuild (only when sent, so an
    # inherited request without one stays "default").
    if request.model is not None:
        body["model"] = request.model
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
) -> tuple[list[dict[str, Any]], str, str]:
    """Every usable example for ``c`` across ``refs``, in list order, and
    the rule and value it matched by (``match_target``)."""
    by, value = match_target(c)
    found = []
    for ref in refs:
        for name, entry in ((ref.record or {}).get("criteria") or {}).items():
            if entry.get("usable") and _matches(by, value, name, entry):
                found.append(_example(ref, name, entry))
    return found, by, value


async def auto_pool(
    request: AssessRequest, registry: ReferenceRegistry
) -> tuple[list[Reference], bool]:
    """The ``auto`` pool, fixed at submit: ``(references, truncated)``. A
    reference joins when one of its usable criteria is what some
    llm-answered criterion's ``match_target`` asks for — by name, or by key."""
    auto = request.auto_references()
    assert auto is not None
    targets = [match_target(c) for c in request.criteria if c.guided_by_llm()]
    wanted_names = {_fold(value) for by, value in targets if by != "key"}
    wanted_keys = {_fold(value) for by, value in targets if by == "key"}
    ready, _ = await registry.list(status="ready", limit=100_000)
    pool: list[Reference] = []
    for ref in ready:  # newest first
        tags = set(ref.tags)
        if auto.tags:
            if auto.tags_match == "all" and not set(auto.tags) <= tags:
                continue
            if auto.tags_match == "any" and not set(auto.tags) & tags:
                continue
        usable = [
            (n, e) for n, e in ((ref.record or {}).get("criteria") or {}).items()
            if e.get("usable")
        ]
        names = {_fold(n) for n, _ in usable}
        keys = {_fold_key((e.get("input") or {}).get("key")) for _, e in usable} - {None}
        if names & wanted_names or keys & wanted_keys:
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
            by, value = match_target(c)
            opts = c.reference_options() or ReferenceOptions().resolve(c.name)
            criteria_plan[c.name] = {"matched": value, "matched_by": by, "options": opts}
        examples = {
            ref.id: {
                _fold(name): _example(ref, name, entry)
                for name, entry in ((ref.record or {}).get("criteria") or {}).items()
                if entry.get("usable")
            }
            for ref in pool
        }
        # The same examples by folded key, for criteria matched by key — one
        # example per key, since keys are unique within a reference. Only
        # references that have a keyed usable criterion appear.
        examples_by_key: dict[str, dict[str, dict[str, Any]]] = {}
        for ref in pool:
            for name, entry in ((ref.record or {}).get("criteria") or {}).items():
                key = (entry.get("input") or {}).get("key")
                if entry.get("usable") and key is not None:
                    examples_by_key.setdefault(ref.id, {})[_fold(key)] = _example(
                        ref, name, entry
                    )
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
                "examples_by_key": examples_by_key,
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
        examples, by, value = _examples_for(c, refs)
        # Only an explicit options.reference.criterion is a promise that the
        # example exists; an unmatched key or name scores unguided, noted.
        if by == "criterion" and not examples:
            raise ReferenceResolutionError(
                400,
                f"criterion {c.name!r}: options.reference.criterion {value!r} is not a "
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
            "matched": value,
            "matched_by": by,
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
