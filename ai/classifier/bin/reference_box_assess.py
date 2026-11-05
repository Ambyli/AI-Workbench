"""Measure the box loop END TO END, through the service's own API (ZT-527).

``bin/grounding_experiment.py`` calls ``llm.boxes.locate_criterion`` directly,
handing it examples it builds itself. That measures the loop, and nothing
around it: not the stored references (``POST /references``), not the key
matching that finds a criterion's examples (``references.resolve``), not
``analysis.references.box_selection`` (a FAIL example reaches the loop only
when its REFERENCE criterion's hint is ``presence``), and not the
per-criterion options (``options.refine``, ``options.reference.boxes``, …)
that decide how each criterion runs the loop. This script measures the same
pages and truth the production way — over HTTP, with real references, real
``/assess`` jobs and the job result as the only output read:

    1. references  ONE stored reference per (truth page, criterion,
                   polarity) that can teach something — PASS when the page
                   has a truth box for the criterion (expected PASS, score
                   10, the region its FIRST box), FAIL when it has a box under
                   the decoy ``--fail-from-decoys`` maps to the criterion
                   (expected FAIL, score 1, the region its first decoy box).
                   Each holds one criterion: the production locate criterion
                   as a SCORED copy (same name and ``key``, ``type: llm``,
                   ``hint: presence``, ``boxes: true``) — a ``score: false``
                   criterion never becomes a usable example. Fully supplied
                   (answer, region, a GENERIC title and description, tag
                   ``zt527-e2e``), so creating one costs no model call. The
                   run refuses to start when the store would pass
                   ``CLASSIFIER_REFERENCE_MAX_COUNT``, waits until every
                   reference is ``ready``, and leaves a ``failed`` one out
                   (its id and error kind are reported).
    2. jobs        ONE ``/assess`` job per (candidate page, repeat): the page,
                   and every criterion the truth asks of it (``absent`` ones
                   included) as the production locate criterion —
                   ``hint: presence``, ``score: false``, ``boxes: true``, its
                   ``key`` (``--key``) and its ``--options`` overlay — with
                   ``references``: per criterion, ONE PASS reference and (when
                   mapped) ONE FAIL reference, from pages outside the
                   candidate's group (never the candidate), picked exactly as
                   the experiment picks them (``grounding_experiment.
                   _truth_reference`` / ``_truth_fail_reference``:
                   deterministic, rotated across repeats, PASS and FAIL
                   independently). A FAIL is sent only beside a PASS and only
                   to a model with 3 images per prompt, as the experiment
                   does. ``--no-references`` sends the same jobs without
                   references (and without any ``options.reference``): the
                   baseline through the same path. At most ``--concurrency``
                   jobs are in flight; the service's queue and limits do the
                   rest.
    3. scoring     from each job's result alone: the criterion's ACCEPTED
                   attempt (``localization.attempts``, ``bbox_px`` in page
                   pixels) against the truth, exactly as the experiment
                   scores a run (``grounding_experiment._against_truth``): a
                   HIT is its centre inside any copy; an ``absent`` pair is
                   CORRECT when nothing was accepted; ``landed_on`` names the
                   other criterion or decoy a wrong box landed on. Plus the
                   loop's calls, and from ``localization.references`` whether
                   the examples reached the loop, which steps carried them,
                   and their polarities — so a FAIL that the service dropped
                   (a reference criterion whose hint is not presence, a model
                   with 2 images) shows as not reached.
    4. clean-up    in a ``finally``, whatever happened (an error, Ctrl-C):
                   every job this run made is deleted (``DELETE /jobs/{id}`` —
                   each finished job right after its record is in ``--out``),
                   the references' creation jobs too, and every reference it
                   created (``DELETE /references/{id}?force=true`` — they are
                   this run's own). ``--keep-references`` keeps the
                   references, for debugging only; ``--purge`` deletes every
                   reference tagged ``zt527-e2e`` (after a run that was
                   killed outright) and exits.

**How references and the candidate's criteria meet.** Each reference holds
ONE criterion, keyed like the candidate's (``--key``; without one, the names
match). ``references.resolve`` finds a criterion's examples among the listed
references by its key (a keyed criterion never falls back to its name), so
listing one PASS and one FAIL reference per criterion gives that criterion
exactly those two examples and no other criterion any of them — a reference
holding several criteria would hand its other criteria to the candidate's
other criteria too. The FAIL example is ``polarity: fail`` with hint
``presence``, so ``box_selection`` shows it to the loop (at three images per
prompt) after the PASS one: *"… was FAIL here — the outlined thing is NOT
…; do not box it."* A page with both a truth box and a mapped decoy box for
one criterion gets two references, one of each.

**What it never prints or writes**: truth boxes, images, OCR text, model
reasons (``verify_reason``, the scoring ``reason``), or page content in a
reference's title, description or breakdown reason. It prints ids, counts
and rates. ``--out`` holds, per run (candidate page x criterion x repeat):
the page file name, criterion, key, repeat, config label, job id, the
reference ids sent (and the pages they came from), the outcome (``accepted``,
``bbox_px`` — the model's accepted box, ``expected`` / ``hit`` / ``correct``
/ ``iou`` / ``landed_on``), the loop's calls and attempts (boxes, scores,
the service's own rejection sentences — no reasons), what the loop and the
scoring call were shown (applied, steps, polarities), and the box-loop
settings that ran (``options_used``).

**It runs where the service is**, in the classifier container, against its
own uvicorn (``--base-url``, default ``http://127.0.0.1:8000``)::

    # The groups (one per project) — once per test set
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py \\
        --groups-from-selection /data/grounding_ops/selection.json \\
        --write-groups /data/grounding_ops/groups.json

    # Config 1 — the baseline: the same jobs, no references, no options
    docker exec classifier uv run --no-sync python bin/reference_box_assess.py \\
        --images /data/grounding_ops/images --truth /data/grounding_ops/truth.json \\
        --groups /data/grounding_ops/groups.json --repeats 5 \\
        --key "total the customer owes=amount_due_figure" \\
        --key "usage history=usage_history" \\
        --no-references --label baseline --out /data/grounding_ops/e2e_baseline.json

    # Config 2 — per-criterion options plus references (marks for the amount,
    # examples for both, a FAIL example of the usage look-alike)
    docker exec classifier uv run --no-sync python bin/reference_box_assess.py \\
        --images /data/grounding_ops/images --truth /data/grounding_ops/truth.json \\
        --groups /data/grounding_ops/groups.json --repeats 5 \\
        --key "total the customer owes=amount_due_figure" \\
        --key "usage history=usage_history" \\
        --options 'total the customer owes={"refine":"marks","reference":{"boxes":{"ask":true,"refine":true,"verify":true}}}' \\
        --options 'usage history={"reference":{"boxes":{"ask":true,"refine":true,"verify":true}}}' \\
        --fail-from-decoys "lookalike false usage history=usage history" \\
        --label guided --out /data/grounding_ops/e2e_guided.json

The truth file is the experiment's (``grounding_experiment._load_truth``:
``criteria``, ``decoys``, ``absent``), and so are ``--groups`` and
``--subset``. ``--key`` / ``--options`` / ``--fail-from-decoys`` name a
criterion by its exact name, else the one equal to it ignoring case, else the
ONE containing it ignoring case (unknown or ambiguous is refused). The loop
runs IN THE SERVICE with the SERVICE's settings: an env knob such as
``CLASSIFIER_LLM_BBOX_MARKS_MAX`` is the classifier container's (its ``.env``
and ``make up classifier``), never this process's.

What it prints, per criterion: runs; for pairs with truth boxes the hit rate
and false accepts (accepted, not a hit); for ``absent`` pairs the correct
rate (nothing accepted) and wrong accepts; where wrong boxes landed; how often
the loop ran, was shown examples, and was shown a FAIL example (of the runs
that were sent one); the mean loop calls; unit errors, by kind (the error's text before its
first colon, never the detail); and with ``refine:
"marks"`` how many refines the marks pass made and how many fell back
because the window held more OCR lines than the service's
``CLASSIFIER_LLM_BBOX_MARKS_MAX`` — the number that says whether 40 is
enough.

Process flow position: none — a standalone operator tool, an HTTP client of
the running service. It imports ``grounding_experiment`` for the truth
format, the picks and the scoring (so both measure the same way), and
``api.schemas`` only to check the locate criteria before anything is
created.
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

_BIN = pathlib.Path(__file__).resolve().parent
for _path in (str(_BIN.parent), str(_BIN)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import grounding_experiment as ge  # noqa: E402
from common.vision import PageGeometry  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
TAG = "zt527-e2e"
# Fixed wording: a reference's title, description and breakdown reasons say
# nothing about its page (they become the model's caption).
REASONS = {
    "pass": "The outlined area is an example of this criterion.",
    "fail": "The outlined area is a look-alike: it is not this criterion.",
}
DESCRIPTION = (
    "zt527 e2e measurement reference: one {polarity} example of one criterion, "
    "created and deleted by bin/reference_box_assess.py"
)
NO_PASS_REFERENCE = (
    "no ready PASS reference for this criterion outside the candidate's group — unguided"
)
FAIL_NEEDS_PASS = "no PASS reference for this pair — a FAIL is only sent beside one"
FAIL_NEEDS_ROOM = (
    "the job's model takes {images} image(s) per prompt; PASS + FAIL + the candidate need 3 "
    "— PASS only"
)
NO_FAIL_REFERENCE = (
    "no ready FAIL reference ({decoy!r} decoy) outside the candidate's group — PASS only"
)
# The loop's own sentence for "the model answered bbox: null".
_BBOX_NULL = "you answered bbox: null"


# ---------------------------------------------------------------------------
# Naming things without their content
# ---------------------------------------------------------------------------


def page_id(name: str) -> str:
    """A page's short id — ``p`` + crc32 of its file name — for a reference's
    title: the file name itself never goes into anything the model sees."""
    return f"p{zlib.crc32(name.encode('utf-8')):08x}"


def reference_title(page: str, polarity: str) -> str:
    return f"zt527 e2e reference {page_id(page)} {polarity}"


def match_criterion(value: str, names: list[str], flag: str, spec: str) -> str:
    """``value`` → the one criterion it names: exact, else equal ignoring
    case, else the ONE containing it ignoring case — the rule
    ``grounding_experiment._fail_map`` uses. Unknown or ambiguous exits."""
    if value in names:
        return value
    folded = [n for n in names if n.casefold() == value.casefold()]
    matches = folded or [n for n in names if value.casefold() in n.casefold()]
    if not matches:
        raise SystemExit(f"{flag} {spec!r}: {value!r} matches no criterion "
                         f"(criteria: {', '.join(map(repr, names)) or 'none'})")
    if len(matches) > 1:
        raise SystemExit(f"{flag} {spec!r}: {value!r} is ambiguous — it matches "
                         f"{', '.join(map(repr, matches))}")
    return matches[0]


def parse_keys(specs: list[str], names: list[str]) -> dict[str, str]:
    """``--key CRITERION=KEY`` (repeatable) → ``{criterion: key}``."""
    out: dict[str, str] = {}
    for spec in specs or []:
        wanted, sep, key = (part.strip() for part in spec.partition("="))
        if not sep or not wanted or not key:
            raise SystemExit(f"--key {spec!r}: expected CRITERION_SUBSTRING=FACET_KEY")
        criterion = match_criterion(wanted, names, "--key", spec)
        if criterion in out:
            raise SystemExit(f"--key {spec!r}: {criterion!r} already has key {out[criterion]!r}")
        out[criterion] = key
    return out


def parse_options(specs: list[str], names: list[str]) -> dict[str, dict]:
    """``--options CRITERION=JSON`` (repeatable) → ``{criterion: overlay}``,
    each a JSON object merged into that criterion's ``options``."""
    out: dict[str, dict] = {}
    for spec in specs or []:
        wanted, sep, raw = spec.partition("=")
        wanted = wanted.strip()
        if not sep or not wanted or not raw.strip():
            raise SystemExit(f"--options {spec[:60]!r}: expected CRITERION_SUBSTRING=JSON")
        try:
            overlay = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--options for {wanted!r}: not JSON ({exc})")
        if not isinstance(overlay, dict):
            raise SystemExit(f"--options for {wanted!r}: the overlay is a JSON object")
        criterion = match_criterion(wanted, names, "--options", wanted)
        if criterion in out:
            raise SystemExit(f"--options: {criterion!r} given twice")
        out[criterion] = overlay
    return out


