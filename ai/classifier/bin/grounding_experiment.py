"""Measure how well the vision model actually grounds — before you trust it.

Step 0 of phase 3 of the regions plan (kept outside the repo, in the operator's Claude project folder), kept as a script because the question
it answers is empirical and deployment-specific: *does asking Muse Glimmer for
a bounding box produce a usable one often enough to build on?* The enforcement
loop (`llm/boxes.py`) is designed for the answer being "not always", but the
numbers decide how to configure it — and whether the open-vocabulary detector
should be the primary source with the LLM as the fallback rather than the
other way round.

It also measures what REFERENCE EXAMPLES do to the loop (ZT-527, the
`CLASSIFIER_REFERENCE_BOX_ASK` / `_REFINE` / `_VERIFY` knobs): the same
images and criteria run once per CONFIGURATION — which of the ask, refine and
verify calls are shown the examples — repeated, and with a TRUTH file every
accepted box is scored against where the feature really is. The knobs'
defaults come from these numbers; nothing is defaulted on before it is
measured.

**This has to run where the model is.** It talks to a registry model's URL
directly (`models.yaml`; the default is the `muse-glimmer` container on
`ai_shared`, `--model <id>` picks another vision entry), so it runs ON the
box — in the classifier container, or on the host with the port published. It
cannot be run from a laptop with no GPU behind it.

Usage::

    # Inside the classifier container, against the bundled fixtures
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py

    # A directory of your own photos plus the criteria to look for
    uv run --no-sync python bin/grounding_experiment.py \\
        --images /data/roof-photos \\
        --criteria /data/roof-criteria.json \\
        --attempts 3 --out /data/grounding.json

    # The same against another registry model (GET /models)
    uv run --no-sync python bin/grounding_experiment.py --model qwen2.5-vl

    # With and without reference examples, scored against the truth file.
    # The container has no unit-tests/ folder, so copy the two bills and the
    # truth file into its data volume, then run once per bill as the
    # reference (the reference image is never its own candidate):
    docker exec classifier mkdir -p /data/grounding
    docker cp unit-tests/classifier/documents/utility_bill.jpeg   classifier:/data/grounding/
    docker cp unit-tests/classifier/documents/utility_bill_2.jpeg classifier:/data/grounding/
    docker cp unit-tests/classifier/documents/grounding_truth.json classifier:/data/grounding/
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py --images /data/grounding \\
        --truth /data/grounding/grounding_truth.json \\
        --reference-image /data/grounding/utility_bill_2.jpeg --out /data/grounding/ref2.json
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py --images /data/grounding \\
        --truth /data/grounding/grounding_truth.json \\
        --reference-image /data/grounding/utility_bill.jpeg --out /data/grounding/ref1.json

    # Stored references (CLASSIFIER_REFERENCE_DIR) as the examples instead
    uv run --no-sync python bin/grounding_experiment.py --images /data/grounding \\
        --truth /data/grounding/grounding_truth.json --references r0123456789ab

``--criteria`` is a JSON object mapping file name to the criteria to ask
about, e.g.::

    {
      "house-01.jpg": ["solar panels", "a satellite dish"],
      "house-02.jpg": ["solar panels"]
    }

A bare list instead of an object applies those criteria to every image.

``--truth`` says where things really are, per file name: ``criteria`` —
the criteria to ask, each with EVERY real copy of it on the page — and
``decoys`` — things never asked, only used to name where a missed box landed.
Each box is ``[x1, y1, x2, y2]`` in ORIGINAL upright page pixels (the frame
regions are stored in); a bare box stands for a list of one. With it, the
criteria asked are the truth file's ``criteria``, and ``--criteria`` is
ignored. ``unit-tests/classifier/documents/grounding_truth.json`` holds the
two photographed bills' logo, amount due and account number — every copy:
the header, the payment slip, bill 2's right-hand page — and one decoy, the
lines of text that name the utility (what a model boxes when it mistakes the
company name for the logo)::

    {"utility_bill.jpeg": {
        "criteria": {"the utility's logo": [[x1, y1, x2, y2], ...], ...},
        "decoys": {"text naming the utility": [[x1, y1, x2, y2], ...]}}, ...}

The examples: ``--reference-image <file>`` builds one PASS example per
criterion from that image's FIRST truth box for it — the composite exactly as a stored
reference renders it (``references.render.composite``, the box drawn with the
caller's ``manual`` stroke), the crop with ``llm.boxes.example_crop`` — and
never uses a page as its own example. ``--references r…`` reads stored
references from ``CLASSIFIER_REFERENCE_DIR`` and picks each criterion's
examples (matched by name, case-insensitive) the way a job does
(``analysis.references.JobReferences.box_examples``), skipping a candidate
whose file name a stored reference's page was saved from. The two fixture bills
are from different utilities (AEP Ohio, Ohio Edison), so bill-to-bill tests
generalisation, not the one-template case.

``--configs`` (default ``off,ask,refine,verify,all``) names which calls see
the examples: ``off`` none, ``ask`` / ``refine`` / ``verify`` that call
alone, ``all`` every one. Without examples only ``off`` runs. ``--repeats``
(default 5) runs each image x criterion x configuration that many times —
Muse samples at temperature 1.0, so one run of each is an anecdote.

``--verify-images`` (default ``crop``) is what the verify call is shown when
it carries examples (``verify`` and ``all``): ``crop``, the examples' region
crops, as the service does; ``composite``, the same composites the ask is
shown (the reference page with the feature outlined, the composite captions)
— ``llm.boxes.locate_criterion(verify_examples=...)``, which the service never
passes. Refine always sees the crops.

What it prints, per configuration, per criterion and overall:

    attempt-1 valid     how often the FIRST box survived validation. This is
                        the headline number without a truth file: a low one
                        means most of the model's answers are full-frame
                        non-answers, and every localisation costs at least
                        two round trips.
    verify pass         how often a valid box's crop actually showed the
                        feature. A high attempt-1-valid with a low verify pass
                        means the model draws tidy boxes in the wrong place —
                        the worst case, because it looks right until you open
                        the crop.
    accepted            how often the loop ended with a box at all (the
                        accepted-attempt rate).
    hit                 (--truth) how often the accepted box's CENTRE lies
                        inside ANY copy of the criterion — a correct box on
                        the payment slip's copy is correct; IoU alone is too
                        harsh on a thin text line. Over every run, accepted
                        or not.
    false accept        (--truth) accepted and NOT a hit: the loop vouched for
                        the wrong place. The number the verify knob is judged
                        by — a model shown two crops can score resemblance —
                        so CLASSIFIER_REFERENCE_BOX_VERIFY goes on only if
                        this does not rise.
    bbox null           how often the model answered "not visible here" (the
                        loop stops on it).
    IoU / centre error  (--truth) of the accepted box against the best-
                        matching copy (highest IoU, then closest centre;
                        ``truth_copy`` in the JSON is its index); the centre
                        error in grid units (0-1000 per axis).
    landed on           (--truth) for an accepted box that is not a hit, the
                        OTHER criterion or decoy whose box holds its centre
                        (criteria first) — the identification case: the logo
                        asked for, ``text naming the utility`` boxed.
    mean attempts       how many rounds an accepted criterion needed.
    detector IoU        with DETECTOR_URL set, mean overlap between the
                        accepted box and the detector's best box for the same
                        label. Under ~0.5 the two sources disagree about what
                        the criterion means, which is a prompt problem, not a
                        geometry one.
    per call kind       calls, mean prompt tokens (each response's ``usage``;
                        "n/a" when the server reports none) and mean seconds
                        of the ask / refine / verify calls — what the examples
                        cost: each guided call carries 1 + examples images.

Rule of thumb from the plan: if attempt-1 validity is under ~50%, make the
detector the primary source and the LLM loop the fallback.

``--out`` writes ``{"summary", "records"}``; ``summary.config`` carries
``verify_images``. One record per run (image x criterion x configuration x
repeat), with the numbers above per run, and — to say WHY, not just how often:

    verify_images   what a guided verify was shown on this run.
    attempt_log     ``[a.as_dict() for a in loc.attempts]`` — each attempt's
                    box (``bbox_grid`` / ``bbox_px``, coarse and refined),
                    ``verify_score``, ``verify_reason`` and ``reject`` (the
                    sentence the retry is told), as ``localization.attempts``
                    has them.
    answers         every model request of the run in call order, as the
                    model answered it: ``{kind: ask | refine | verify,
                    attempt, request, bbox (ask / refine) | score (verify),
                    reason, ok}``. ``attempt`` is the loop attempt (from the
                    call label, ``bbox/<criterion>#<n>``), ``request`` the
                    request within it (> 1 is a parse retry). ``score`` is
                    the model's own number, before the loop clamps it. When
                    the answer would not parse (or the request failed) ``ok``
                    is false and ``raw`` holds its content (or the error), cut
                    to 400 characters. The prompt is never stored — it holds
                    the images.
    call_log        per request: kind, seconds, prompt tokens, ok (HTTP).

Process flow position: none — a standalone operator tool. It imports the same
`llm.boxes` loop the pipeline runs, so what it measures is what production
does, not a reimplementation of it. Usage rows are captured in-process (it has
no job and no database): ``llm.usage.record_call`` is replaced for the run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import pathlib
import re
import statistics
import sys
import time

# The classifier's modules import each other by bare name (the container's
# WORKDIR). Running this from `bin/` needs the parent on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common.vision import PageGeometry, Region  # noqa: E402

from detector import client as detector_client  # noqa: E402
from llm import boxes as llm_boxes  # noqa: E402
from llm import client as llm_client  # noqa: E402
from llm import usage as llm_usage  # noqa: E402
from config import (  # noqa: E402
    GROUNDING_DEFAULT_CRITERIA,
    GROUNDING_IMAGE_SUFFIXES,
    LLM_BBOX_GRID,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_MAX_AREA,
    LLM_BBOX_MIN_AREA,
    LLM_BBOX_REFINE,
    LLM_BBOX_VERIFY_PASS,
    MAX_WORKING_DIMENSION,
)
from llm.client import encode_image_to_base64  # noqa: E402
from llm.models import ModelSpec, get_registry  # noqa: E402
from llm.prompts import BoxExample  # noqa: E402

# Which of the loop's calls see the examples: (ask, refine, verify).
CONFIGS: dict[str, tuple[bool, bool, bool]] = {
    "off": (False, False, False),
    "ask": (True, False, False),
    "refine": (False, True, False),
    "verify": (False, False, True),
    "all": (True, True, True),
}
CALL_KINDS: tuple[str, ...] = ("ask", "refine", "verify")


def _model(model_id: str | None) -> ModelSpec:
    """The registry entry ``--model`` names (default: the registry's), or exit."""
    registry = get_registry()
    try:
        spec = registry.check_selectable(model_id) if model_id else registry.default
    except ValueError as exc:
        raise SystemExit(f"--model: {exc}")
    if not spec.vision:
        raise SystemExit(f"--model: {spec.id!r} is text-only; grounding needs a vision model")
    return spec

# The image suffixes scanned for and the per-fixture default criteria live in
# config.py § Grounding experiment, next to every other classifier constant.


def _repo_fixture_dir() -> pathlib.Path:
    """`unit-tests/classifier/` if this is a checkout, else the cwd."""
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "unit-tests" / "classifier"
        if candidate.is_dir():
            return candidate
    return pathlib.Path.cwd()


def _load_criteria(path: pathlib.Path | None, names: list[str]) -> dict[str, list[str]]:
    """The file → criteria map, from ``--criteria`` or the built-in defaults."""
    if path is None:
        return {name: GROUNDING_DEFAULT_CRITERIA.get(name, []) for name in names}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {name: [str(c) for c in data] for name in names}
    if not isinstance(data, dict):
        raise SystemExit(
            f"{path}: expected a JSON object {{filename: [criterion, ...]}} or a "
            "flat list of criteria to apply to every image"
        )
    return {name: [str(c) for c in data.get(name, [])] for name in names}


# One image's truth: {"criteria": {name: [box, ...]}, "decoys": {name: [box, ...]}}.
Truth = dict[str, dict[str, list[list[float]]]]


def _is_box(value) -> bool:
    return (
        isinstance(value, list) and len(value) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


def _boxes(path: pathlib.Path, where: str, value) -> list[list[float]]:
    """A truth entry as a list of boxes — ``[box, ...]``, or one bare box."""
    boxes = [value] if _is_box(value) else value
    if not isinstance(boxes, list) or not boxes or not all(_is_box(b) for b in boxes):
        raise SystemExit(
            f"{path}: {where} must be [x1, y1, x2, y2] or a non-empty list of them, "
            f"got {value!r}"
        )
    for box in boxes:
        if box[0] >= box[2] or box[1] >= box[3]:
            raise SystemExit(f"{path}: {where}: {box!r} needs x1 < x2 and y1 < y2")
    return [[float(v) for v in box] for box in boxes]


def _load_truth(path: pathlib.Path) -> dict[str, Truth]:
    """``--truth``: file name → ``{"criteria": {name: [box, ...]}, "decoys":
    {name: [box, ...]}}``, every box ``[x1, y1, x2, y2]`` in original upright
    pixels (a bare box for a list of one). Every real copy of a criterion is
    one of its boxes; decoys are never asked — they only name where a missed
    box landed."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(
            f"{path}: expected {{filename: {{\"criteria\": {{name: [box, ...]}}, "
            "\"decoys\": {name: [box, ...]}}}}"
        )
    truth: dict[str, Truth] = {}
    for image, entry in data.items():
        if (not isinstance(entry, dict) or not isinstance(entry.get("criteria"), dict)
                or not isinstance(entry.get("decoys", {}), dict)
                or set(entry) - {"criteria", "decoys"}):
            raise SystemExit(
                f"{path}: {image!r} must be {{\"criteria\": {{...}}, \"decoys\": {{...}}}} "
                "(decoys optional)"
            )
        truth[image] = {
            part: {name: _boxes(path, f"{image!r} / {part} / {name!r}", value)
                   for name, value in (entry.get(part) or {}).items()}
            for part in ("criteria", "decoys")
        }
        both = set(truth[image]["criteria"]) & set(truth[image]["decoys"])
        if both:
            raise SystemExit(f"{path}: {image!r}: {sorted(both)} is both a criterion and a decoy")
    return truth


def _images(directory: pathlib.Path) -> list[pathlib.Path]:
    found = sorted(
        p for p in directory.rglob("*") if p.suffix.lower() in GROUNDING_IMAGE_SUFFIXES
    )
    if not found:
        raise SystemExit(f"{directory}: no .jpg/.jpeg/.png files found")
    return found


def _load(path: pathlib.Path):
    """``(original_bgr, working_bgr, PageGeometry)`` for one image file.

    The same preparation the pipeline does: the model sees a ≤1000-px working
    copy, the verify crop comes out of the original, and the geometry is what
    turns the 0-1000 grid into original pixels.
    """
    import cv2

    original = cv2.imread(str(path))
    if original is None:
        return None, None, None
    height, width = original.shape[:2]
    working = original
    if max(height, width) > MAX_WORKING_DIMENSION:
        scale = MAX_WORKING_DIMENSION / max(height, width)
        working = cv2.resize(
            original, (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_AREA,
        )
    geometry = PageGeometry(
        page=0, width=width, height=height,
        working_scale=working.shape[1] / width,
    )
    return original, working, geometry


async def _detector_boxes(working, labels: list[str], geometry: PageGeometry):
    """The detector's boxes for the same labels, or ``{}`` when it is off."""
    if not detector_client.is_configured() or not labels:
        return {}
    try:
        return await detector_client.detect_page(working, labels, geometry)
    except detector_client.DetectorUnavailable as exc:
        print(f"    detector unavailable: {exc}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# The examples
# ---------------------------------------------------------------------------


def _image_examples(
    path: pathlib.Path, criteria: dict[str, list[list[float]]]
) -> dict[str, tuple[list[BoxExample], list[BoxExample]]]:
    """``--reference-image``: per criterion, ``(ask, crops)`` — one PASS
    example built from the image's FIRST truth box for it (list the primary
    copy first; decoys are never examples), as production would show it: the
    composite drawn by ``references.render.composite`` (the caller's
    ``manual`` stroke), the crop cut by ``llm.boxes.example_crop``."""
    from references.render import composite

    original, working, geometry = _load(path)
    if original is None:
        raise SystemExit(f"--reference-image: {path} is unreadable")
    out: dict[str, tuple[list[BoxExample], list[BoxExample]]] = {}
    for criterion, copies in criteria.items():
        x1, y1, x2, y2 = copies[0]
        region = Region(page=0, kind="box", points=[(x1, y1), (x2, y2)], label=criterion,
                        source="manual")
        drawn = base64.b64encode(composite(working, geometry, [region])).decode("ascii")
        crop = encode_image_to_base64(llm_boxes.example_crop(original, [x1, y1, x2, y2]))
        out[criterion] = (
            [BoxExample(criterion=criterion, verdict="PASS", image_b64=drawn)],
            [BoxExample(criterion=criterion, verdict="PASS", image_b64=crop, crop=True)],
        )
    return out


def _stored_examples(reference_ids: list[str]) -> tuple[dict[str, list[dict]], set[str]]:
    """``--references``: every usable example in those stored references, by
    folded criterion name, in the shape a job's plan carries
    (``references.resolve._example``) — read from ``record.json`` under
    CLASSIFIER_REFERENCE_DIR, no database — and the file names their pages
    were saved from, so no page is measured against itself."""
    from references.model import RECORD_FILE, Reference, is_reference_id
    from references.resolve import _example
    from references.store import reference_files

    by_name: dict[str, list[dict]] = {}
    pages: set[str] = set()
    for rid in reference_ids:
        if not is_reference_id(rid):
            raise SystemExit(f"--references: {rid!r} is not a reference id")
        record = reference_files.read_json(rid, RECORD_FILE)
        if record is None:
            raise SystemExit(f"--references: {rid} has no {RECORD_FILE} under the reference dir")
        if (record.get("page") or {}).get("filename"):
            pages.add(record["page"]["filename"])
        ref = Reference(id=rid, status="ready", source_kind="document",
                        created_at="", updated_at="", record=record)
        for name, entry in (record.get("criteria") or {}).items():
            if entry.get("usable"):
                by_name.setdefault(name.casefold().strip(), []).append(_example(ref, name, entry))
    return by_name, pages


async def _stored_guide(
    label: str, by_name: dict[str, list[dict]], spec: ModelSpec
) -> tuple[list[BoxExample], list[BoxExample]]:
    """One criterion's ``(ask, crops)`` from stored references, chosen and
    loaded exactly as a job's ``box_examples`` does (no position check)."""
    from analysis.references import CriterionGuide, JobReferences

    examples = by_name.get(label.casefold().strip(), [])
    guide = CriterionGuide(label, "explicit", {}, examples)
    box_guide = await JobReferences(plan={}).box_examples(guide, spec, position_check=False)
    if box_guide.note:
        print(f"    {label}: {box_guide.note}")
    return box_guide.ask, box_guide.crops


# ---------------------------------------------------------------------------
# Scoring one run against the truth
# ---------------------------------------------------------------------------


def _centre(box: list[float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _inside(point: tuple[float, float], box: list[float]) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _against_truth(
    accepted: list[float] | None,
    label: str,
    truth: Truth,
    geometry: PageGeometry,
) -> dict:
    """``{hit, truth_copy, iou, center_error_grid, landed_on}`` for one run;
    every key None when this image has no truth box for ``label``. An
    unaccepted run is not a hit and has no IoU.

    A HIT is the accepted box's centre inside ANY copy of the criterion (a
    bill repeats its logo, amount and account number on the payment slip, and
    a correct box on any of them is correct). IoU and the centre error (grid
    units) are against the best-matching copy — the highest IoU, the closest
    centre on a tie — and ``truth_copy`` is its index. ``landed_on`` names,
    for a miss, the OTHER criterion or decoy with a box holding the centre
    (criteria first, then decoys; None when it landed on nothing listed)."""
    copies = truth["criteria"].get(label)
    if not copies:
        return {"hit": None, "truth_copy": None, "iou": None, "center_error_grid": None,
                "landed_on": None}
    if accepted is None:
        return {"hit": False, "truth_copy": None, "iou": None, "center_error_grid": None,
                "landed_on": None}
    centre = _centre(accepted)

    def error(box: list[float]) -> float:
        tx, ty = _centre(box)
        return math.hypot(
            (centre[0] - tx) / geometry.width * LLM_BBOX_GRID,
            (centre[1] - ty) / geometry.height * LLM_BBOX_GRID,
        )

    best = max(range(len(copies)), key=lambda i: (_iou(accepted, copies[i]), -error(copies[i])))
    hit = any(_inside(centre, box) for box in copies)
    landed_on = None
    if not hit:
        others = [(name, boxes) for name, boxes in truth["criteria"].items() if name != label]
        others += list(truth["decoys"].items())
        landed_on = next(
            (name for name, boxes in others if any(_inside(centre, b) for b in boxes)), None,
        )
    return {"hit": hit, "truth_copy": best, "iou": round(_iou(accepted, copies[best]), 4),
            "center_error_grid": round(error(copies[best]), 1), "landed_on": landed_on}


# The loop's call labels: "bbox/<criterion>#<attempt>", "refine/…", "verify/…".
_LABEL = re.compile(r"^(?:bbox|refine|verify)/.*#(\d+)$")


class _CallLog:
    """Every model request the loop makes, as ``llm.client`` reports it to
    ``llm.usage.record_call``: its kind, seconds and prompt tokens (``rows``),
    and what the model answered (``answers``). Installed in place of the usage
    writer for the run — the script has no job and no database pool, and the
    per-call cost is one of the numbers it reports. The prompt is never kept:
    it holds the images."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.answers: list[dict] = []

    async def record(self, **fields) -> None:
        counts = fields.get("counts") or {}
        self.rows.append({
            "kind": fields.get("kind"),
            "seconds": round(float(fields.get("seconds") or 0.0), 3),
            "prompt_tokens": counts.get("prompt"),
            "ok": fields.get("error") is None,
        })
        self.answers.append(_answer(fields))


def _answer(fields: dict) -> dict:
    """One request's answer: parsed the way ``llm.client`` parses it
    (``_parse_content``), else its raw content — or the error — cut to 400
    characters. Never raises: a record must not cost the run."""
    kind = fields.get("kind")
    match = _LABEL.match(fields.get("label") or "")
    out: dict = {
        "kind": kind,
        "attempt": int(match.group(1)) if match else None,
        "request": fields.get("attempt"),
    }
    data, error = fields.get("data"), fields.get("error")
    try:
        parsed = llm_client._parse_content(data)
    except Exception:  # noqa: BLE001 — the raw text is the evidence
        parsed = None
    if parsed is None:
        try:
            raw = data["choices"][0]["message"].get("content")
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None:
            raw = str(error) if error is not None else json.dumps(data)[:400]
        return {**out, "reason": None, "ok": False, "raw": str(raw)[:400]}
    if kind == "verify":
        out["score"] = parsed.get("score")
    else:
        out["bbox"] = parsed.get("bbox")
    reason = parsed.get("reason")
    return {**out, "reason": None if reason is None else str(reason)[:400], "ok": True}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run(args) -> dict:
    directory = pathlib.Path(args.images).resolve()
    paths = _images(directory)
    truth = _load_truth(pathlib.Path(args.truth).resolve()) if args.truth else {}
    if truth:
        if args.criteria:
            print("--truth given: the criteria asked are the truth file's; --criteria is ignored")
        # Only the criteria are asked; decoys only name where a miss landed.
        criteria_map = {
            p.name: list((truth.get(p.name) or {}).get("criteria", {})) for p in paths
        }
    else:
        criteria_map = _load_criteria(
            pathlib.Path(args.criteria).resolve() if args.criteria else None,
            [p.name for p in paths],
        )

    spec = _model(args.model)
    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown or not configs:
        raise SystemExit(f"--configs: unknown {unknown}; choose from {', '.join(CONFIGS)}")

    reference_path = (
        pathlib.Path(args.reference_image).resolve() if args.reference_image else None
    )
    image_examples: dict[str, tuple[list[BoxExample], list[BoxExample]]] = {}
    stored: dict[str, list[dict]] = {}
    stored_pages: set[str] = set()
    if reference_path is not None and args.references:
        raise SystemExit("give --reference-image or --references, not both")
    if reference_path is not None:
        if not (truth.get(reference_path.name) or {}).get("criteria"):
            raise SystemExit(
                f"--reference-image: {reference_path.name} has no truth boxes in --truth — "
                "they are what the examples are drawn from"
            )
        if spec.max_images_per_prompt < 2:
            raise SystemExit(f"--model {spec.id} takes 1 image per prompt: no room for an example")
        image_examples = _image_examples(reference_path,
                                         truth[reference_path.name]["criteria"])
    elif args.references:
        stored, stored_pages = _stored_examples(args.references)
    examples_given = bool(image_examples or stored)
    if not examples_given and configs != ["off"]:
        print("no --reference-image / --references: only the 'off' configuration is measured")
        configs = ["off"]

    print(f"model   : {spec.id} ({spec.served_name}) at {spec.url}")
    print(f"detector: {'on — ' + str(detector_client.status()['url_host']) if detector_client.is_configured() else 'off'}")
    print(f"loop    : {args.attempts} attempt(s), verify >= {LLM_BBOX_VERIFY_PASS}, "
          f"area {LLM_BBOX_MIN_AREA:.3%}-{LLM_BBOX_MAX_AREA:.0%}, refine {LLM_BBOX_REFINE}")
    print(f"examples: {reference_path.name if reference_path else (', '.join(args.references) if args.references else 'none')}")
    print(f"configs : {', '.join(configs)}, {args.repeats} repeat(s) each")
    print(f"verify  : shown {args.verify_images}s when it carries examples")
    print(f"images  : {len(paths)} under {directory}\n")

    log = _CallLog()
    llm_usage.record_call = log.record
    records: list[dict] = []
    started = time.monotonic()

    for path in paths:
        if reference_path is not None and path.resolve() == reference_path:
            print(f"{path.name}: the reference image — never its own candidate, skipped")
            continue
        if path.name in stored_pages:
            print(f"{path.name}: a --references page was saved from it — never its own "
                  "candidate, skipped")
            continue
        labels = criteria_map.get(path.name) or []
        if not labels:
            print(f"{path.name}: no criteria configured — skipped")
            continue
        original, working, geometry = _load(path)
        if original is None:
            print(f"{path.name}: unreadable — skipped", file=sys.stderr)
            continue

        image_b64 = encode_image_to_base64(working)
        detector = await _detector_boxes(working, labels, geometry)
        boxes: Truth = truth.get(path.name) or {"criteria": {}, "decoys": {}}
        print(f"{path.name} ({geometry.width}x{geometry.height})")

        for label in labels:
            if image_examples:
                ask_examples, crop_examples = image_examples.get(label, ([], []))
            elif stored:
                ask_examples, crop_examples = await _stored_guide(label, stored, spec)
            else:
                ask_examples, crop_examples = [], []
            for repeat in range(1, args.repeats + 1):
                for config in configs:
                    guide_ask, guide_refine, guide_verify = CONFIGS[config]
                    first_row = len(log.rows)
                    first_answer = len(log.answers)
                    t0 = time.monotonic()
                    regions, loc = await llm_boxes.locate_criterion(
                        label,
                        image_b64=image_b64,
                        original_image=original,
                        geometry=geometry,
                        detector_regions=detector.get(label),
                        max_attempts=args.attempts,
                        working_image=working,
                        model=spec,
                        ask_examples=ask_examples,
                        crop_examples=crop_examples,
                        # composite: the ask's composites; crop: None, so
                        # the loop uses crop_examples as the service does.
                        verify_examples=(
                            ask_examples if args.verify_images == "composite" else None
                        ),
                        guide_ask=guide_ask,
                        guide_refine=guide_refine,
                        guide_verify=guide_verify,
                    )
                    elapsed = time.monotonic() - t0
                    first = loc.attempts[0] if loc.attempts else None
                    accepted = next((a for a in loc.attempts if a.accepted), None)
                    record = {
                        "image": path.name,
                        "criterion": label,
                        "config": config,
                        "repeat": repeat,
                        "attempt_1_valid": bool(first and first.valid),
                        "attempt_1_reject": first.reject if first else "no attempt made",
                        "any_valid": any(a.valid for a in loc.attempts),
                        "verify_scores": [a.verify_score for a in loc.attempts if a.valid],
                        "accepted": accepted is not None,
                        "accepted_attempt": loc.accepted_attempt,
                        # The loop's own sentence for "the model answered bbox: null".
                        "bbox_null": any(
                            (a.reject or "").startswith("you answered bbox: null")
                            for a in loc.attempts
                        ),
                        "attempts": len(loc.attempts),
                        "calls": loc.calls,
                        "guided": dict(loc.guided),
                        "verify_images": args.verify_images,
                        "detector_iou": accepted.detector_iou if accepted else None,
                        "detector_boxes": len(detector.get(label) or []),
                        "seconds": round(elapsed, 2),
                        "bbox_px": accepted.bbox_px if accepted else None,
                        "truth_px": boxes["criteria"].get(label),
                        **_against_truth(accepted.bbox_px if accepted else None, label,
                                         boxes, geometry),
                        "attempt_log": [a.as_dict() for a in loc.attempts],
                        "answers": log.answers[first_answer:],
                        "call_log": log.rows[first_row:],
                    }
                    records.append(record)
                    mark = "OK " if record["accepted"] else "-- "
                    detail = (
                        f"attempt {loc.accepted_attempt}, verify {accepted.verify_score}"
                        if accepted
                        else (first.reject if first else "no answer")
                    )
                    hit = "" if record["hit"] is None else ("  HIT" if record["hit"] else "  miss")
                    iou = (
                        f", iou {record['detector_iou']:.2f}"
                        if record["detector_iou"] is not None
                        else ""
                    )
                    print(f"  {mark} {config:<7}#{repeat} {label[:30]:<31} {detail}{iou}{hit}"
                          f"  ({elapsed:.1f}s)")
        print()

    summary = _summarise(records, time.monotonic() - started, spec.id, configs, bool(truth),
                         args.verify_images)
    _print_summary(summary)
    if args.out:
        out = pathlib.Path(args.out)
        out.write_text(
            json.dumps({"summary": summary, "records": records}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {out}")
    return summary


def _rate(values: list[bool]) -> float:
    return (sum(1 for v in values if v) / len(values)) if values else 0.0


def _mean(values: list, digits: int = 3):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), digits) if values else None


def _summarise(
    records: list[dict], elapsed: float, model_id: str, configs: list[str], scored: bool,
    verify_images: str,
) -> dict:
    """Per configuration: the rates overall and per criterion, and what each
    call kind cost."""

    def block(rows: list[dict]) -> dict:
        valid_first = [r["attempt_1_valid"] for r in rows]
        verified = [s for r in rows for s in r["verify_scores"] if s is not None]
        accepted = [r for r in rows if r["accepted"]]
        ious = [r["detector_iou"] for r in accepted if r["detector_iou"] is not None]
        out = {
            "n": len(rows),
            "attempt_1_valid_rate": round(_rate(valid_first), 3),
            "verify_pass_rate": round(
                _rate([s >= LLM_BBOX_VERIFY_PASS for s in verified]), 3
            ),
            "accepted_rate": round(_rate([r["accepted"] for r in rows]), 3),
            "bbox_null_rate": round(_rate([r["bbox_null"] for r in rows]), 3),
            "mean_attempts_to_accept": (
                round(statistics.fmean(r["accepted_attempt"] for r in accepted), 2)
                if accepted else None
            ),
            "mean_calls": round(statistics.fmean(r["calls"] for r in rows), 2) if rows else None,
            "mean_detector_iou": round(statistics.fmean(ious), 3) if ious else None,
            "iou_at_least_0_5_rate": round(_rate([i >= 0.5 for i in ious]), 3) if ious else None,
        }
        truthed = [r for r in rows if r["hit"] is not None]
        if scored and truthed:
            landed: dict[str, int] = {}
            for r in truthed:
                if r["landed_on"]:
                    landed[r["landed_on"]] = landed.get(r["landed_on"], 0) + 1
            out.update({
                "hit_rate": round(_rate([r["hit"] for r in truthed]), 3),
                "false_accept_rate": round(
                    _rate([r["accepted"] and not r["hit"] for r in truthed]), 3
                ),
                "mean_truth_iou": _mean([r["iou"] for r in truthed]),
                "mean_center_error_grid": _mean([r["center_error_grid"] for r in truthed], 1),
                "landed_on_rates": {
                    other: round(count / len(truthed), 3) for other, count in sorted(landed.items())
                },
            })
        return out

    def calls(rows: list[dict]) -> dict:
        logged = [c for r in rows for c in r["call_log"]]
        per_kind = {}
        for kind in CALL_KINDS:
            mine = [c for c in logged if c["kind"] == kind]
            per_kind[kind] = {
                "calls": len(mine),
                "failed": sum(1 for c in mine if not c["ok"]),
                "mean_prompt_tokens": _mean([c["prompt_tokens"] for c in mine], 1),
                "mean_seconds": _mean([c["seconds"] for c in mine], 2),
            }
        return per_kind

    names = sorted({r["criterion"] for r in records})
    by_config = {}
    for config in configs:
        rows = [r for r in records if r["config"] == config]
        by_config[config] = {
            "overall": block(rows),
            "per_criterion": {
                name: block([r for r in rows if r["criterion"] == name]) for name in names
            },
            "calls_per_kind": calls(rows),
        }
    return {
        "configs": by_config,
        "elapsed_seconds": round(elapsed, 1),
        "config": {
            "model": model_id,
            "verify_pass": LLM_BBOX_VERIFY_PASS,
            "min_area": LLM_BBOX_MIN_AREA,
            "max_area": LLM_BBOX_MAX_AREA,
            "refine": LLM_BBOX_REFINE,
            "verify_images": verify_images,
            "detector": detector_client.status(),
        },
    }


def _pct(value) -> str:
    """A rate for the table, or n/a. No nested f-strings — the container runs
    Python 3.11, where an f-string cannot reuse its own quote character."""
    return "n/a" if value is None else format(value, ".0%")


def _row(name: str, row: dict) -> str:
    iou = row["mean_detector_iou"]
    iou_text = "n/a" if iou is None else format(iou, ".2f")
    attempts = row["mean_attempts_to_accept"] or 0
    return (
        f"{name[:27]:<28}{row['n']:>4}{row['attempt_1_valid_rate']:>10.0%}"
        f"{row['verify_pass_rate']:>9.0%}{row['accepted_rate']:>10.0%}"
        f"{_pct(row.get('hit_rate')):>7}{_pct(row.get('false_accept_rate')):>8}"
        f"{row['bbox_null_rate']:>7.0%}{attempts:>10.2f}{iou_text:>8}"
    )


def _print_summary(summary: dict) -> None:
    header = (
        f"{'criterion':<28}{'n':>4}{'a1 valid':>10}{'verify':>9}"
        f"{'accepted':>10}{'hit':>7}{'false+':>8}{'null':>7}{'attempts':>10}{'iou':>8}"
    )
    for config, block in summary["configs"].items():
        print("=" * len(header))
        print(f"configuration: {config} (examples shown to: "
              f"{', '.join(k for k, on in zip(CALL_KINDS, CONFIGS[config]) if on) or 'nothing'})")
        print(header)
        print("-" * len(header))
        for name, row in block["per_criterion"].items():
            print(_row(name, row))
            for other, rate in (row.get("landed_on_rates") or {}).items():
                print(f"    landed on {other!r}: {rate:.0%} of runs")
        print("-" * len(header))
        print(_row("OVERALL", block["overall"]))
        for kind, cost in block["calls_per_kind"].items():
            tokens = cost["mean_prompt_tokens"]
            seconds = cost["mean_seconds"]
            print(f"  {kind:<7} {cost['calls']:>4} calls ({cost['failed']} failed), "
                  f"prompt tokens {'n/a' if tokens is None else tokens}, "
                  f"{'n/a' if seconds is None else seconds}s each")
    print("=" * len(header))
    print(f"{summary['elapsed_seconds']}s total.")
    off = summary["configs"].get("off", {}).get("overall")
    if off and off["attempt_1_valid_rate"] < 0.5:
        print(
            "\nAttempt-1 validity is under 50%: most first answers are not usable "
            "boxes. Per the regions plan § 3.3, prefer the open-vocabulary "
            "detector (a `detector` criterion) as the primary source and keep "
            "llm criteria with options.boxes as the fallback for labels it "
            "cannot name."
        )
    if off and off["mean_detector_iou"] is not None and off["mean_detector_iou"] < 0.5:
        print(
            "\nMean IoU with the detector is under 0.5: the two sources disagree "
            "about what these criteria mean. Re-word the criteria before "
            "re-tuning any threshold."
        )


def main() -> None:
    fixtures = _repo_fixture_dir()
    parser = argparse.ArgumentParser(
        description="Measure the vision model's bounding-box grounding quality.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--images", default=str(fixtures),
        help=f"directory of images, searched recursively (default: {fixtures})",
    )
    parser.add_argument(
        "--criteria", default=None,
        help='JSON: {"file.jpg": ["criterion", ...]} or a flat list applied to '
             "every image (default: the built-in per-fixture criteria)",
    )
    parser.add_argument(
        "--truth", default=None,
        help='JSON: {"file.jpg": {"criteria": {"name": [[x1, y1, x2, y2], ...]}, '
             '"decoys": {"name": [...]}}} in original page pixels, every copy listed; '
             "its criteria are asked (decoys never are) and every accepted box is "
             "scored against them (hit on any copy, false accept, IoU, centre error, "
             "landed on)",
    )
    parser.add_argument(
        "--reference-image", default=None,
        help="an image whose --truth boxes become the examples (one PASS composite "
             "and crop per criterion, from its first box); never measured as its own "
             "candidate",
    )
    parser.add_argument(
        "--references", nargs="+", default=None, metavar="REFERENCE_ID",
        help="stored references (CLASSIFIER_REFERENCE_DIR) to take the examples from, "
             "matched by criterion name",
    )
    parser.add_argument(
        "--configs", default=",".join(CONFIGS),
        help="which calls see the examples, comma-separated: "
             f"{', '.join(CONFIGS)} (default: all five)",
    )
    parser.add_argument(
        "--verify-images", choices=("crop", "composite"), default="crop",
        help="what a guided verify is shown (the verify and all configs): crop, the "
             "examples' region crops as the service does (default), or composite, the "
             "same composites the ask is shown",
    )
    parser.add_argument(
        "--repeats", type=int, default=5,
        help="runs per image x criterion x configuration (default: 5 — the model "
             "samples at temperature 1.0)",
    )
    parser.add_argument(
        "--attempts", type=int, default=LLM_BBOX_MAX_ATTEMPTS,
        help=f"loop attempts per criterion (default: {LLM_BBOX_MAX_ATTEMPTS})",
    )
    parser.add_argument(
        "--model", default=None,
        help="registry model id to measure (models.yaml / GET /models; default: the "
             "registry default). Must be a vision model.",
    )
    parser.add_argument("--out", default=None, help="write the full JSON report here")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
