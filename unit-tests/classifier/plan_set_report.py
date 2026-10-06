#!/usr/bin/env python3
"""Ask the classifier whether a document is a solar plan set, and show why.

``regions_report.py`` runs the Postman collection. This script asks ONE
question of the documents passed on the command line — *is this a solar PV
permit plan set?* — and renders the answer with the evidence behind it:

    for each document:
      POST /assess   every page, criteria = solar_plan_set_criteria.json
      → poll → download regions.json + text layers → re-draw the hits
      → index.html + summary.json + exit code

The criteria (``solar_plan_set_criteria.json``) judge the document AS A
WHOLE, not page by page:

  * six ``text`` criteria with ``scope: "document"`` — every page's text
    joined and searched once: "photovoltaic|solar", PV/E/S sheet numbers,
    NEC 690, a kW DC system size, a line diagram, a site or roof plan.
    Deterministic and free; each hit is drawn on the page it landed on;
  * one ``llm`` criterion — "this page is the cover sheet …" — run on every
    page with ``hint: presence`` and ``aggregate: any``, so it passes when
    SOME page is the cover sheet. A plan set's equipment spec sheets are not
    plan sheets, and the default ``worst`` rule would let them fail it. It
    ``depends_on`` the solar mention, so a document that never mentions solar
    costs no model calls.

The overall verdict is the classifier's weighted score of those seven: PASS
reads as "yes, a plan set". The report puts that answer first, then a table
of what each criterion found (hit counts, the pages they landed on, the page
the model picked as the cover sheet), then the generic per-criterion and
per-page tables and the drawn hits from ``regions_report.py``.

**Every page is one item, and the service caps a request at
``CLASSIFIER_MAX_ITEMS``** (20 by default). A longer plan set is refused at
submit with a 400 — the report says so and names the knob; raise it on the
box and ``make up classifier``. Splitting the PDF is not a fix: each half
would be judged on its own.

Usage (from the repo root)::

    uv run --package classifier python unit-tests/classifier/plan_set_report.py \\
        "~/Downloads/REV0 - Plan set - Marvin Ryder.pdf" --expect PASS
    # in-process, no vision model: the llm criterion is dropped, text still runs
    uv run --package classifier python unit-tests/classifier/plan_set_report.py \\
        some_plan_set.pdf --local

Documents are passed in, never committed: a real plan set carries a
customer's name and address. ``--expect PASS|MARGINAL|FAIL`` asserts the
overall verdict of every document given; without it the verdict is reported
and its check recorded as skipped.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import pathlib
import sys
from typing import Any, Optional

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import regions_report as rr  # noqa: E402 — after the path insert; it sets up `common`

from common.env import load_env  # noqa: E402

DEFAULT_CRITERIA = HERE / "solar_plan_set_criteria.json"
FOLDER = "Solar plan set"

# Overall verdict → the answer to the question this script asks.
ANSWERS = {
    "PASS": "yes — a solar plan set",
    "MARGINAL": "uncertain — some plan-set evidence, not enough",
    "FAIL": "no — not a solar plan set",
}


# ---------------------------------------------------------------------------
# Running one document
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PlanSetResult:
    """One document's /assess call, read for the plan-set question."""

    document: pathlib.Path
    pages: Optional[int]
    result: rr.CaseResult
    evidence: list[dict] = dataclasses.field(default_factory=list)
    checks: list[dict] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)


def page_count(path: pathlib.Path) -> Optional[int]:
    """Pages the service will count as items — a PDF's page count, 1 for
    anything else. None when PyMuPDF cannot open it (the service will say
    why at submit)."""
    if path.suffix.lower() != ".pdf":
        return 1
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001 — the submit is the authority on bad files
        return None


