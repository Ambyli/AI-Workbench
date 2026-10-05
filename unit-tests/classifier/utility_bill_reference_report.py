#!/usr/bin/env python3
"""Save one utility bill as a reference, and use it to judge another.

``regions_report.py`` runs the Postman collection; ``plan_set_report.py``
asks one whole-document question. This script asks *is this document a
utility bill?* — and answers it twice, so the report can show what a saved
example changes:

    POST /references   the reference bill (utility_bill_2.jpeg, AEP Ohio),
                       criteria + answer key from utility_bill_reference.json
      → poll GET /references/{id} until ready → download the composite
        (the exact image the model is shown) and record.json
    for each candidate (utility_bill.jpeg, Ohio Edison, by default):
      POST /assess     baseline — the criteria, no references
      POST /assess     guided   — the same criteria, references: [id]
      → poll → download regions.json + text layers → re-draw the text hits
    → DELETE /references/{id} → index.html + summary.json + exit code

The criteria (``utility_bill_reference.json``) are one ``llm`` criterion —
*"This document is a utility bill: …"*, weight 3 — and four ``text``
criteria that corroborate it from the OCR'd text (usage in kWh / therms,
an amount due, an account number, a billing period). Only the ``llm``
criterion is answered by the vision model, so only it is guided: on the
guided call its one scoring call becomes ONE call with two images — the
reference (captioned *"in this reference '…' was PASS (10): …"*) and the
candidate. The text criteria come out the same both times; they are there
so the overall verdict does not rest on one model call.

**The reference's answer key is supplied, not inferred.** The breakdown
(PASS 10, with a reason), the region (``[]`` — the whole page is the
example) and the description are all in the spec, so creating the
reference costs no model call at all. Its reason describes the bill's
structure, never its values: the reference is a stored copy of a customer
document, kept until deleted, so this script deletes it at the end unless
``--keep-reference`` is given (``--reference-id`` reuses a kept one and
never deletes it).

Usage (from the repo root)::

    uv run --package classifier python unit-tests/classifier/utility_bill_reference_report.py
    # your own candidates (no expectations: the verdict check is skipped)
    uv run --package classifier python unit-tests/classifier/utility_bill_reference_report.py \\
        ~/Downloads/some_bill.jpg ~/Downloads/not_a_bill.pdf
    # in-process, no vision model: the reference is created and listed for
    # real; the llm criterion comes back status: error
    uv run --package classifier python unit-tests/classifier/utility_bill_reference_report.py --local

With no candidates given, the spec's ``candidates`` are run, each with its
own ``expect`` (the guided verdict) and optional ``expect_changed`` (the
reference must flip the verdict: guided ≠ baseline). Documents on the
command line replace them; ``--expect PASS|MARGINAL|FAIL`` then asserts the
guided verdict of every one.

**An example where the reference decides the answer** —
``utility_bill_k7_reference.json``. "Is this a utility bill?" is something
the model already knows, so the default spec's reference has nothing to add
(PASS 10 either way). The K7 spec asks *"This document is an intake class
K7 document"* — an internal label the model has never seen, defined ONLY by
the reference (the AEP bill, PASS 10). Without it the model scores the Ohio
Edison bill FAIL ("intake class K7 is absent"); with it, PASS ("a
residential electric bill matching the intake class K7 example"). The
roofing invoice is FAIL both ways, so the reference teaches a meaning
rather than passing everything::

    uv run --package classifier python unit-tests/classifier/utility_bill_reference_report.py \\
        --spec unit-tests/classifier/utility_bill_k7_reference.json
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import shutil
import sys
import time
from typing import Any, Optional

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import regions_report as rr  # noqa: E402 — after the path insert; it sets up `common`

from common.env import load_env  # noqa: E402

DEFAULT_SPEC = HERE / "utility_bill_reference.json"
VERDICTS = ("PASS", "MARGINAL", "FAIL")
FOLDER = "Utility bill, guided by a reference"
REFERENCE_DIR = "reference"     # under the output root
REFERENCE_TERMINAL = ("ready", "failed")

# Overall verdict → the answer to the question this script asks.
ANSWERS = {
    "PASS": "yes — a utility bill",
    "MARGINAL": "uncertain — some utility-bill evidence, not enough",
    "FAIL": "no — not a utility bill",
}


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Candidate:
    """A document to judge, and what its guided run should come to."""

    document: pathlib.Path
    expect: Optional[str] = None
    # The reference must flip the verdict: guided ≠ baseline.
    expect_changed: bool = False
    # Passed on the command line rather than listed in the spec.
    adhoc: bool = False


@dataclasses.dataclass
class Spec:
    """``utility_bill_reference.json``, checked before anything is sent."""

    document: pathlib.Path
    title: str
    description: Optional[str]
    tags: list[str]
    breakdown: dict[str, dict]
    regions: dict[str, list]
    criteria: list[dict]
    candidates: list[Candidate] = dataclasses.field(default_factory=list)

    @property
    def reference_criteria(self) -> list[dict]:
        """The criteria the reference stores: the ones with an answer key.

        ``depends_on`` is dropped — the reference holds only these, and a
        dependency on a criterion it does not hold is a 400.
        """
        return [
            {k: v for k, v in c.items() if k != "depends_on"}
            for c in self.criteria
            if c["name"] in self.breakdown
        ]

    @property
    def guided(self) -> list[str]:
        """The criteria the reference can guide: llm-answered, with an answer key."""
        return [
            c["name"] for c in self.criteria
            if (c.get("type") or "llm") == "llm" and c["name"] in self.breakdown
        ]


def load_spec(path: pathlib.Path, document: Optional[pathlib.Path]) -> Spec:
    raw = json.loads(path.read_text(encoding="utf-8"))
    ref = raw.get("reference") or {}
    criteria = list(raw.get("criteria") or [])
    names = {c["name"] for c in criteria}
    breakdown = dict(ref.get("breakdown") or {})
    regions = dict(ref.get("regions") or {})
    unknown = sorted((set(breakdown) | set(regions)) - names)
    if unknown:
        raise SystemExit(f"{path.name}: reference breakdown / regions name no criterion: {unknown}")
    if not breakdown:
        raise SystemExit(f"{path.name}: the reference needs a breakdown (its answer key)")
    for c in criteria:
        if (c.get("options") or {}).get("reference") is not None:
            raise SystemExit(
                f"{path.name}: {c['name']!r} carries options.reference, which the "
                "baseline call (no references) would refuse — the guided call matches by name"
            )
    candidates = []
    for entry in raw.get("candidates") or []:
        if entry.get("expect") not in (None, *VERDICTS):
            raise SystemExit(f"{path.name}: candidate {entry.get('document')!r}: "
                             f"expect must be one of {VERDICTS}")
        candidates.append(Candidate(
            document=(rr.REPO_ROOT / entry["document"]).resolve(),
            expect=entry.get("expect"),
            expect_changed=bool(entry.get("expect_changed")),
        ))
    doc = document or (rr.REPO_ROOT / ref["document"])
    return Spec(
        document=doc.expanduser().resolve(),
        title=ref.get("title") or doc.name,
        description=ref.get("description"),
        tags=list(ref.get("tags") or []),
        breakdown=breakdown,
        regions=regions,
        criteria=criteria,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# The reference
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ReferenceResult:
    """The reference this run created (or was handed), as the service holds it."""

    document: Optional[pathlib.Path]
    created: bool                       # False with --reference-id
    reference_id: Optional[str] = None
    job_id: Optional[str] = None
    http_status: int = 0
    status: str = "not-submitted"
    elapsed_s: float = 0.0
    body: dict = dataclasses.field(default_factory=dict)
    files: list[str] = dataclasses.field(default_factory=list)
    error: Optional[str] = None
    deleted: Optional[str] = None       # what DELETE said, when it was tried
    checks: list[dict] = dataclasses.field(default_factory=list)

    @property
    def record(self) -> dict:
        return self.body.get("record") or {}

    @property
    def criteria(self) -> dict[str, dict]:
        return dict(self.record.get("criteria") or {})


def create_reference(
    spec: Spec, transport: Any, out_dir: pathlib.Path, timeout_s: float
) -> ReferenceResult:
    """POST /references (multipart) and poll it to ready or failed."""
    ref = ReferenceResult(document=spec.document, created=True)
    data: dict[str, Any] = {
        "criteria": rr._compact(spec.reference_criteria),
        "breakdown": rr._compact(spec.breakdown),
        "regions": rr._compact(spec.regions),
        "title": spec.title,
        "tags": rr._compact(spec.tags),
    }
    if spec.description:
        data["description"] = spec.description
    files = [("file", (spec.document.name, spec.document.read_bytes(), "application/octet-stream"))]

    started = time.monotonic()
    try:
        response = transport.request("POST", "/v1/classifier/references", data=data, files=files)
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        ref.error = f"submit failed: {exc}"
        return ref
    ref.http_status = response.status_code
    if response.status_code >= 400:
        ref.status = "rejected"
        ref.error = (
            "this classifier has no POST /references — it is a build from before the "
            "references API; deploy the current one (make build classifier && make up classifier)"
            if response.status_code == 404
            else rr._short(response.text)
        )
        rr._write_json(out_dir / "response.json",
                       {"status": response.status_code, "body": rr._json_or_text(response)})
        return ref
    accepted = response.json()
    ref.reference_id, ref.job_id = accepted.get("reference_id"), accepted.get("job_id")
    if not ref.reference_id:
        ref.error = "202 with no reference_id"
        return ref

    _poll_reference(ref, transport, timeout_s)
    ref.elapsed_s = time.monotonic() - started
    _download_reference(ref, transport, out_dir)
    return ref


def fetch_reference(reference_id: str, transport: Any, out_dir: pathlib.Path) -> ReferenceResult:
    """--reference-id: read an existing reference instead of creating one."""
    ref = ReferenceResult(document=None, created=False, reference_id=reference_id)
    _poll_reference(ref, transport, timeout_s=0)
    _download_reference(ref, transport, out_dir)
    return ref


def _poll_reference(ref: ReferenceResult, transport: Any, timeout_s: float) -> None:
    """GET /references/{id} until ready / failed — once when ``timeout_s`` is 0."""
    deadline = time.monotonic() + timeout_s
    while True:
        response = transport.request("GET", f"/v1/classifier/references/{ref.reference_id}")
        ref.http_status = response.status_code
        if response.status_code != 200:
            ref.status = "missing" if response.status_code == 404 else "unreadable"
            ref.error = f"GET /references/{ref.reference_id} returned {response.status_code}: " \
                        f"{rr._short(response.text, 200)}"
            return
        ref.body = response.json()
        ref.status = ref.body.get("status") or "unknown"
        ref.job_id = ref.job_id or ref.body.get("job_id")
        if ref.status in REFERENCE_TERMINAL or time.monotonic() >= deadline:
            break
        time.sleep(rr.POLL_INTERVAL_S)
    if ref.status == "failed":
        ref.error = ref.body.get("error") or "the creation job failed"
    elif ref.status not in REFERENCE_TERMINAL:
        ref.error = f"still {ref.status} after {timeout_s:.0f}s"


def _download_reference(ref: ReferenceResult, transport: Any, out_dir: pathlib.Path) -> None:
    """The composites (what the model is shown) and record.json."""
    if ref.body:
        rr._write_json(out_dir / "reference.json", ref.body)
    for entry in ref.body.get("files") or []:
        name = entry.get("name") or ""
        if not (name == "record.json" or (name.startswith("c.") and name.endswith(".jpg"))):
            continue
        response = transport.request(
            "GET", f"/v1/classifier/references/{ref.reference_id}/files/{name}"
        )
        if response.status_code == 200:
            (out_dir / name).write_bytes(response.content)
            ref.files.append(name)


def delete_reference(ref: ReferenceResult, transport: Any) -> None:
    response = transport.request("DELETE", f"/v1/classifier/references/{ref.reference_id}")
    ref.deleted = (
        "deleted" if response.status_code == 204
        else f"DELETE returned {response.status_code}: {rr._short(response.text, 200)}"
    )


def check_reference(ref: ReferenceResult, spec: Spec) -> None:
    ref.checks.append({
        "kind": "reference", "target": "the reference is ready", "ok": ref.status == "ready",
        "expected": "ready", "actual": f"{ref.status}" + (f": {ref.error}" if ref.error else ""),
    })
    if ref.status != "ready":
        return
    for name in spec.guided:
        entry = ref.criteria.get(name) or {}
        expected = entry.get("expected") or {}
        want = spec.breakdown[name]
        ref.checks.append({
            "kind": "reference",
            "target": f"'{rr._clip(name, 60)}' is a usable example",
            "ok": bool(entry.get("usable")) and expected.get("score") == want.get("score"),
            "expected": f"usable, {want.get('score')} ({expected.get('source') or 'caller'})",
            "actual": (
                f"{'usable' if entry.get('usable') else 'not usable'}, "
                f"{expected.get('verdict')} {expected.get('score')} "
                f"({expected.get('source')}), region {entry.get('region_source')}"
                if entry else "not in the record"
            ),
        })


# ---------------------------------------------------------------------------
# The candidates
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CandidateResult:
    """One candidate, assessed without and with the reference."""

    document: pathlib.Path
    expect: Optional[str]
    guided: rr.CaseResult
    baseline: Optional[rr.CaseResult] = None
    expect_changed: bool = False
    checks: list[dict] = dataclasses.field(default_factory=list)

    @property
    def changed(self) -> Optional[bool]:
        """Did the reference flip the overall verdict? None when there is no
        baseline, or either side has no verdict to compare."""
        if self.baseline is None:
            return None
        before, after = rr.overall(self.baseline)[0], rr.overall(self.guided)[0]
        return None if before is None or after is None else before != after

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)


def _assess_case(
    document: pathlib.Path, criteria: list[dict], *, index: int, reference_id: Optional[str],
    adhoc: bool,
) -> rr.Case:
    form = {"criteria": rr._compact(criteria)}
    if reference_id:
        form["references"] = rr._compact([reference_id])
        name = f"guided by the reference — {document.name}"
        description = (
            "The same criteria with references: [the reference]. Each llm criterion "
            "matches the reference's criterion by name, so its one scoring call carries "
            "two images: the reference (captioned with its answer) and this document. "
            "Text criteria are not answered by the model and are unchanged."
        )
    else:
        name = f"baseline, no reference — {document.name}"
        description = (
            "One /assess call with the spec's criteria — each llm criterion one "
            "scoring call with the page as its one image, each text criterion a search "
            "of the OCR'd text. No references — the comparison point."
        )
    return rr.Case(
        name=name, folder=FOLDER, index=index, method="POST",
        path="/v1/classifier/assess", description=description, headers={},
        form=form, uploads=[("file", document)], subjects=[document], adhoc=adhoc,
    )


def run_candidate(
    candidate: Candidate,
    spec: Spec,
    reference_id: Optional[str],
    transport: Any,
    out_root: pathlib.Path,
    args: argparse.Namespace,
    *,
    first_index: int,
) -> CandidateResult:
    document, adhoc = candidate.document, candidate.adhoc
    baseline = None
    index = first_index
    if not args.no_baseline:
        baseline = rr.run_case(
            _assess_case(document, json.loads(json.dumps(spec.criteria)), index=index,
                         reference_id=None, adhoc=adhoc),
            transport, out_root, args, keep_remote=True,
        )
        index += 1
    guided = rr.run_case(
        _assess_case(document, json.loads(json.dumps(spec.criteria)), index=index,
                     reference_id=reference_id, adhoc=adhoc),
        transport, out_root, args, keep_remote=True,
    )
    return CandidateResult(document=document, expect=candidate.expect, guided=guided,
                           baseline=baseline, expect_changed=candidate.expect_changed)


def reference_detail(result: rr.CaseResult, name: str) -> dict:
    """``detail.reference`` of the guided criterion — on the criterion, or on
    its one unit when an older aggregate did not carry it up."""
    entry = rr.criterion_results(result).get(name) or {}
    detail = (entry.get("detail") or {}).get("reference")
    if detail is None:
        for unit in entry.get("items") or []:
            detail = (unit.get("detail") or {}).get("reference")
            if detail is not None:
                break
    return detail or {}


def check_candidate(cand: CandidateResult, spec: Spec, *, local: bool) -> None:
    for label, result in (("baseline", cand.baseline), ("guided", cand.guided)):
        if result is None:
            continue
        cand.checks.append({
            "kind": "phase", "target": f"{label} job completed",
            "ok": result.phase == "completed", "expected": "completed",
            "actual": f"{result.phase}" + (f": {result.error}" if result.error else ""),
        })
    guided = cand.guided
    if guided.phase != "completed":
        return

    for name in spec.guided:
        entry = rr.criterion_results(guided).get(name) or {}
        detail = reference_detail(guided, name)
        calls = detail.get("calls") or []
        answered = [c for c in calls if not c.get("error")]
        target = f"the reference guided '{rr._clip(name, 50)}'"
        if entry.get("status") == "error" and local:
            cand.checks.append({
                "kind": "reference", "target": target, "ok": True, "skipped": True,
                "expected": "applied, ≥1 two-image call answered",
                "actual": f"local mode, no vision model: {rr._short(entry.get('error') or '', 120)}",
            })
            continue
        cand.checks.append({
            "kind": "reference", "target": target,
            "ok": bool(detail.get("applied")) and bool(answered)
                  and all((c.get("images") or 0) >= 2 for c in answered),
            "expected": "applied, ≥1 two-image call answered",
            "actual": (
                f"applied={detail.get('applied')}, {len(answered)}/{len(calls)} call(s) answered, "
                f"images {[c.get('images') for c in calls]}"
                + (f" — {detail.get('note')}" if detail.get("note") else "")
                + (f" — {entry.get('error')}" if entry.get("status") == "error" else "")
            ),
        })

    verdict, score = rr.overall(guided)
    actual = f"{verdict or '—'} {score if score is not None else ''}".strip()
    errored = [n for n, entry in rr.criterion_results(guided).items()
               if entry.get("status") == "error"]
    if cand.expect is None:
        cand.checks.append({
            "kind": "verdict", "target": "guided overall verdict", "ok": True, "skipped": True,
            "expected": "nothing (no --expect)", "actual": actual,
        })
    elif local and errored:
        # An errored criterion makes the assessment incomplete, so there is
        # no overall verdict to hold against the expectation.
        cand.checks.append({
            "kind": "verdict", "target": "guided overall verdict", "ok": True, "skipped": True,
            "expected": cand.expect,
            "actual": f"local mode: {len(errored)} criterion errored (no vision model), "
                      "so the assessment has no overall verdict",
        })
    else:
        cand.checks.append({
            "kind": "verdict", "target": "guided overall verdict", "ok": verdict == cand.expect,
            "expected": cand.expect, "actual": actual,
        })

    if cand.expect_changed:
        target = "the reference changed the verdict"
        expected = "guided verdict ≠ baseline verdict"
        before = rr.overall(cand.baseline)[0] if cand.baseline else None
        if cand.baseline is None:
            cand.checks.append({"kind": "impact", "target": target, "ok": True, "skipped": True,
                                "expected": expected, "actual": "--no-baseline: nothing to compare"})
        elif local and errored:
            cand.checks.append({"kind": "impact", "target": target, "ok": True, "skipped": True,
                                "expected": expected,
                                "actual": "local mode: no vision model, so no verdict to compare"})
        else:
            cand.checks.append({"kind": "impact", "target": target, "ok": bool(cand.changed),
                                "expected": expected,
                                "actual": f"{before or '—'} → {verdict or '—'}"})


def comparison(cand: CandidateResult, spec: Spec) -> list[dict]:
    """One row per requested criterion: baseline vs guided."""
    base = rr.criterion_results(cand.baseline) if cand.baseline else {}
    guided = rr.criterion_results(cand.guided)
    rows = []
    for criterion in spec.criteria:
        name = criterion["name"]
        b, g = base.get(name) or {}, guided.get(name) or {}
        delta = None
        if isinstance(b.get("score"), (int, float)) and isinstance(g.get("score"), (int, float)):
            delta = round(g["score"] - b["score"], 2)
        rows.append({
            "name": name,
            "type": criterion.get("type") or "llm",
            "weight": criterion.get("weight", 1),
            "guided_by_reference": name in spec.guided,
            "baseline": _side(b) if cand.baseline else None,
            "guided": _side(g),
            "delta": delta,
        })
    return rows


def _side(entry: dict) -> dict:
    if not entry:
        return {"status": None, "verdict": None, "score": None, "reason": "not in the result"}
    return {
        "status": entry.get("status"),
        "verdict": entry.get("verdict"),
        "score": entry.get("score"),
        "reason": entry.get("error") if entry.get("status") == "error" else entry.get("reason"),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _result_cell(side: Optional[dict]) -> str:
    e = rr._e
    if side is None:
        return "<td class='skip'>not run</td>"
    status = side.get("status")
    if status in (None, "ok"):
        pill = rr._verdict_pill(side.get("verdict"))
    else:
        pill = f'<span class="pill {"SKIPPED" if status == "skipped" else "FAIL"}">{e(status)}</span>'
    score = side.get("score")
    return f"<td>{pill} {e(score if score is not None else '')}</td>"


def _reference_block(ref: ReferenceResult, spec: Spec) -> str:
    e = rr._e
    parts = [
        '<h2 id="reference">The reference</h2>',
        f'<p class="meta"><code>{e(ref.reference_id or "—")}</code> · status '
        f"<strong>{e(ref.status)}</strong>"
        + (f" · from <code>{e(rr._rel(ref.document))}</code>" if ref.document else "")
        + (f" · creation job <code>{e(ref.job_id)}</code>" if ref.job_id else "")
        + (f" · {ref.elapsed_s:.1f}s" if ref.created else " · reused (--reference-id)")
        + (f" · {e(ref.deleted)} at the end" if ref.deleted else "")
        + "</p>",
    ]
    if ref.error:
        parts.append(f'<div class="err"><strong>error:</strong> {e(ref.error)}</div>')
    body = ref.body
    if body:
        parts.append(
            f"<p><strong>{e(body.get('title'))}</strong> · tags "
            f"{e(', '.join(body.get('tags') or []) or '—')} · description "
            f"({e(body.get('description_source') or '—')})</p>"
            f"<div class='desc'>{e(body.get('description') or '')}</div>"
        )
    for warning in (ref.record.get("warnings") or []):
        parts.append(f'<div class="notes">{e(warning)}</div>')

    rows = []
    for name, entry in ref.criteria.items():
        expected = entry.get("expected") or {}
        rows.append(
            "<tr>"
            f"<td>{e(name)}</td><td>{e((entry.get('input') or {}).get('type') or 'llm')}</td>"
            f"<td>{rr._verdict_pill(expected.get('verdict'))} {e(expected.get('score'))}"
            f" <span class='skip'>({e(expected.get('source'))})</span></td>"
            f"<td>{e(entry.get('region_source'))}</td>"
            f"<td>{'<span class=ok>yes</span>' if entry.get('usable') else 'no'}</td>"
            f"<td>{e(expected.get('reason') or '')}</td>"
            "</tr>"
        )
    if rows:
        parts.append(
            "<h3>Answer key</h3>"
            "<table><thead><tr><th>criterion</th><th>type</th><th>expected</th><th>region</th>"
            "<th>usable as an example</th><th>reason (the model is shown this)</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
        )

    composites = [n for n in ref.files if n.startswith("c.")]
    if composites:
        panes = "".join(
            f'<div class="pane"><div class="cap"><code>{e(n)}</code> — the exact image a '
            f"guided call shows the model, beside the candidate</div>"
            f'<img src="{REFERENCE_DIR}/{e(n)}" alt="{e(n)}"></div>'
            for n in composites
        )
        parts.append(f'<h3>What the model is shown</h3><div class="pages">{panes}</div>')
    links = [f'<a href="{REFERENCE_DIR}/{e(n)}">{e(n)}</a>' for n in ref.files if n.endswith(".json")]
    links.append(f'<a href="{REFERENCE_DIR}/reference.json">reference.json</a>')
    parts.append(f'<div class="links">{"".join(links)}</div>')
    if ref.checks:
        parts += ["<h3>Checks</h3>", rr._checks_table(ref.checks)]
    return "\n".join(parts)


def _candidate_block(cand: CandidateResult, spec: Spec) -> str:
    e = rr._e
    guided = cand.guided
    slug = guided.case.slug
    verdict, score = rr.overall(guided)
    parts = [
        f'<h2 id="cand-{e(slug)}">Is it a utility bill? — {e(cand.document.name)}</h2>',
        f'<p class="meta">candidate <code>{e(rr._rel(cand.document))}</code>'
        + (f' · <a href="#{e(cand.baseline.case.slug)}">baseline call</a>' if cand.baseline else "")
        + f' · <a href="#{e(slug)}">guided call</a></p>',
    ]
    if verdict:
        parts.append(
            f'<div class="answer"><strong>{e(ANSWERS.get(verdict, verdict))}</strong> · '
            f"{rr._verdict_pill(verdict)} weighted score {e(score)} with the reference"
            + (
                f" · without it {rr._verdict_pill(rr.overall(cand.baseline)[0])} "
                f"{e(rr.overall(cand.baseline)[1])}"
                if cand.baseline else ""
            )
            + "</div>"
        )
    else:
        parts.append(
            f'<div class="err"><strong>no answer</strong> — {e(guided.phase)}: '
            f"{e(guided.error or 'the assessment is incomplete (a criterion errored)')}</div>"
        )
    for result in (cand.baseline, guided):
        for note in (result.notes if result else []):
            parts.append(f'<div class="notes">{e(note)}</div>')

    rows = []
    for row in comparison(cand, spec):
        reasons = []
        if row["guided_by_reference"]:
            if row["baseline"]:
                reasons.append(f"<div class='mono skip'>without: "
                               f"{e(rr._short(row['baseline'].get('reason') or '', 400))}</div>")
            reasons.append(f"<div class='mono'>with: "
                           f"{e(rr._short(row['guided'].get('reason') or '', 400))}</div>")
        else:
            reasons.append(f"<div class='mono skip'>"
                           f"{e(rr._short(row['guided'].get('reason') or '', 200))}</div>")
        delta = "" if row["delta"] is None else f"{row['delta']:+g}"
        rows.append(
            "<tr>"
            f"<td>{e(row['name'])}{' <span class=ok>· guided</span>' if row['guided_by_reference'] else ''}</td>"
            f"<td>{e(row['type'])}</td><td>{e(row['weight'])}</td>"
            f"{_result_cell(row['baseline'])}{_result_cell(row['guided'])}"
            f"<td>{e(delta)}</td>"
            f"<td>{''.join(reasons)}</td>"
            "</tr>"
        )
    parts.append(
        "<h3>Without vs with the reference</h3>"
        "<table><thead><tr><th>criterion</th><th>type</th><th>weight</th><th>without</th>"
        "<th>with</th><th>Δ</th><th>reason</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )

    for name in spec.guided:
        detail = reference_detail(guided, name)
        if not detail:
            continue
        example_rows = "".join(
            "<tr>"
            f"<td><code>{e(x.get('reference_id'))}</code></td><td>{e(x.get('polarity'))}</td>"
            f"<td>{rr._verdict_pill((x.get('expected') or {}).get('verdict'))} "
            f"{e((x.get('expected') or {}).get('score'))}</td>"
            f"<td>{e(x.get('region'))}</td><td>{e(x.get('call'))}</td>"
            "</tr>"
            for x in detail.get("examples") or []
        )
        call_rows = "".join(
            "<tr>"
            f"<td>{e(c.get('call'))}</td><td>{e(c.get('images'))}</td>"
            f"<td>{rr._verdict_pill(c.get('verdict'))} {e(c.get('score'))}</td>"
            f"<td>{e(c.get('confidence'))}</td><td>{'yes' if c.get('chosen') else ''}</td>"
            f"<td>{e(c.get('error') or rr._short(c.get('reason') or '', 300))}</td>"
            "</tr>"
            for c in detail.get("calls") or []
        )
        parts.append(
            f"<h3>detail.reference — applied {e(detail.get('applied'))}, "
            f"combine {e(detail.get('combine'))}</h3>"
            + (f'<div class="notes">{e(detail.get("note"))}</div>' if detail.get("note") else "")
            + "<table><thead><tr><th>example</th><th>polarity</th><th>its answer</th>"
              "<th>region</th><th>call</th></tr></thead><tbody>" + example_rows + "</tbody></table>"
            + "<table><thead><tr><th>call</th><th>images</th><th>answer</th><th>confidence</th>"
              "<th>chosen</th><th>reason / error</th></tr></thead><tbody>" + call_rows
            + "</tbody></table>"
        )

    if cand.checks:
        parts += ["<h3>Checks</h3>", rr._checks_table(cand.checks)]
    return "\n".join(parts)


def _call_block(result: rr.CaseResult) -> str:
    """The call itself, with the generic tables from regions_report."""
    e = rr._e
    verdict, score = rr.overall(result)
    parts = [
        f'<h2 id="{e(result.case.slug)}">{e(result.case.name)}</h2>',
        f'<p class="meta"><code>POST {e(result.case.endpoint)}</code> · phase '
        f"<strong>{e(result.phase)}</strong> · overall {rr._verdict_pill(verdict)} "
        f"{e(score if score is not None else '')} · {result.elapsed_s:.1f}s"
        + (f" · job <code>{e(result.job_id)}</code>" if result.job_id else "")
        + "</p>",
        f'<div class="desc">{e(result.case.description)}</div>',
    ]
    if result.error:
        parts.append(f'<div class="err"><strong>error:</strong> {e(result.error)}</div>')
    parts += ["<h3>Request</h3>", rr._criteria_request_table(result.case),
              "<h3>Result</h3>", rr._criteria_result_table(result, {})]
    pages = rr._pages_block(result)
    if pages:
        parts += [
            "<h3>Where the text hits landed</h3>",
            "<p class='meta'>Left: drawn by this script from <code>regions.json</code> onto "
            "the document on disk. Right: the service's own preview.</p>",
            pages,
        ]
    parts.append(rr._links_block(result))
    return "\n".join(parts)


def _tally(ref: ReferenceResult, cands: list[CandidateResult]) -> tuple[int, int, int]:
    checks = ref.checks + [c for cand in cands for c in cand.checks]
    met = sum(1 for c in checks if c["ok"])
    skipped = sum(1 for c in checks if c.get("skipped"))
    return met, len(checks), skipped


def _changed_cell(cand: CandidateResult) -> str:
    if cand.changed is None:
        return "<span class='skip'>—</span>"
    return "<span class='ok'>yes</span>" if cand.changed else "no"


def write_html(path: pathlib.Path, ref: ReferenceResult, cands: list[CandidateResult],
               spec: Spec, meta: dict) -> None:
    e = rr._e
    met, total, skipped = _tally(ref, cands)
    rows = []
    for cand in cands:
        g_verdict, g_score = rr.overall(cand.guided)
        b_verdict, b_score = rr.overall(cand.baseline) if cand.baseline else (None, None)
        llm = next(iter(spec.guided), None)
        llm_b = (rr.criterion_results(cand.baseline).get(llm) or {}) if cand.baseline and llm else None
        llm_g = rr.criterion_results(cand.guided).get(llm) or {} if llm else {}
        rows.append(
            "<tr>"
            f'<td><a href="#cand-{e(cand.guided.case.slug)}">{e(cand.document.name)}</a></td>'
            f"<td>{rr._verdict_pill(b_verdict) + ' ' + e(b_score) if cand.baseline else '—'}</td>"
            f"<td>{rr._verdict_pill(g_verdict)} {e(g_score if g_score is not None else '')}</td>"
            f"<td>{e(llm_b.get('score') if llm_b else '—')} → {e(llm_g.get('score'))}</td>"
            f"<td>{_changed_cell(cand)}</td>"
            f"<td>{e(ANSWERS.get(g_verdict or '', '—'))}</td>"
            f"<td>{e(cand.expect or '—')}{' · must change' if cand.expect_changed else ''}</td>"
            "</tr>"
        )
    parts = [
        "<!-- generated by unit-tests/classifier/utility_bill_reference_report.py -->",
        f"<title>Utility bill, guided by a reference — {e(meta['generated_at'])}</title>",
        f"<style>{rr.CSS}</style>",
        "<h1>Is it a utility bill? — guided by a reference</h1>",
        f'<p class="meta">{e(meta["generated_at"])} · mode <strong>{e(meta["mode"])}</strong>'
        f' · {e(meta["base_url"])} · spec <code>{e(meta["spec"])}</code></p>',
        '<p class="flow">POST /references (the reference bill) → poll ready → per candidate: '
        "POST /assess without references, POST /assess with references: [id] → DELETE "
        "/references/{id}</p>",
        "<table><thead><tr><th>candidate</th><th>without the reference</th>"
        "<th>with the reference</th><th>llm score without → with</th>"
        "<th>verdict changed?</th><th>answer</th>"
        "<th>expected</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>",
        f'<p class="tally">{met}/{total} checks met'
        + (f" ({skipped} skipped)" if skipped else "") + "</p>",
        _reference_block(ref, spec),
    ]
    parts += [_candidate_block(cand, spec) for cand in cands]
    for cand in cands:
        if cand.baseline:
            parts.append(_call_block(cand.baseline))
        parts.append(_call_block(cand.guided))
    path.write_text("\n".join(parts) + "\n", encoding="utf-8", newline="\n")


def write_summary_json(path: pathlib.Path, ref: ReferenceResult, cands: list[CandidateResult],
                       spec: Spec, meta: dict) -> None:
    met, total, skipped = _tally(ref, cands)

    def call(result: Optional[rr.CaseResult]) -> Optional[dict]:
        if result is None:
            return None
        verdict, score = rr.overall(result)
        return {
            "job_id": result.job_id, "http_status": result.http_status, "phase": result.phase,
            "verdict": verdict, "score": score, "elapsed_s": round(result.elapsed_s, 2),
            "error": result.error, "notes": result.notes, "dir": result.case.slug,
        }

    payload = dict(meta, checks_met=met, checks_total=total, checks_skipped=skipped)
    payload["reference"] = {
        "reference_id": ref.reference_id,
        "created": ref.created,
        "document": rr._rel(ref.document) if ref.document else None,
        "job_id": ref.job_id,
        "status": ref.status,
        "error": ref.error,
        "elapsed_s": round(ref.elapsed_s, 2),
        "title": ref.body.get("title"),
        "description_source": ref.body.get("description_source"),
        "criteria": ref.body.get("criteria"),
        "warnings": ref.record.get("warnings") or [],
        "files": ref.files,
        "deleted": ref.deleted,
        "checks": ref.checks,
    }
    payload["candidates"] = [
        {
            "document": rr._rel(cand.document),
            "expect": cand.expect,
            "expect_changed": cand.expect_changed,
            "changed": cand.changed,
            "answer": ANSWERS.get(rr.overall(cand.guided)[0] or ""),
            "baseline": call(cand.baseline),
            "guided": call(cand.guided),
            "comparison": comparison(cand, spec),
            "reference_detail": {name: reference_detail(cand.guided, name) for name in spec.guided},
            "checks": cand.checks,
        }
        for cand in cands
    ]
    rr._write_json(path, payload)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("candidates", type=pathlib.Path, nargs="*",
                        help="Documents to judge (default: the spec's `candidates`, with "
                             "their expectations)")
    parser.add_argument("--spec", type=pathlib.Path, default=DEFAULT_SPEC,
                        help="Reference + criteria JSON (default: utility_bill_reference.json)")
    parser.add_argument("--reference", type=pathlib.Path,
                        help="The reference document (default: the spec's — "
                             "documents/utility_bill_2.jpeg). The spec's reason and "
                             "description describe that bill; edit them for another")
    parser.add_argument("--reference-id",
                        help="Use this existing ready reference instead of creating one "
                             "(it is never deleted)")
    parser.add_argument("--keep-reference", action="store_true",
                        help="Do not DELETE the reference this run created; its id is printed")
    parser.add_argument("--no-baseline", action="store_true",
                        help="Skip the unguided /assess call per candidate")
    parser.add_argument("--expect", choices=VERDICTS,
                        help="Assert this guided overall verdict for every candidate given "
                             "on the command line")
    parser.add_argument("--base-url", help="Classifier base URL, LiteLLM pass-through included "
                                           "(default: CLASSIFIER_BASE_URL, else "
                                           f"{rr.DEFAULT_BASE_URL})")
    parser.add_argument("--api-key", help="Bearer token (default: CLASSIFIER_API_KEY, else "
                                          "DEFAULT_LITELLM_MASTER_KEY)")
    parser.add_argument("--out", type=pathlib.Path,
                        help="Output directory (default: unit-tests/classifier/reports/"
                             "utility-bill-reference-<UTC timestamp>/)")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="Seconds to wait for one job or for the reference to be ready")
    parser.add_argument("--local", action="store_true",
                        help="Mount ai/classifier/main.py in-process. The reference is created "
                             "for real (it needs no model call); with no vision model the llm "
                             "criterion comes back status: error")
    parser.add_argument("--keep-jobs", action="store_true",
                        help="Skip the DELETE /jobs/{id} cleanup at the end")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)
    load_env()

    base_url = (args.base_url or os.environ.get("CLASSIFIER_BASE_URL") or rr.DEFAULT_BASE_URL).rstrip("/")
    api_key = (
        args.api_key
        or os.environ.get("CLASSIFIER_API_KEY")
        or os.environ.get("DEFAULT_LITELLM_MASTER_KEY")
        or ""
    )

    spec = load_spec(args.spec.resolve(), args.reference)
    if not args.reference_id and not spec.document.is_file():
        raise SystemExit(f"reference document not found: {spec.document}")
    if args.candidates:
        candidates = [Candidate(p.expanduser().resolve(), expect=args.expect, adhoc=True)
                      for p in args.candidates]
    else:
        candidates = spec.candidates
    if not candidates:
        raise SystemExit(f"no candidates: pass documents, or list them under `candidates` "
                         f"in {args.spec.name}")
    for cand in candidates:
        if not cand.document.is_file():
            raise SystemExit(f"candidate not found: {cand.document}")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_root = (args.out or (rr.DEFAULT_REPORTS_DIR / f"utility-bill-reference-{stamp}")).resolve()
    ref_dir = out_root / REFERENCE_DIR
    ref_dir.mkdir(parents=True, exist_ok=True)

    print(f"reference {args.reference_id or spec.document.name}, "
          f"{len(candidates)} candidate(s) → {out_root}")
    print(f"mode: {'local (in-process TestClient)' if args.local else base_url}\n")

    transport_factory = (
        (lambda: rr.LocalTransport(out_root / "_service"))
        if args.local
        else (lambda: rr.HttpTransport(base_url, api_key))
    )

    cands: list[CandidateResult] = []
    with transport_factory() as transport:
        if args.reference_id:
            print(f"  · reading reference {args.reference_id}", flush=True)
            ref = fetch_reference(args.reference_id, transport, ref_dir)
        else:
            print(f"  · POST /references {spec.document.name}", flush=True)
            ref = create_reference(spec, transport, ref_dir, args.timeout)
        check_reference(ref, spec)
        print(f"    {ref.reference_id or '—'}: {ref.status}"
              + (f" — {ref.error}" if ref.error else ""), flush=True)

        if ref.status == "ready":
            index = 1
            for candidate in candidates:
                print(f"  · {candidate.document.name}: "
                      f"{'guided' if args.no_baseline else 'baseline, then guided'}", flush=True)
                cand = run_candidate(
                    candidate, spec, ref.reference_id, transport, out_root, args,
                    first_index=index,
                )
                index += 1 if args.no_baseline else 2
                check_candidate(cand, spec, local=args.local)
                cands.append(cand)

        if ref.created and ref.reference_id and not args.keep_reference:
            delete_reference(ref, transport)

    if not args.reference_id and spec.document.is_file():
        # The original beside the composite, for the reader's eye only.
        shutil.copyfile(spec.document, ref_dir / f"original{spec.document.suffix.lower()}")

    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base_url": "in-process" if args.local else base_url,
        "mode": "local" if args.local else "http",
        "spec": rr._rel(args.spec.resolve()),
    }
    write_html(out_root / "index.html", ref, cands, spec, meta)
    write_summary_json(out_root / "summary.json", ref, cands, spec, meta)

    print()
    print(f"reference {ref.reference_id or '—'}: {ref.status}"
          + (f", {ref.deleted}" if ref.deleted else "")
          + (" (kept — DELETE it when done: it is a copy of a customer document)"
             if ref.created and args.keep_reference and ref.reference_id else ""))
    for cand in cands:
        verdict, score = rr.overall(cand.guided)
        print(f"{cand.document.name}: {verdict or cand.guided.phase} "
              f"{score if score is not None else ''} — "
              f"{ANSWERS.get(verdict or '', cand.guided.error or 'no answer')}")
        if cand.changed is not None:
            before = rr.overall(cand.baseline)
            print(f"    without the reference: {before[0]} {before[1]} — "
                  + ("the reference CHANGED the verdict" if cand.changed
                     else "same verdict either way"))
        def shown(side: Optional[dict]) -> str:
            if side is None:
                return "—"
            return str(side["score"] if side.get("score") is not None else side.get("status"))

        for row in comparison(cand, spec):
            print(f"    {shown(row['baseline']):>6} → {shown(row['guided']):<6}"
                  f" {rr._clip(row['name'], 60)}")
        for result in (cand.baseline, cand.guided):
            for note in (result.notes if result else []):
                print(f"    note: {rr._short(note, 200)}")
    met, total, skipped = _tally(ref, cands)
    print(f"\n{met}/{total} checks met" + (f" ({skipped} skipped)" if skipped else ""))
    print(f"report: {(out_root / 'index.html').as_uri()}")
    return 0 if met == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