def merged(base: dict, overlay: dict) -> dict:
    """``overlay`` merged into a copy of ``base``, dicts recursively."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# ---------------------------------------------------------------------------
# The criteria: the production locate criterion, and its scored copy
# ---------------------------------------------------------------------------


def locate_criterion(
    name: str, key: Optional[str], overlay: Optional[dict] = None, *, references: bool = True,
) -> dict:
    """The production locate criterion (ZT-526's): ``hint: presence``,
    ``score: false``, ``boxes: true``, its ``key``, and the ``--options``
    overlay on its options. Without references on the job the overlay's
    ``reference`` is left out — ``options.reference`` without
    ``references`` is a 400."""
    options = merged({"hint": "presence", "boxes": True}, overlay or {})
    if not references:
        options.pop("reference", None)
    criterion: dict[str, Any] = {"name": name, "type": "llm", "score": False,
                                 "options": options}
    if key is not None:
        criterion["key"] = key
    return criterion


def reference_criterion(name: str, key: Optional[str]) -> dict:
    """A reference's criterion: the locate criterion as a SCORED copy, so it
    becomes a usable example. No overlay — the box-loop settings are the
    candidate's, and a reference's criteria may not carry
    ``options.reference``."""
    criterion: dict[str, Any] = {"name": name, "type": "llm", "score": True,
                                 "options": {"hint": "presence", "boxes": True}}
    if key is not None:
        criterion["key"] = key
    return criterion


# ---------------------------------------------------------------------------
# Step 1 — the references
# ---------------------------------------------------------------------------


@dataclass
class RefSpec:
    """One reference to create: ``page``'s example of ``criterion``."""

    page: str
    criterion: str
    polarity: str                      # "pass" | "fail"
    box: list[float]
    decoy: Optional[str] = None        # the FAIL example's decoy name
    reference_id: Optional[str] = None
    job_id: Optional[str] = None
    status: str = "planned"            # planned → pending → ready | failed
    error_kind: Optional[str] = None


def plan_references(
    truth: dict[str, ge.Truth], pages: set[str], labels: list[str], fail_map: dict[str, str],
) -> list[RefSpec]:
    """Every (page, criterion, polarity) a reference can teach, over the
    truth pages present under ``--images``: PASS from the criterion's first
    truth box, FAIL from the first box of its mapped decoy. A page with
    neither for a criterion teaches it nothing; with both, it gets one of
    each. Sorted, so a run creates them in a stable order."""
    specs: list[RefSpec] = []
    for page in sorted(p for p in truth if p in pages):
        entry = truth[page]
        for label in labels:
            copies = entry["criteria"].get(label)
            if copies:
                specs.append(RefSpec(page, label, "pass", list(copies[0])))
            decoy = fail_map.get(label)
            if decoy and entry["decoys"].get(decoy):
                specs.append(RefSpec(page, label, "fail", list(entry["decoys"][decoy][0]),
                                     decoy=decoy))
    return specs


def image_size(raw: bytes) -> tuple[int, int]:
    """An image's (width, height) AFTER EXIF rotation, from its header — the
    frame the truth boxes (``cv2.imread``) and the service's regions are in."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        width, height = img.size
        orientation = img.getexif().get(0x0112)
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    return int(width), int(height)


def clamped(box: list[float], size: tuple[int, int]) -> list[float]:
    """A truth box held inside the page (the service allows one pixel of
    overshoot; a hand-drawn box may have more)."""
    width, height = size
    x1, y1, x2, y2 = box
    return [max(0.0, min(float(width), x1)), max(0.0, min(float(height), y1)),
            max(0.0, min(float(width), x2)), max(0.0, min(float(height), y2))]


def reference_form(spec: RefSpec, key: Optional[str], size: tuple[int, int]) -> dict[str, str]:
    """The multipart fields of ``spec``'s ``POST /references`` (the page is
    the ``file`` part): one scored criterion, its answer (PASS 10 / FAIL 1,
    a fixed reason) and its region in page pixels, a generic title and
    description, the run's tag — nothing from the page itself."""
    score = 10 if spec.polarity == "pass" else 1
    return {
        "criteria": json.dumps([reference_criterion(spec.criterion, key)]),
        "breakdown": json.dumps({spec.criterion: {"score": score,
                                                  "reason": REASONS[spec.polarity]}}),
        "regions": json.dumps({spec.criterion: [{"box": clamped(spec.box, size)}]}),
        "region_units": "px",
        "title": reference_title(spec.page, spec.polarity),
        "description": DESCRIPTION.format(polarity=spec.polarity.upper()),
        "tags": json.dumps([TAG]),
    }