def server_max_items(transport: Any) -> Optional[int]:
    """The live ``CLASSIFIER_MAX_ITEMS`` from ``GET /document-kinds``."""
    try:
        response = transport.request("GET", "/v1/classifier/document-kinds")
        if response.status_code == 200:
            return (response.json().get("limits") or {}).get("max_items")
    except Exception:  # noqa: BLE001 — only used to explain a 400
        pass
    return None


def run_document(
    document: pathlib.Path,
    criteria: list[dict],
    transport: Any,
    out_root: pathlib.Path,
    args: argparse.Namespace,
    *,
    index: int,
    max_items: Optional[int],
) -> PlanSetResult:
    pages = page_count(document)
    case = rr.Case(
        name=f"solar plan set? — {document.name}",
        folder=FOLDER,
        index=index,
        method="POST",
        path="/v1/classifier/assess",
        description=(
            "One /assess call over every page. Six document-scope text criteria "
            "search the joined text of all pages; one llm criterion asks each page "
            "whether it is the plan set's cover sheet and passes if any page is. "
            "Expect PASS on a solar PV permit plan set, FAIL on anything else."
        ),
        headers={},
        form={"criteria": rr._compact(criteria)},
        uploads=[("file", document)],
        subjects=[document],
        adhoc=True,
    )
    result = rr.run_case(case, transport, out_root, args)
    plan = PlanSetResult(document=document, pages=pages, result=result)

    if result.phase == "rejected" and pages and max_items and pages > max_items:
        result.notes.append(
            f"{document.name} has {pages} pages and this classifier accepts "
            f"{max_items} items per request (CLASSIFIER_MAX_ITEMS). Raise it in the "
            "box's .env and `make up classifier` — splitting the PDF would judge "
            "each half on its own"
        )

    plan.evidence = read_evidence(result, criteria)
    return plan


def read_evidence(result: rr.CaseResult, criteria: list[dict]) -> list[dict]:
    """One row per REQUESTED criterion: what it found and where.

    Pages are reported 1-based, the way a person reads a plan set — the
    service's items are 0-based, and the one document's item n is page n+1.
    """
    scores = rr.criterion_results(result)
    rows = []
    for criterion in criteria:
        name = criterion["name"]
        kind = criterion.get("type") or "llm"
        entry = scores.get(name)
        row: dict[str, Any] = {
            "name": name,
            "type": kind,
            "weight": criterion.get("weight", 1),
            "status": None,
            "verdict": None,
            "score": None,
            "found": "",
        }
        rows.append(row)
        if entry is None:
            row["found"] = (
                "dropped by --local (no vision model)"
                if name in result.dropped_criteria
                else "not in the result"
            )
            continue

        row.update(status=entry.get("status"), verdict=entry.get("verdict"),
                   score=entry.get("score"))
        if entry.get("status") == "error":
            row["found"] = f"error: {entry.get('error')}"
        elif entry.get("status") == "skipped":
            row["found"] = entry.get("reason") or "skipped"
        elif kind == "text":
            row.update(_text_evidence(entry))
        elif kind == "llm":
            row.update(_llm_evidence(entry))
        else:
            row["found"] = entry.get("reason") or ""
    return rows


def _text_evidence(entry: dict) -> dict:
    detail = entry.get("detail") or {}
    count, wanted = detail.get("count") or 0, detail.get("min_count") or 1
    pages = [int(i) + 1 for i in detail.get("items_with_hits") or []]
    snippets = [s.get("text", "") for s in detail.get("snippets") or []][:2]
    return {
        "found": f"{count} hit(s), {wanted} needed"
                 + (f" · pages {_ranges(pages)}" if pages else ""),
        "pages": pages,
        "count": count,
        "snippets": snippets,
    }