def error_kind(error: Any) -> Optional[str]:
    """A reference's or a unit's error, reduced to its kind: the text before
    the first colon, at most 60 characters — never the rest, which may quote
    scores, warnings or the model's answer."""
    if not error:
        return None
    return str(error).split(":", 1)[0].strip()[:60] or "error"


# ---------------------------------------------------------------------------
# Step 2 — the jobs
# ---------------------------------------------------------------------------


@dataclass
class Guide:
    """The references one candidate criterion is sent on one repeat."""

    pass_id: Optional[str] = None
    pass_page: Optional[str] = None
    fail_id: Optional[str] = None
    fail_page: Optional[str] = None
    note: Optional[str] = None
    fail_note: Optional[str] = None


@dataclass
class JobSpec:
    """One ``/assess`` job: a candidate page on one repeat."""

    page: str
    repeat: int
    labels: list[str]
    guides: dict[str, Guide] = field(default_factory=dict)
    job_id: Optional[str] = None
    submitted_at: Optional[float] = None

    def reference_ids(self) -> list[str]:
        """The ``references`` list: per criterion its PASS then its FAIL id,
        in criterion order, de-duplicated."""
        ids: list[str] = []
        for label in self.labels:
            guide = self.guides.get(label) or Guide()
            for rid in (guide.pass_id, guide.fail_id):
                if rid and rid not in ids:
                    ids.append(rid)
        return ids


def plan_jobs(
    candidates: dict[str, list[str]],
    repeats: int,
    truth: dict[str, ge.Truth],
    groups: dict[str, frozenset[str]],
    ready: list[RefSpec],
    fail_map: dict[str, str],
    *,
    references: bool,
    max_images: int,
    max_per_request: int,
) -> list[JobSpec]:
    """One job per (candidate page, repeat), each criterion's references
    picked as the experiment picks its examples — the PASS page by
    ``ge._truth_reference``, the FAIL page by ``ge._truth_fail_reference``
    (its own salt, so the two are independent), both among the pages with a
    READY reference of that polarity, outside the candidate's group, never
    the candidate. Exits when one job would list more than
    ``max_per_request`` references (CLASSIFIER_REFERENCE_MAX_PER_REQUEST)."""
    pass_ids = {(s.page, s.criterion): s.reference_id for s in ready if s.polarity == "pass"}
    fail_ids = {(s.page, s.criterion): s.reference_id for s in ready if s.polarity == "fail"}
    jobs: list[JobSpec] = []
    for page in sorted(candidates):
        labels = candidates[page]
        for repeat in range(1, repeats + 1):
            job = JobSpec(page=page, repeat=repeat, labels=list(labels))
            if references:
                for label in labels:
                    job.guides[label] = _guide(page, label, repeat, truth, groups, pass_ids,
                                               fail_ids, fail_map, max_images)
                if len(job.reference_ids()) > max_per_request:
                    raise SystemExit(
                        f"page {page_id(page)} repeat {repeat}: {len(job.reference_ids())} "
                        f"references, over CLASSIFIER_REFERENCE_MAX_PER_REQUEST "
                        f"({max_per_request}); ask fewer criteria per job"
                    )
            jobs.append(job)
    return jobs


def _guide(
    page: str, label: str, repeat: int, truth: dict[str, ge.Truth],
    groups: dict[str, frozenset[str]], pass_ids: dict, fail_ids: dict,
    fail_map: dict[str, str], max_images: int,
) -> Guide:
    guide = Guide()
    passes = {p for (p, c) in pass_ids if c == label}
    guide.pass_page = ge._truth_reference(page, label, repeat, truth, groups, passes)
    if guide.pass_page is None:
        guide.note = NO_PASS_REFERENCE
    else:
        guide.pass_id = pass_ids[(guide.pass_page, label)]
    decoy = fail_map.get(label)
    if decoy is None:
        return guide
    if guide.pass_id is None:
        guide.fail_note = FAIL_NEEDS_PASS
    elif max_images < 3:
        guide.fail_note = FAIL_NEEDS_ROOM.format(images=max_images)
    else:
        fails = {p for (p, c) in fail_ids if c == label}
        guide.fail_page = ge._truth_fail_reference(page, decoy, repeat, truth, groups, fails)
        if guide.fail_page is None:
            guide.fail_note = NO_FAIL_REFERENCE.format(decoy=decoy)
        else:
            guide.fail_id = fail_ids[(guide.fail_page, label)]
    return guide


def job_form(
    job: JobSpec, keys: dict[str, str], overlays: dict[str, dict], model: Optional[str],
) -> dict[str, str]:
    """The multipart fields of ``job``'s ``POST /assess`` (the page is the
    ``file`` part): every criterion as the locate criterion, and the
    references list — left out when there is none to send (an empty list is
    a 400), and then without any ``options.reference``."""
    ids = job.reference_ids()
    fields = {"criteria": json.dumps([
        locate_criterion(label, keys.get(label), overlays.get(label), references=bool(ids))
        for label in job.labels
    ])}
    if ids:
        fields["references"] = json.dumps(ids)
    if model:
        fields["model"] = model
    return fields


# ---------------------------------------------------------------------------
# Step 3 — scoring one criterion of one job's result
# ---------------------------------------------------------------------------


# The attempt keys copied to --out: everything but `verify_reason` (the
# model's words).
_ATTEMPT_KEYS = ("attempt", "bbox_grid", "bbox_px", "valid", "accepted", "verify_score",
                 "reject", "coarse_bbox_grid", "refined", "refine_reject", "refine_mode",
                 "marks", "marks_shown", "marks_fallback", "verify_partial")
_LOOP_OPTIONS = ("refine", "verify_partial", "verify_min_span")


def score_entry(entry: Optional[dict], result: dict, label: str, truth_entry: ge.Truth) -> dict:
    """One criterion's outcome on one job, from its ``per_criterion_scores``
    entry (one item, so ``localization`` is that item's record verbatim):
    the accepted attempt against the truth (``ge._against_truth``), the
    loop's calls, what the loop was shown (``localization.references``) and
    what the scoring call was (``detail.reference``), and the box-loop
    settings that ran — never a reason."""
    if not isinstance(entry, dict):
        return {"status": "missing", **ge._scored(None)}
    loc = entry.get("localization") or {}
    attempts = [a for a in loc.get("attempts") or [] if isinstance(a, dict)]
    accepted = next((a for a in attempts if a.get("accepted")), None)
    geometry = (result.get("page_geometry") or [{}])[0] or {}
    frame = PageGeometry(page=0, width=int(geometry.get("width") or 1),
                         height=int(geometry.get("height") or 1), working_scale=1.0)
    bbox = list(accepted["bbox_px"]) if accepted and accepted.get("bbox_px") else None
    shown = (loc.get("references") or [None])[0] or {}
    polarities = [e.get("polarity") for e in shown.get("examples") or []]
    steps = {step: bool(shown.get(step)) for step in ("ask", "refine", "verify")}
    detail_reference = (entry.get("detail") or {}).get("reference") or {}
    used = entry.get("options_used") or {}
    used_reference = used.get("reference") or {}
    return {
        "status": entry.get("status"),
        "error_kind": error_kind(entry.get("error")),
        "loop_ran": bool(attempts),
        "accepted": accepted is not None,
        "accepted_attempt": loc.get("accepted_attempt"),
        "attempts": len(attempts),
        "calls": int(loc.get("calls") or 0),
        "bbox_null": any(str(a.get("reject") or "").startswith(_BBOX_NULL) for a in attempts),
        "bbox_px": bbox,
        **ge._against_truth(bbox, label, truth_entry, frame),
        "loop_references": {
            "applied": bool(shown.get("applied")),
            **steps,
            "ask_images": shown.get("ask_images"),
            "polarities": polarities,
            "reference_ids": [e.get("reference_id") for e in shown.get("examples") or []],
            "note": shown.get("note"),
        } if shown else None,
        "fail_reached": bool(shown.get("applied")) and "fail" in polarities and any(
            steps.values()),
        "scoring_reference": {
            "applied": bool(detail_reference.get("applied")),
            "polarities": [e.get("polarity") for e in detail_reference.get("examples") or []],
            "calls": len(detail_reference.get("calls") or []),
        } if detail_reference else None,
        "options_used": {
            **{key: used.get(key) for key in _LOOP_OPTIONS},
            "reference_boxes": used_reference.get("boxes"),
            "reference_text": used_reference.get("text"),
        },
        "attempt_log": [{k: a[k] for k in _ATTEMPT_KEYS if k in a} for a in attempts],
    }