def _llm_evidence(entry: dict) -> dict:
    """The page the model rated most like the cover sheet, and how many
    pages it called one — a plan set has exactly one."""
    units = [u for u in entry.get("items") or [] if u.get("status") == "ok"]
    if not units:
        return {"found": entry.get("reason") or "no page was evaluated"}
    best = max(units, key=lambda u: u.get("score") or 0)
    passing = [int(u["item"]) + 1 for u in units if u.get("verdict") == "PASS"]
    return {
        "found": f"best page {int(best['item']) + 1} (score {best.get('score')}) · "
                 f"{len(passing)} of {len(units)} page(s) PASS"
                 + (f": {_ranges(passing)}" if passing else ""),
        "pages": passing,
        "best_page": int(best["item"]) + 1,
        "snippets": [best.get("reason") or ""],
    }


def _ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 5, 7, 8] → "1–3, 5, 7–8"."""
    spans: list[list[int]] = []
    for n in sorted(set(numbers)):
        if spans and spans[-1][1] == n - 1:
            spans[-1][1] = n
        else:
            spans.append([n, n])
    return ", ".join(f"{a}" if a == b else f"{a}–{b}" for a, b in spans)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check(plan: PlanSetResult, expect: Optional[str]) -> None:
    result = plan.result
    if result.phase != "completed":
        plan.checks.append({
            "kind": "phase", "target": "job", "ok": False,
            "expected": "completed",
            "actual": f"{result.phase}: {result.error or 'no error reported'}",
        })
        return

    verdict, score = rr.overall(result)
    if expect is None:
        plan.checks.append({
            "kind": "verdict", "target": "overall", "ok": True, "skipped": True,
            "expected": "nothing (no --expect)", "actual": f"{verdict} {score}",
        })
        return
    if result.dropped_criteria:
        result.notes.append(
            "the overall verdict was computed without the dropped llm criterion, "
            "from the text criteria alone"
        )
    plan.checks.append({
        "kind": "verdict", "target": "overall", "ok": verdict == expect,
        "expected": expect, "actual": f"{verdict} {score}",
    })


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _answer_block(plan: PlanSetResult) -> str:
    result = plan.result
    verdict, score = rr.overall(result)
    e = rr._e
    parts = [
        f'<h2 id="plan-{e(result.case.slug)}">Is it a solar plan set? — {e(plan.document.name)}</h2>',
        f'<p class="meta">document <code>{e(rr._rel(plan.document))}</code> · '
        f"{e(plan.pages if plan.pages is not None else '?')} page(s) · one "
        f"<code>POST /assess</code> · {result.elapsed_s:.1f}s"
        f' · <a href="#{e(result.case.slug)}">the call</a></p>',
    ]
    if verdict:
        parts.append(
            f'<div class="answer"><strong>{e(ANSWERS.get(verdict, verdict))}</strong> '
            f"· {rr._verdict_pill(verdict)} weighted score {e(score)}</div>"
        )
    else:
        parts.append(
            f'<div class="err"><strong>no answer</strong> — {e(result.phase)}: '
            f"{e(result.error or 'the assessment is incomplete (a criterion errored)')}</div>"
        )
    for note in result.notes:
        parts.append(f'<div class="notes">{e(note)}</div>')

    rows = []
    for row in plan.evidence:
        status = row["status"]
        if status in (None, "ok"):
            pill = rr._verdict_pill(row["verdict"])
        else:
            pill = f'<span class="pill {"SKIPPED" if status == "skipped" else "FAIL"}">{e(status)}</span>'
        snippets = "".join(
            f"<div class='mono skip'>{e(rr._short(s, 160))}</div>" for s in row.get("snippets") or []
        )
        rows.append(
            "<tr>"
            f"<td>{e(row['name'])}</td><td>{e(row['type'])}</td><td>{e(row['weight'])}</td>"
            f"<td>{pill} {e(row['score'] if row['score'] is not None else '')}</td>"
            f"<td>{e(row['found'])}{snippets}</td>"
            "</tr>"
        )
    parts.append(
        "<h3>Evidence</h3>"
        "<table><thead><tr><th>criterion</th><th>type</th><th>weight</th><th>result</th>"
        "<th>what it found (pages are 1-based)</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )
    if plan.checks:
        parts.append("<h3>Checks</h3>")
        parts.append(rr._checks_table(plan.checks))
    return "\n".join(parts)


def _call_block(result: rr.CaseResult) -> str:
    """The call itself, with the generic tables from regions_report."""
    e = rr._e
    verdict, score = rr.overall(result)
    parts = [
        f'<h2 id="{e(result.case.slug)}">{e(result.case.name)}</h2>',
        f'<p class="meta"><code>POST {e(result.case.endpoint)}</code> · phase '
        f"<strong>{e(result.phase)}</strong> · overall {rr._verdict_pill(verdict)} "
        f"{e(score if score is not None else '')}"
        + (f" · job <code>{e(result.job_id)}</code>" if result.job_id else "")
        + "</p>",
    ]
    if result.error:
        parts.append(f'<div class="err"><strong>error:</strong> {e(result.error)}</div>')
    parts += ["<h3>Request</h3>", rr._criteria_request_table(result.case),
              "<h3>Result</h3>", rr._criteria_result_table(result, {})]
    per_item = rr._items_table(result)
    if per_item:
        parts += ["<h3>Per page</h3>", per_item]
    pages = rr._pages_block(result)
    if pages:
        parts += [
            "<h3>Where the text hits landed</h3>",
            "<p class='meta'>Left: drawn by this script from <code>regions.json</code> onto "
            "the document on disk. Right: the service's own preview. Only pages with a "
            "hit are drawn.</p>",
            pages,
        ]
    parts.append(rr._links_block(result))
    return "\n".join(parts)


def write_html(path: pathlib.Path, plans: list[PlanSetResult], meta: dict) -> None:
    e = rr._e
    met = sum(1 for p in plans for c in p.checks if c["ok"])
    total = sum(len(p.checks) for p in plans)
    rows = []
    for plan in plans:
        verdict, score = rr.overall(plan.result)
        rows.append(
            "<tr>"
            f'<td><a href="#plan-{e(plan.result.case.slug)}">{e(plan.document.name)}</a></td>'
            f"<td>{e(plan.pages)}</td><td>{e(plan.result.phase)}</td>"
            f"<td>{rr._verdict_pill(verdict)} {e(score if score is not None else '')}</td>"
            f"<td>{e(ANSWERS.get(verdict or '', '—'))}</td>"
            f"<td>{plan.result.elapsed_s:.1f}s</td>"
            "</tr>"
        )
    parts = [
        "<!-- generated by unit-tests/classifier/plan_set_report.py -->",
        f"<title>Solar plan set check — {e(meta['generated_at'])}</title>",
        f"<style>{rr.CSS}</style>",
        "<h1>Solar plan set check</h1>",
        f'<p class="meta">{e(meta["generated_at"])} · mode <strong>{e(meta["mode"])}</strong>'
        f' · {e(meta["base_url"])} · criteria <code>{e(meta["criteria"])}</code></p>',
        "<table><thead><tr><th>document</th><th>pages</th><th>phase</th><th>verdict</th>"
        "<th>answer</th><th>elapsed</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>",
        f'<p class="tally">{met}/{total} checks met</p>',
    ]
    parts += [_answer_block(plan) for plan in plans]
    parts += [_call_block(plan.result) for plan in plans]
    path.write_text("\n".join(parts) + "\n", encoding="utf-8", newline="\n")


def write_summary_json(path: pathlib.Path, plans: list[PlanSetResult], meta: dict) -> None:
    payload = dict(meta, documents=[])
    for plan in plans:
        verdict, score = rr.overall(plan.result)
        payload["documents"].append({
            "document": rr._rel(plan.document),
            "pages": plan.pages,
            "job_id": plan.result.job_id,
            "http_status": plan.result.http_status,
            "phase": plan.result.phase,
            "verdict": verdict,
            "score": score,
            "answer": ANSWERS.get(verdict or ""),
            "elapsed_s": round(plan.result.elapsed_s, 2),
            "evidence": plan.evidence,
            "checks": plan.checks,
            "notes": plan.result.notes,
            "error": plan.result.error,
            "dir": plan.result.case.slug,
        })
    rr._write_json(path, payload)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("documents", type=pathlib.Path, nargs="+",
                        help="Documents to check (a PDF plan set, usually)")
    parser.add_argument("--criteria", type=pathlib.Path, default=DEFAULT_CRITERIA,
                        help="Criteria JSON (default: solar_plan_set_criteria.json)")
    parser.add_argument("--expect", choices=("PASS", "MARGINAL", "FAIL"),
                        help="Assert this overall verdict for every document")
    parser.add_argument("--base-url", help="Classifier base URL, LiteLLM pass-through included "
                                           "(default: CLASSIFIER_BASE_URL, else "
                                           f"{rr.DEFAULT_BASE_URL})")
    parser.add_argument("--api-key", help="Bearer token (default: CLASSIFIER_API_KEY, else "
                                          "DEFAULT_LITELLM_MASTER_KEY)")
    parser.add_argument("--out", type=pathlib.Path,
                        help="Output directory (default: "
                             "unit-tests/classifier/reports/plan-set-<UTC timestamp>/)")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="Seconds to wait for one job — one vision call per page, "
                             "so a long plan set takes a while")
    parser.add_argument("--local", action="store_true",
                        help="Mount ai/classifier/main.py in-process. No vision model, so "
                             "the llm criterion is dropped and the text criteria decide")
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

    documents = [p.expanduser().resolve() for p in args.documents]
    for path in documents:
        if not path.is_file():
            raise SystemExit(f"document not found: {path}")
    criteria = json.loads(args.criteria.read_text(encoding="utf-8"))

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_root = (args.out or (rr.DEFAULT_REPORTS_DIR / f"plan-set-{stamp}")).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"{len(documents)} document(s) → {out_root}")
    print(f"mode: {'local (in-process TestClient)' if args.local else base_url}\n")

    transport_factory = (
        (lambda: rr.LocalTransport(out_root / "_service"))
        if args.local
        else (lambda: rr.HttpTransport(base_url, api_key))
    )

    plans: list[PlanSetResult] = []
    with transport_factory() as transport:
        max_items = server_max_items(transport)
        for index, document in enumerate(documents, start=1):
            pages = page_count(document)
            print(f"  · {document.name} ({pages} page(s), server max {max_items})", flush=True)
            # Each run_case may drop criteria in --local; give it its own copy.
            plan = run_document(
                document, json.loads(json.dumps(criteria)), transport, out_root, args,
                index=index, max_items=max_items,
            )
            check(plan, args.expect)
            plans.append(plan)

    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base_url": "in-process" if args.local else base_url,
        "mode": "local" if args.local else "http",
        "criteria": rr._rel(args.criteria.resolve()),
    }
    write_html(out_root / "index.html", plans, meta)
    write_summary_json(out_root / "summary.json", plans, meta)

    print()
    for plan in plans:
        verdict, score = rr.overall(plan.result)
        print(f"{plan.document.name}: {verdict or plan.result.phase} {score if score is not None else ''}"
              f" — {ANSWERS.get(verdict or '', plan.result.error or 'no answer')}")
        for row in plan.evidence:
            print(f"    {str(row['verdict'] or row['status'] or '—'):8} "
                  f"{rr._clip(row['name'], 48):48}  {row['found']}")
        for note in plan.result.notes:
            print(f"    note: {rr._short(note, 200)}")
    met = sum(1 for p in plans for c in p.checks if c["ok"])
    total = sum(len(p.checks) for p in plans)
    print(f"\n{met}/{total} checks met")
    print(f"report: {(out_root / 'index.html').as_uri()}")
    return 0 if met == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