def job_records(job: JobSpec, result: dict, truth: dict[str, ge.Truth],
                keys: dict[str, str], label_of_config: str) -> list[dict]:
    """The ``--out`` records of one finished job, one per criterion."""
    per = ((result.get("assessment") or {}).get("per_criterion_scores")) or {}
    truth_entry = truth.get(job.page) or {"criteria": {}, "decoys": {}, "absent": []}
    records = []
    for label in job.labels:
        guide = job.guides.get(label) or Guide()
        records.append({
            "image": job.page,
            "page_id": page_id(job.page),
            "criterion": label,
            "key": keys.get(label),
            "repeat": job.repeat,
            "config": label_of_config,
            "job_id": job.job_id,
            "references": job.reference_ids(),
            "pass_reference": {"id": guide.pass_id, "page": guide.pass_page},
            "fail_reference": {"id": guide.fail_id, "page": guide.fail_page},
            "reference_note": guide.note,
            "fail_reference_note": guide.fail_note,
            **score_entry(per.get(label), result, label, truth_entry),
        })
    return records


def failed_job_records(job: JobSpec, why: str, keys: dict[str, str], config: str) -> list[dict]:
    """The records of a job that never produced a result (failed, timed
    out): every criterion ``status: job_<why>``, nothing accepted."""
    return [{
        "image": job.page, "page_id": page_id(job.page), "criterion": label,
        "key": keys.get(label), "repeat": job.repeat, "config": config, "job_id": job.job_id,
        "references": job.reference_ids(), "status": f"job_{why}", "loop_ran": False,
        "accepted": False, "calls": 0, "fail_reached": False, **ge._scored(None),
    } for label in job.labels]


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def summarise(records: list[dict]) -> dict:
    """Per criterion and overall: hit rate and false accepts (pairs with
    truth boxes), correct rate and wrong accepts (``absent`` pairs), where
    wrong boxes landed, how often the loop ran / was guided / was shown a
    FAIL (of the runs sent one), mean loop calls, unit errors (and their
    kinds)."""

    def block(rows: list[dict]) -> dict:
        present = [r for r in rows if r.get("expected") == "present"]
        absent = [r for r in rows if r.get("expected") == "absent"]
        sent_fail = [r for r in rows if (r.get("fail_reference") or {}).get("id")]
        out: dict[str, Any] = {
            "n": len(rows),
            "errors": sum(1 for r in rows if r.get("status") != "ok"),
            # A unit's error kind, or the status itself for a job that never
            # finished (job_failed, job_timed_out) or a missing entry.
            "error_kinds": dict(Counter(r.get("error_kind") or r.get("status")
                                        for r in rows if r.get("status") != "ok")),
            "loop_ran_rate": round(ge._rate([r.get("loop_ran") for r in rows]), 3),
            "accepted_rate": round(ge._rate([r.get("accepted") for r in rows]), 3),
            "loop_guided_rate": round(ge._rate([
                bool((r.get("loop_references") or {}).get("applied")) for r in rows]), 3),
            "fail_reached_rate": round(ge._rate([r.get("fail_reached") for r in rows]), 3),
            "fail_sent": len(sent_fail),
            "fail_reached_of_sent": (
                round(ge._rate([r.get("fail_reached") for r in sent_fail]), 3)
                if sent_fail else None),
            "mean_calls": ge._mean([r.get("calls") for r in rows], 2),
        }
        # refine: "marks" — of the refines, how many the marks pass made, and
        # how many fell back because the window held more OCR lines than the
        # SERVICE's CLASSIFIER_LLM_BBOX_MARKS_MAX.
        attempts = [a for r in rows for a in r.get("attempt_log") or []]
        refines = [a.get("refine_mode") for a in attempts if a.get("refine_mode")]
        if refines:
            out["marks_refined_rate"] = round(ge._rate([m == "marks" for m in refines]), 3)
            out["marks_over_cap"] = sum(
                1 for a in attempts if "over the cap" in str(a.get("marks_fallback") or ""))
        if present:
            out["present"] = {
                "n": len(present),
                "hit_rate": round(ge._rate([r.get("hit") for r in present]), 3),
                "false_accept_rate": round(ge._rate(
                    [r.get("accepted") and not r.get("hit") for r in present]), 3),
                "mean_iou": ge._mean([r.get("iou") for r in present]),
                "landed_on_rates": ge._landed_rates(present),
            }
        if absent:
            out["absent"] = {
                "n": len(absent),
                "correct_rate": round(ge._rate([r.get("correct") for r in absent]), 3),
                "wrong_accept_rate": round(ge._rate([not r.get("correct") for r in absent]), 3),
                "bbox_null_rate": round(ge._rate([r.get("bbox_null") for r in absent]), 3),
                "landed_on_rates": ge._landed_rates(absent),
            }
        return out

    names = sorted({r["criterion"] for r in records})
    return {
        "overall": block(records),
        "per_criterion": {n: block([r for r in records if r["criterion"] == n]) for n in names},
    }


def print_summary(summary: dict, config: str) -> None:
    def pct(value) -> str:
        return "n/a" if value is None else format(value, ".0%")

    print(f"\n=== {config} ===")
    for name, row in [*summary["per_criterion"].items(), ("OVERALL", summary["overall"])]:
        print(f"{name[:60]}: {row['n']} run(s), {row['errors']} error(s), loop ran "
              f"{pct(row['loop_ran_rate'])}, accepted {pct(row['accepted_rate'])}, "
              f"mean calls {row['mean_calls']}")
        if row.get("error_kinds"):
            print("    errors: " + ", ".join(f"{kind} ×{n}"
                                          for kind, n in row["error_kinds"].items()))
        present = row.get("present")
        if present:
            print(f"    present ({present['n']}): hit {pct(present['hit_rate'])}, false accept "
                  f"{pct(present['false_accept_rate'])}")
            for other, rate in present["landed_on_rates"].items():
                print(f"      missed box landed on {other!r}: {pct(rate)}")
        absent = row.get("absent")
        if absent:
            print(f"    absent ({absent['n']}): right (no box) {pct(absent['correct_rate'])}, "
                  f"wrong accept {pct(absent['wrong_accept_rate'])}, said bbox null "
                  f"{pct(absent['bbox_null_rate'])}")
            for other, rate in absent["landed_on_rates"].items():
                print(f"      wrong box landed on {other!r}: {pct(rate)}")
        print(f"    loop shown examples {pct(row['loop_guided_rate'])}; FAIL reached the loop "
              f"{pct(row['fail_reached_rate'])} of runs ({pct(row['fail_reached_of_sent'])} of "
              f"the {row['fail_sent']} sent one)")
        if "marks_refined_rate" in row:
            print(f"    marks refined {pct(row['marks_refined_rate'])} of the refines; "
                  f"{row['marks_over_cap']} fell back over the service's marks cap")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Run:
    """What this run created — so the ``finally`` can delete all of it."""

    references: list[RefSpec] = field(default_factory=list)
    jobs: list[JobSpec] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    deleted_jobs: set[str] = field(default_factory=set)
    summary: Optional[dict] = None
    complete: bool = False


def _check(response, what: str) -> dict:
    """The JSON body of a 2xx response, or exit naming the call and status
    (the detail is the service's own message, never page content)."""
    if response.status_code >= 300:
        try:
            detail = response.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = response.text[:300]
        raise SystemExit(f"{what}: HTTP {response.status_code}: {str(detail)[:400]}")
    return response.json() if response.content else {}


def _validate_criteria(criteria: list[dict], references: bool) -> None:
    """The locate criteria checked against this build's request model before
    anything is created (the service checks again on every submit)."""
    try:
        from pydantic import ValidationError

        from api.schemas import AssessRequest, validation_message
    except Exception:  # noqa: BLE001 — not in the container: the service checks
        return
    body: dict[str, Any] = {"document": {"type": "text", "data": "x"}, "criteria": criteria}
    if references:
        body["references"] = ["r000000000000"]
    try:
        AssessRequest.model_validate(body)
    except ValidationError as exc:
        raise SystemExit(f"--options / --key: the locate criteria are not a valid request: "
                         f"{validation_message(exc)}")


def run(args: argparse.Namespace, client: Any, state: Run) -> dict:
    """Steps 1-3; ``main`` runs ``cleanup`` in its ``finally``. ``client`` is
    an ``httpx.Client`` with the service's base URL (a ``TestClient`` in the
    tests)."""
    images = {p.name: p for p in ge._images(pathlib.Path(args.images).resolve())}
    truth = ge._load_truth(pathlib.Path(args.truth).resolve())
    groups = ge._load_groups(pathlib.Path(args.groups).resolve()) if args.groups else {}
    candidates = {
        page: [*entry["criteria"], *entry["absent"]]
        for page, entry in truth.items() if page in images
    }
    if args.subset:
        subset = ge._load_subset(pathlib.Path(args.subset).resolve())
        candidates = {page: [c for c in labels if c in set(subset.get(page, []))]
                      for page, labels in candidates.items()}
    candidates = {page: labels for page, labels in candidates.items() if labels}
    names = sorted({c for labels in candidates.values() for c in labels})
    if not names:
        raise SystemExit("no candidate page x criterion: check --images, --truth and --subset")
    # --key / --options name criteria among everything the truth asks (as
    # --fail-from-decoys does), so one command line serves a --subset too;
    # only the criteria this run asks are used.
    every = sorted({c for entry in truth.values() for c in [*entry["criteria"], *entry["absent"]]})
    keys = {c: k for c, k in parse_keys(args.key, every).items() if c in names}
    overlays = {c: o for c, o in parse_options(args.options, every).items() if c in names}
    references = not args.no_references
    fail_map = ge._fail_map(args.fail_from_decoys, truth) if args.fail_from_decoys else {}
    if fail_map and not references:
        raise SystemExit("--fail-from-decoys adds FAIL references; it cannot go with "
                         "--no-references")
    for label in names:
        if label not in keys:
            print(f"note    : {label[:60]!r} has no --key; references match it by name")
    if not references and any("reference" in o for o in overlays.values()):
        print("note    : --no-references: each overlay's `reference` is left out")
    _validate_criteria([locate_criterion(n, keys.get(n), overlays.get(n),
                                         references=references) for n in names], references)

    limits = _check(client.get("/document-kinds"), "GET /document-kinds").get("references") or {}
    registry = _check(client.get("/models"), "GET /models")
    model_id = args.model or registry.get("default")
    spec = next((m for m in registry.get("models") or [] if m.get("id") == model_id), None)
    if spec is None:
        raise SystemExit(f"--model {model_id!r} is not in GET /models")
    max_images = int(spec.get("max_images_per_prompt") or 1)
    config = args.label or ("baseline" if not references else "guided")
    print(f"service : {args.base_url}  model {model_id} ({max_images} image(s) per prompt)")
    print(f"config  : {config}; {len(candidates)} page(s), {sum(map(len, candidates.values()))} "
          f"pair(s), {args.repeats} repeat(s), {args.concurrency} job(s) in flight")

    # Step 1 — the references.
    if references:
        state.references = plan_references(truth, set(images), names, fail_map)
        existing = int(_check(client.get("/references", params={"limit": 1}),
                              "GET /references").get("total") or 0)
        cap = int(limits.get("max_count") or 0)
        print(f"refs    : {existing} exist, {len(state.references)} to create, cap {cap} "
              "(CLASSIFIER_REFERENCE_MAX_COUNT)")
        if cap and existing + len(state.references) > cap:
            raise SystemExit(
                f"{existing} references exist and this run needs {len(state.references)}: over "
                f"CLASSIFIER_REFERENCE_MAX_COUNT ({cap}). Delete some (or --purge a killed "
                "run's), or run on a --subset")
        create_references(client, state, images, keys)
        wait_for_references(client, state, args)
    ready = [s for s in state.references if s.status == "ready"]
    failed = [s for s in state.references if s.status != "ready"]
    if references:
        print(f"refs    : {len(ready)} ready, {len(failed)} failed")
        for s in failed:
            print(f"    failed: {s.reference_id or page_id(s.page) + ' ' + s.polarity}"
                  f" ({s.error_kind})")

    # Step 2 — the jobs.
    state.jobs = plan_jobs(
        candidates, args.repeats, truth, groups, ready, fail_map, references=references,
        max_images=max_images,
        max_per_request=int(limits.get("max_per_request") or 10),
    )
    run_jobs(client, state, args, images, truth, keys, overlays, config)

    # Step 3 — the summary (the records were scored as each job finished).
    state.summary = summarise(state.records)
    print_summary(state.summary, config)
    state.complete = True
    return state.summary


def create_references(client: Any, state: Run, images: dict[str, pathlib.Path],
                      keys: dict[str, str]) -> None:
    """``POST /references`` for every planned reference. One the service
    refuses is ``failed`` with its HTTP status only — a 400's detail may
    quote the region, which is a truth box — and the run goes on without
    it."""
    raws: dict[str, bytes] = {}
    for spec in state.references:
        raw = raws.get(spec.page)
        if raw is None:
            raw = raws[spec.page] = images[spec.page].read_bytes()
            if len(raws) > 4:           # keep a few pages in memory, not the set
                raws.pop(next(iter(raws)))
        response = client.post(
            "/references",
            data=reference_form(spec, keys.get(spec.criterion), image_size(raw)),
            files={"file": (spec.page, raw, _content_type(spec.page))},
        )
        if response.status_code != 202:
            spec.status, spec.error_kind = "failed", f"refused: HTTP {response.status_code}"
            continue
        body = response.json()
        spec.reference_id, spec.job_id, spec.status = (
            body["reference_id"], body.get("job_id"), "pending")
    print(f"refs    : {sum(1 for s in state.references if s.reference_id)} submitted")


def wait_for_references(client: Any, state: Run, args: argparse.Namespace) -> None:
    deadline = time.monotonic() + args.timeout
    while True:
        pending = [s for s in state.references if s.status == "pending"]
        for spec in pending:
            body = _check(client.get(f"/references/{spec.reference_id}"),
                          f"GET /references/{spec.reference_id}")
            if body.get("status") in ("ready", "failed"):
                spec.status = body["status"]
                spec.error_kind = error_kind(body.get("error"))
        if not any(s.status == "pending" for s in state.references):
            return
        if time.monotonic() > deadline:
            for spec in state.references:
                if spec.status == "pending":
                    spec.status, spec.error_kind = "failed", "timed out waiting"
            return
        time.sleep(args.poll)


def run_jobs(client: Any, state: Run, args: argparse.Namespace,
             images: dict[str, pathlib.Path], truth: dict[str, ge.Truth],
             keys: dict[str, str], overlays: dict[str, dict], config: str) -> None:
    """At most ``--concurrency`` jobs in flight: submit, poll, and as each
    finishes score it, write ``--out``, then delete the job."""
    queue = list(state.jobs)
    flying: list[JobSpec] = []
    done = 0
    while queue or flying:
        while queue and len(flying) < args.concurrency:
            job = queue.pop(0)
            raw = images[job.page].read_bytes()
            body = _check(
                client.post("/assess", data=job_form(job, keys, overlays, args.model),
                            files={"file": (job.page, raw, _content_type(job.page))}),
                f"POST /assess ({page_id(job.page)} #{job.repeat})",
            )
            job.job_id, job.submitted_at = body["job_id"], time.monotonic()
            flying.append(job)
        time.sleep(args.poll)
        for job in list(flying):
            body = _check(client.get(f"/jobs/{job.job_id}"), f"GET /jobs/{job.job_id}")
            phase = body.get("phase")
            seconds = round(time.monotonic() - (job.submitted_at or 0), 1)
            if phase == "completed":
                records = job_records(job, body.get("result") or {}, truth, keys, config)
            elif phase == "failed":
                records = failed_job_records(job, "failed", keys, config)
            elif seconds > args.timeout:
                records = failed_job_records(job, "timed_out", keys, config)
            else:
                continue
            for record in records:
                record["job_seconds"] = seconds
            state.records.extend(records)
            flying.remove(job)
            done += 1
            write_out(args, state, config)
            _delete_job(client, state, job.job_id)
            print(f"  job {done}/{len(state.jobs)} {job.job_id} {phase}")


def _content_type(name: str) -> str:
    suffix = pathlib.Path(name).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(
        suffix, "application/octet-stream")


def _delete_job(client: Any, state: Run, job_id: Optional[str]) -> None:
    if not job_id or job_id in state.deleted_jobs:
        return
    response = client.delete(f"/jobs/{job_id}")
    if response.status_code in (204, 404):
        state.deleted_jobs.add(job_id)


def write_out(args: argparse.Namespace, state: Run, config: str) -> None:
    """``--out``: written after every finished job (so a job's record is on
    disk before the job is deleted) and once more at the end."""
    if not args.out:
        return
    out = {
        "config": {
            "label": config, "base_url": args.base_url, "model": args.model,
            "repeats": args.repeats, "references": not args.no_references,
            "keys": list(args.key or []), "options": list(args.options or []),
            "fail_from_decoys": list(args.fail_from_decoys or []),
        },
        "complete": state.complete,
        "summary": state.summary,
        "references": [{
            "reference_id": s.reference_id, "page": s.page, "page_id": page_id(s.page),
            "criterion": s.criterion, "polarity": s.polarity, "decoy": s.decoy,
            "status": s.status, "error_kind": s.error_kind,
        } for s in state.references],
        "records": state.records,
    }
    pathlib.Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")


def cleanup(client: Any, state: Run, keep_references: bool = False) -> dict:
    """Delete every job and reference this run made — the ``finally``. Each
    call is guarded on its own: one failure never stops the rest."""
    counts = {"jobs": 0, "reference_jobs": 0, "references": 0, "failed": 0}
    for job in state.jobs:
        if job.job_id and job.job_id not in state.deleted_jobs:
            try:
                _delete_job(client, state, job.job_id)
            except Exception:  # noqa: BLE001 — clean up the rest regardless
                pass
        if job.job_id in state.deleted_jobs:
            counts["jobs"] += 1
        elif job.job_id:
            counts["failed"] += 1
    if not keep_references:
        for spec in state.references:
            if not spec.reference_id:
                continue
            try:
                # Ours, and our jobs are gone: force past a job still running.
                response = client.delete(f"/references/{spec.reference_id}",
                                         params={"force": "true"})
                ok = response.status_code in (204, 404)
            except Exception:  # noqa: BLE001
                ok = False
            counts["references" if ok else "failed"] += 1
    for spec in state.references:
        if spec.job_id:
            try:
                _delete_job(client, state, spec.job_id)
            except Exception:  # noqa: BLE001
                pass
            if spec.job_id in state.deleted_jobs:
                counts["reference_jobs"] += 1
    kept = " (references kept: --keep-references)" if keep_references else ""
    print(f"cleanup : deleted {counts['jobs']} job(s), {counts['references']} reference(s), "
          f"{counts['reference_jobs']} creation job(s); {counts['failed']} failed{kept}")
    return counts


def purge(client: Any) -> int:
    """``--purge``: delete every reference tagged ``zt527-e2e`` (a run that
    was killed outright never reached its ``finally``)."""
    ids: list[str] = []
    offset = 0
    while True:
        body = _check(client.get("/references", params={"tag": TAG, "limit": 200,
                                                         "offset": offset}), "GET /references")
        rows = body.get("references") or []
        ids += [r["reference_id"] for r in rows]
        offset += len(rows)
        if not rows or offset >= int(body.get("total") or 0):
            break
    deleted = sum(
        1 for rid in ids
        if client.delete(f"/references/{rid}", params={"force": "true"}).status_code in (204, 404)
    )
    print(f"purge   : {deleted} of {len(ids)} reference(s) tagged {TAG!r} deleted")
    return deleted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the box loop end to end through the classifier's API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"the classifier (default {DEFAULT_BASE_URL}, its own uvicorn)")
    parser.add_argument("--images", help="directory of the pages, searched recursively")
    parser.add_argument("--truth", help="the experiment's truth file (criteria, decoys, absent)")
    parser.add_argument("--groups", default=None,
                        help='{"file.jpg": group}: a page is never the reference of a page '
                             "of its own group (grounding_experiment.py --groups-from-selection)")
    parser.add_argument("--subset", default=None,
                        help='{"file.jpg": ["criterion", ...]}: only these candidate pairs')
    parser.add_argument("--repeats", type=int, default=5,
                        help="jobs per candidate page (default 5, as the experiment); the "
                             "references rotate")
    parser.add_argument("--key", action="append", default=[],
                        metavar="CRITERION_SUBSTRING=FACET_KEY",
                        help="the criterion's key, on the locate criterion and its references "
                             "(repeatable)")
    parser.add_argument("--options", action="append", default=[],
                        metavar="CRITERION_SUBSTRING=JSON",
                        help="an overlay merged into that criterion's options on its locate "
                             "job, e.g. '...={\"refine\":\"marks\"}' (repeatable)")
    parser.add_argument("--fail-from-decoys", action="append", default=None,
                        metavar="DECOY=CRITERION_SUBSTRING",
                        help="FAIL references from that decoy's boxes, one per job beside the "
                             "PASS (repeatable, one decoy per criterion)")
    parser.add_argument("--no-references", action="store_true",
                        help="the same jobs without references: the baseline")
    parser.add_argument("--label", default=None,
                        help="the config label on every record (default: baseline / guided)")
    parser.add_argument("--model", default=None,
                        help="the job's registry model (default: the service's default)")
    parser.add_argument("--concurrency", type=int, default=4, help="jobs in flight (default 4)")
    parser.add_argument("--poll", type=float, default=2.0, help="poll interval, seconds")
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="seconds one job (or the references) may take (default 1800)")
    parser.add_argument("--out", default=None, help="write the records and summary here")
    parser.add_argument("--keep-references", action="store_true",
                        help="debugging only: do not delete the references at the end")
    parser.add_argument("--purge", action="store_true",
                        help=f"delete every reference tagged {TAG!r}, and exit")
    return parser


def main(argv: Optional[list[str]] = None, client: Any = None) -> Optional[dict]:
    """Parse, run, and always clean up. ``client`` is for the tests (a
    ``TestClient``); on the box it is an ``httpx.Client`` on ``--base-url``."""
    parser = build_parser()
    args = parser.parse_args(argv)
    owned = client is None
    if owned:
        import httpx

        client = httpx.Client(base_url=args.base_url, timeout=300.0)
    try:
        if args.purge:
            purge(client)
            return None
        if not args.images or not args.truth:
            parser.error("--images and --truth are required")
        if args.repeats < 1 or args.concurrency < 1:
            parser.error("--repeats and --concurrency are at least 1")
        state = Run()
        try:
            return run(args, client, state)
        finally:
            config = args.label or ("baseline" if args.no_references else "guided")
            write_out(args, state, config)
            cleanup(client, state, keep_references=args.keep_references)
    finally:
        if owned:
            client.close()


if __name__ == "__main__":
    main()
