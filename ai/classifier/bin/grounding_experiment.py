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

    # A test set of many pages, in two steps (every page x criterion x config
    # at once is hours). Copy the set in — images/, truth.json and the set's
    # selection.json — and derive the groups (one per project: a project's
    # pages are likely one utility's, so never each other's example):
    docker exec classifier mkdir -p /data/grounding_ops
    docker cp images         classifier:/data/grounding_ops/images
    docker cp truth.json     classifier:/data/grounding_ops/truth.json
    docker cp selection.json classifier:/data/grounding_ops/selection.json
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py \\
        --groups-from-selection /data/grounding_ops/selection.json \\
        --write-groups /data/grounding_ops/groups.json
    # Step 1 — the baseline, no examples, on everything; writes the pairs
    # that hit in under half their repeats:
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py \\
        --images /data/grounding_ops/images --truth /data/grounding_ops/truth.json \\
        --configs off --write-failures /data/grounding_ops/failures.json \\
        --out /data/grounding_ops/baseline.json
    # Step 2 — the guided configurations on those pairs only, each example
    # another page of the set, outside the candidate's project:
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py \\
        --images /data/grounding_ops/images --truth /data/grounding_ops/truth.json \\
        --subset /data/grounding_ops/failures.json \\
        --references-from-truth --groups /data/grounding_ops/groups.json \\
        --configs ask,refine,verify,all --out /data/grounding_ops/guided.json
    # The same with a FAIL example of a look-alike beside the PASS one, for
    # the criterion whose look-alikes are marked as the `usage look-alike`
    # decoy (pages without the real chart list it under `absent`, so a box
    # there is counted wrong and "not found" right):
    docker exec classifier uv run --no-sync python bin/grounding_experiment.py \\
        --images /data/grounding_ops/images --truth /data/grounding_ops/truth.json \\
        --subset /data/grounding_ops/failures.json \\
        --references-from-truth --groups /data/grounding_ops/groups.json \\
        --fail-from-decoys "usage look-alike=usage history" \\
        --configs ask,refine,verify,all --out /data/grounding_ops/guided_fail.json

``--criteria`` is a JSON object mapping file name to the criteria to ask
about, e.g.::

    {
      "house-01.jpg": ["solar panels", "a satellite dish"],
      "house-02.jpg": ["solar panels"]
    }

A bare list instead of an object applies those criteria to every image.

``--truth`` says where things really are, per file name: ``criteria`` —
the criteria to ask, each with EVERY real copy of it on the page —
``decoys`` — things never asked, used to name where a missed box landed (and,
with ``--fail-from-decoys``, as FAIL examples) — and ``absent`` — criteria
that are NOT on the page, asked anyway, where the right answer is no box at
all. Each box is ``[x1, y1, x2, y2]`` in ORIGINAL upright page pixels (the
frame regions are stored in); a bare box stands for a list of one. With it,
the criteria asked are the truth file's ``criteria`` plus its ``absent``
ones, and ``--criteria`` is ignored. ``unit-tests/classifier/documents/
grounding_truth.json`` holds the two photographed bills' logo, amount due and
account number — every copy: the header, the payment slip, bill 2's
right-hand page — and one decoy, the lines of text that name the utility
(what a model boxes when it mistakes the company name for the logo)::

    {"utility_bill.jpeg": {
        "criteria": {"the utility's logo": [[x1, y1, x2, y2], ...], ...},
        "decoys": {"text naming the utility": [[x1, y1, x2, y2], ...]},
        "absent": ["the monthly usage chart"]}, ...}

``absent`` (optional, a list of names; ``criteria`` may then be omitted)
measures the other half of identification: a page WITHOUT the feature,
usually with a look-alike on it (a bar graph that is not the usage chart —
mark it as a decoy). An absent criterion is asked exactly like any other —
it is in the image's criteria list — and its runs are scored ``expected:
"absent"``: CORRECT when the loop accepted no box (the model answered
``bbox: null``, or every attempt was rejected), WRONG when it accepted one;
``landed_on`` still names the decoy or criterion a wrong box landed on. A
name both in ``absent`` and in ``criteria`` or ``decoys`` is refused. Absent
pairs never count toward the hit rate: the summary reports them on their own
(below), and an absent pair is never a PASS example's source
(``--references-from-truth`` takes examples only from ``criteria`` boxes).

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

``--references-from-truth`` (a set of many pages, with ``--truth``) takes
each example from the set itself: for every candidate x criterion x repeat,
ONE other page of the truth file with a box for that criterion, built from
its first box exactly as ``--reference-image`` builds one (cached per page
and criterion). Never the candidate, and — with ``--groups``, a JSON
``{"file.jpg": group}`` — never a page of the candidate's group: a project's
other pages are likely the same utility's bills, and an example from the
same template is not the generalisation being measured. The page is picked
deterministically: the eligible pages sorted, ``(crc32(candidate) + repeat)
% n`` — a rerun picks the same, and the repeats rotate through different
references. A pair with no eligible page runs unguided, noted. Every record
says which page was its example (``reference_image``, ``reference_note``).
``--groups-from-selection <selection.json> --write-groups <groups.json>``
writes the groups a test set's ``selection.json`` implies (each
``pages[].image`` to its ``links[].project_id``) and exits.

``--fail-from-decoys DECOY=CRITERION`` (repeatable; needs
``--references-from-truth``) adds a FAIL example after the PASS one — the
pairing production sends at three images per prompt when the reference
criterion's hint is presence (``analysis.references.box_selection``). It
measures IDENTIFICATION: a look-alike the loop boxes instead of the real
thing (a graph that resembles the monthly usage chart), marked in the truth
file as a decoy. ``DECOY`` is a decoy name; ``CRITERION`` a truth criterion,
exact or a case-insensitive substring of exactly one (unknown or ambiguous is
refused). For every candidate x mapped criterion x repeat the FAIL example
is ONE other page with a box under that decoy name and no group shared with
the candidate — the PASS rule — picked deterministically and rotated across
repeats like the PASS pick but with its own salt, so the two are chosen
independently (the PASS page itself may be picked: a page with the real
chart and a look-alike is good contrast). It is built from that page's FIRST
decoy box exactly as a PASS example is built from a truth box — the
composite (``references.render.composite``) and the crop
(``llm.boxes.example_crop``), with ``--reference-text`` its text — captioned
FAIL ("the outlined thing is NOT <criterion>; do not box it"), and shown
after the PASS example in the ask, refine and verify (the composites in a
``--verify-images composite`` verify). PASS only, with a
``fail_reference_note``, when no page is eligible, the page will not load,
the pair has no PASS example, or the model takes fewer than 3 images per
prompt. ``off`` and unmapped criteria are unchanged. Step 2 below with
``--fail-from-decoys "usage look-alike=usage history"`` is the measurement;
``landed on`` (below) counts how often accepted boxes landed on the decoy.

``--subset`` (``{"file.jpg": ["criterion", ...]}``) asks only those pairs;
an image not listed is skipped. ``--write-failures <file>`` writes, after a
run, the ``--subset`` of pairs whose ``off`` runs hit in under 50% of their
repeats (a run that accepted nothing is a miss) — and of ``absent`` pairs
whose ``off`` runs were CORRECT in under 50% of theirs — and prints how
many: step 1 of the recipe above measures the baseline on everything, step 2
runs the guided configurations only where the baseline fails.

``--verify-images`` (default ``crop``) is what the verify call is shown when
it carries examples (``verify`` and ``all``): ``crop``, the examples' region
crops, as the service does; ``composite``, the same composites the ask is
shown (the reference page with the feature outlined, the composite captions)
— ``llm.boxes.locate_criterion(verify_examples=...)``, which the service never
passes. Refine always sees the crops.

The loop's opt-in placement aids, each defaulting to the container's own
setting (so a bare run measures what production does) and passed to
``llm.boxes.locate_criterion`` / the example builders explicitly:

    --refine-mode grid|marks    CLASSIFIER_LLM_BBOX_REFINE_MODE: marks numbers
                                the OCR lines in the refine window and asks
                                which. The script OCRs each image ONCE
                                (``analysis.ocr.get_ocr_engine``, as the
                                service's text layer does) and caches it;
                                with no engine the attempts fall back to the
                                grid and say so.
    --reference-text / --no-…   CLASSIFIER_REFERENCE_BOX_TEXT: each example's
                                caption quotes its region's text, OCR'd as the
                                service does (``analysis.references
                                .region_text``).
    --verify-partial / --no-…   CLASSIFIER_LLM_BBOX_VERIFY_PARTIAL: the verify
                                also answers ``partial``; a rejected partial
                                crop's retry says "same place, larger".
    --verify-min-span F         CLASSIFIER_LLM_BBOX_VERIFY_MIN_SPAN: the
                                verify crop grown to at least F of the page.

The header prints them, ``summary.config`` holds them, and every record
carries them (``refine_mode``, ``reference_text``, ``verify_partial``,
``verify_min_span``); with marks, each configuration's summary adds
``marks_refined_rate`` (of the attempts that refined, how many the marks
pass refined rather than the grid fallback).

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
                        asked for, ``text naming the utility`` boxed; the
                        usage chart asked for, a ``usage look-alike`` boxed.
                        Per configuration, per criterion and overall
                        (``landed_on_rates``, a share of the runs), so a
                        FAIL example's effect is read here.
    absent              (--truth with ``absent``) the absent pairs on their
                        own, never in the rates above that need a truth box
                        (hit, false accept, IoU, centre error): ``n``,
                        ``correct_rate`` (no box accepted), ``wrong_accept_
                        rate`` (a box accepted where there is nothing to
                        find), ``bbox_null_rate`` (how often the model said
                        so itself rather than running out of attempts) and
                        ``landed_on_rates`` (where the wrong boxes went — the
                        look-alike decoy, usually). Per configuration, per
                        criterion and overall (``absent`` in each block).
    FAIL example        (--fail-from-decoys) ``fail_example_rate``: of the
                        mapped criterion's runs in a configuration that shows
                        examples, how many carried the FAIL beside the PASS.
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
    expected        (--truth) ``present`` (the pair has truth boxes; ``hit``
                    and the rest score it), ``absent`` (``correct`` scores
                    it; ``hit`` is null) or null (no truth for the pair).
    correct         an ``absent`` run only: true when no box was accepted.
                    Null on every other run — a present pair is ``hit``.
    reference_image the page the examples were drawn from — the
                    ``--reference-image``, or ``--references-from-truth``'s
                    page for this repeat; null with stored references or
                    none. ``reference_note`` says why a pair ran unguided.
    fail_reference_image  (--fail-from-decoys) the page the FAIL example came
                    from on this repeat; null for an unmapped criterion or
                    when none was picked. ``fail_reference_note`` says why a
                    mapped pair showed the PASS only (null when the FAIL was
                    there). ``summary.config`` holds ``fail_from_decoys``
                    (``{criterion: decoy}``) and ``max_images_per_prompt``.
    attempt_log     ``[a.as_dict() for a in loc.attempts]`` — each attempt's
                    box (``bbox_grid`` / ``bbox_px``, coarse and refined),
                    ``verify_score``, ``verify_reason`` and ``reject`` (the
                    sentence the retry is told), as ``localization.attempts``
                    has them — with the aids on, ``refine_mode``, ``marks``
                    (the chosen mark numbers), ``marks_shown``,
                    ``marks_fallback`` and ``verify_partial`` too.
    answers         every model request of the run in call order, as the
                    model answered it: ``{kind: ask | refine | verify,
                    attempt, request, bbox (ask / grid refine) | marks (marks
                    refine, label ``refine/marks/…``) | score (verify, plus
                    ``partial`` when it answered one), reason, ok}``.
                    ``attempt`` is the loop attempt (from the
                    call label, ``bbox/<criterion>#<n>``), ``request`` the
                    request within it (> 1 is a parse retry). ``score`` is
                    the model's own number, before the loop clamps it. When
                    the answer would not parse (or the request failed) ``ok``
                    is false and ``raw`` holds its content (or the error), cut
                    to 400 characters. The prompt is never stored — it holds
                    the images.
    example_text    the text the run's example captions quoted (the first
                    example's; null without --reference-text, without text,
                    or in the ``off`` configuration, which shows none).
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
import zlib
from typing import Any

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
    LLM_BBOX_MARKS_MAX,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_MAX_AREA,
    LLM_BBOX_MIN_AREA,
    LLM_BBOX_REFINE,
    LLM_BBOX_REFINE_MODE,
    LLM_BBOX_VERIFY_MIN_SPAN,
    LLM_BBOX_VERIFY_PARTIAL,
    LLM_BBOX_VERIFY_PASS,
    MAX_WORKING_DIMENSION,
    REFERENCE_BOX_TEXT,
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


# One image's truth: {"criteria": {name: [box, ...]}, "decoys": {name: [box,
# ...]}, "absent": [name, ...]} — every part present after _load_truth.
Truth = dict[str, Any]


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
    {name: [box, ...]}, "absent": [name, ...]}``, every box ``[x1, y1, x2,
    y2]`` in original upright pixels (a bare box for a list of one). Every
    real copy of a criterion is one of its boxes; decoys are never asked —
    they name where a missed box landed, and with ``--fail-from-decoys``
    become FAIL examples; ``absent`` names criteria that are NOT on the page,
    asked anyway and scored right when no box is accepted.

    ``decoys`` and ``absent`` are optional (each image comes back with all
    three parts); ``criteria`` may be left out only when ``absent`` is given.
    ``absent`` is a list of non-empty names, repeats collapsed in order. A
    name in two parts — a criterion that is also a decoy, or absent and
    boxed — is refused: a feature is either on the page or not."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(
            f"{path}: expected {{filename: {{\"criteria\": {{name: [box, ...]}}, "
            "\"decoys\": {name: [box, ...]}, \"absent\": [name, ...]}}"
        )
    truth: dict[str, Truth] = {}
    for image, entry in data.items():
        shape = (
            f"{path}: {image!r} must be {{\"criteria\": {{...}}, \"decoys\": {{...}}, "
            "\"absent\": [...]}} (decoys optional; criteria optional with absent)"
        )
        if not isinstance(entry, dict) or set(entry) - {"criteria", "decoys", "absent"}:
            raise SystemExit(shape)
        if not isinstance(entry.get("criteria", {} if "absent" in entry else None), dict):
            raise SystemExit(shape)
        if not isinstance(entry.get("decoys", {}), dict):
            raise SystemExit(shape)
        absent = entry.get("absent", [])
        if not isinstance(absent, list) or not all(
            isinstance(name, str) and name.strip() for name in absent
        ):
            raise SystemExit(
                f"{path}: {image!r} / absent must be a list of criterion names, got {absent!r}"
            )
        truth[image] = {
            part: {name: _boxes(path, f"{image!r} / {part} / {name!r}", value)
                   for name, value in (entry.get(part) or {}).items()}
            for part in ("criteria", "decoys")
        }
        truth[image]["absent"] = list(dict.fromkeys(absent))
        both = set(truth[image]["criteria"]) & set(truth[image]["decoys"])
        if both:
            raise SystemExit(f"{path}: {image!r}: {sorted(both)} is both a criterion and a decoy")
        for part, what in (("criteria", "a criterion with boxes"), ("decoys", "a decoy")):
            both = set(truth[image]["absent"]) & set(truth[image][part])
            if both:
                raise SystemExit(
                    f"{path}: {image!r}: {sorted(both)} is both absent and {what} — a "
                    "feature is either on the page (boxed) or not (absent)"
                )
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
    path: pathlib.Path,
    criteria: dict[str, list[list[float]]],
    *,
    with_text: bool = False,
    verdict: str = "PASS",
) -> dict[str, tuple[list[BoxExample], list[BoxExample]]]:
    """``--reference-image``: per criterion, ``(ask, crops)`` — one example
    built from the FIRST box listed for it (list the primary copy first), as
    production would show it: the composite drawn by
    ``references.render.composite`` (the caller's ``manual`` stroke), the
    crop cut by ``llm.boxes.example_crop`` — and, ``with_text``
    (``--reference-text``), the box's text, OCR'd by
    ``analysis.references.region_text`` exactly as a job reads a stored
    reference's (None when there is no engine or no text).

    ``verdict`` is ``PASS`` for a truth box. ``FAIL`` is ``--fail-from-decoys``:
    the boxes are a DECOY's (a look-alike), still labelled with the
    criterion, so the captions say "the outlined thing is NOT <criterion>; do
    not box it" (composite) / "a crop of something that is NOT <criterion>"
    (crop) — the FAIL example production shows beside a PASS one at three
    images per prompt (``analysis.references.box_selection``)."""
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
        text = _box_text(original, [x1, y1, x2, y2]) if with_text else None
        if with_text:
            print(f"    example text {path.name} / {criterion} ({verdict}): {text!r}")
        out[criterion] = (
            [BoxExample(criterion=criterion, verdict=verdict, image_b64=drawn, text=text)],
            [BoxExample(criterion=criterion, verdict=verdict, image_b64=crop, crop=True,
                        text=text)],
        )
    return out


def _box_text(original, box: list[float]) -> str | None:
    """``--reference-text``: what the text inside ``box`` (original pixels)
    says, as the service's caption would quote it — or None (no engine, no
    text, an OCR error: an example is shown without its sentence, never
    fatal)."""
    from analysis.ocr import get_ocr_engine
    from analysis.references import region_text

    try:
        return region_text(original, box, get_ocr_engine())
    except Exception as exc:  # noqa: BLE001 — as JobReferences.text_of
        print(f"    region text failed: {exc}", file=sys.stderr)
        return None


# ``--refine-mode marks``: each image's OCR lines (``TextLayer.lines``' shape,
# original pixels), recognised once per run.
_OCR_LINES: dict[str, list[dict] | None] = {}


def _ocr_lines(path: pathlib.Path, original) -> list[dict] | None:
    """The image's OCR text lines, as the service's text layer holds them for
    a photo (``recognize_text_layer`` → ``engine.recognize(page)``): one pass
    per image, cached. None when no OCR engine is available — the marks
    refine then falls back to the grid, and each attempt records why."""
    key = str(path.resolve())
    if key not in _OCR_LINES:
        from analysis.ocr import get_ocr_engine

        engine = get_ocr_engine()
        if engine is None:
            print(f"    {path.name}: no OCR engine — the marks refine will fall back to the grid",
                  file=sys.stderr)
            _OCR_LINES[key] = None
        else:
            t0 = time.monotonic()
            _OCR_LINES[key] = list(engine.recognize(original).lines)
            print(f"    OCR: {len(_OCR_LINES[key])} line(s) in {time.monotonic() - t0:.1f}s")
    return _OCR_LINES[key]


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
    label: str, by_name: dict[str, list[dict]], spec: ModelSpec, *, with_text: bool = False,
) -> tuple[list[BoxExample], list[BoxExample]]:
    """One criterion's ``(ask, crops)`` from stored references, chosen and
    loaded exactly as a job's ``box_examples`` does (no position check;
    ``with_text`` is ``--reference-text``)."""
    from analysis.references import CriterionGuide, JobReferences

    examples = by_name.get(label.casefold().strip(), [])
    guide = CriterionGuide(label, "explicit", {}, examples)
    box_guide = await JobReferences(plan={}).box_examples(
        guide, spec, position_check=False, with_text=with_text,
    )
    if box_guide.note:
        print(f"    {label}: {box_guide.note}")
    return box_guide.ask, box_guide.crops


# ``--references-from-truth``: the example for (candidate, criterion, repeat).
# The note a pair gets when no page can serve as its example.
NO_TRUTH_REFERENCE = (
    "no other page in --truth has a box for this criterion outside the candidate's group "
    "— run unguided"
)


def _truth_reference(
    candidate: str,
    label: str,
    repeat: int,
    truth: dict[str, Truth],
    groups: dict[str, frozenset[str]],
    available: set[str],
) -> str | None:
    """``--references-from-truth``: the ONE other page that is ``candidate``'s
    example for ``label`` on this ``repeat``, or None when no page qualifies.

    Eligible: a page of the truth file with a box for ``label``, present
    under ``--images``, neither the candidate nor in a group it shares
    (``--groups``; a page with no group shares none). A page where ``label``
    is ``absent`` has no box for it and is never eligible — a PASS example
    is always a real copy. Picked
    deterministically — the eligible pages sorted, then ``(crc32(candidate) +
    repeat) % n`` — so a rerun picks the same page and the repeats rotate
    through different references (each repeat is a different example, not
    the same one five times)."""
    mine = groups.get(candidate, frozenset())
    eligible = sorted(
        name for name, entry in truth.items()
        if name != candidate and name in available and entry["criteria"].get(label)
        and label not in entry.get("absent", ())
        and not mine & groups.get(name, frozenset())
    )
    if not eligible:
        return None
    return eligible[(zlib.crc32(candidate.encode("utf-8")) + repeat) % len(eligible)]


# ``--fail-from-decoys``: the FAIL example beside the PASS one. The notes a
# pair gets when it is shown the PASS example only.
NO_FAIL_REFERENCE = (
    "no other page in --truth has a {decoy!r} decoy box outside the candidate's group — "
    "PASS only"
)
FAIL_NEEDS_PASS = "no PASS example for this pair — a FAIL is only shown beside one"
FAIL_NEEDS_ROOM = (
    "--model {model} takes {images} image(s) per prompt; PASS + FAIL + the candidate need 3 "
    "— PASS only"
)


def _fail_map(specs: list[str], truth: dict[str, Truth]) -> dict[str, str]:
    """``--fail-from-decoys DECOY=CRITERION`` (repeatable) → ``{criterion:
    decoy}``.

    ``DECOY`` is a decoy name in the truth file (exact, else the one name
    equal to it ignoring case). ``CRITERION`` names a truth criterion:
    exactly, else the one criterion equal to it ignoring case, else the ONE
    criterion containing it ignoring case — so a long criterion need not be
    pasted (``"usage look-alike=usage history"``). The criteria are every
    name the truth file asks — boxed or ``absent`` — since a FAIL example of
    a look-alike is what stops a wrong box on a page without the feature.
    Unknown, ambiguous, or a criterion mapped twice (it has one FAIL slot) is
    refused, naming what there is to choose from."""
    decoys = sorted({d for entry in truth.values() for d in entry["decoys"]})
    criteria = sorted({
        c for entry in truth.values() for c in [*entry["criteria"], *entry.get("absent", ())]
    })

    def one(value: str, names: list[str], *, substring: bool) -> list[str]:
        if value in names:
            return [value]
        folded = [n for n in names if n.casefold() == value.casefold()]
        if folded or not substring:
            return folded
        return [n for n in names if value.casefold() in n.casefold()]

    out: dict[str, str] = {}
    for spec in specs:
        decoy, sep, wanted = (part.strip() for part in spec.partition("="))
        if not sep or not decoy or not wanted:
            raise SystemExit(f"--fail-from-decoys {spec!r}: expected DECOY=CRITERION")
        found = one(decoy, decoys, substring=False)
        if not found:
            raise SystemExit(
                f"--fail-from-decoys {spec!r}: no decoy named {decoy!r} in --truth "
                f"(decoys: {', '.join(map(repr, decoys)) or 'none'})"
            )
        matches = one(wanted, criteria, substring=True)
        if not matches:
            raise SystemExit(
                f"--fail-from-decoys {spec!r}: {wanted!r} matches no criterion in --truth "
                f"(criteria: {', '.join(map(repr, criteria)) or 'none'})"
            )
        if len(matches) > 1:
            raise SystemExit(
                f"--fail-from-decoys {spec!r}: {wanted!r} is ambiguous — it matches "
                f"{', '.join(map(repr, matches))}; give more of the criterion's name"
            )
        criterion = matches[0]
        if criterion in out:
            raise SystemExit(
                f"--fail-from-decoys {spec!r}: {criterion!r} is already mapped to decoy "
                f"{out[criterion]!r} — a criterion has one FAIL example"
            )
        out[criterion] = found[0]
    return out


def _truth_fail_reference(
    candidate: str,
    decoy: str,
    repeat: int,
    truth: dict[str, Truth],
    groups: dict[str, frozenset[str]],
    available: set[str],
) -> str | None:
    """``--fail-from-decoys``: the ONE other page whose first ``decoy`` box is
    ``candidate``'s FAIL example on this ``repeat``, or None when no page
    qualifies.

    Eligible as ``_truth_reference``'s PASS pick is — present under
    ``--images``, not the candidate, no group shared with it (``--groups``) —
    but with a box under the DECOY name. The PASS reference's own page is
    allowed: a page with the real feature AND a look-alike is good contrast.
    Picked deterministically and rotated across repeats like the PASS pick,
    with its own salt (the decoy name), so the FAIL page is chosen
    independently of the PASS page."""
    mine = groups.get(candidate, frozenset())
    eligible = sorted(
        name for name, entry in truth.items()
        if name != candidate and name in available and entry["decoys"].get(decoy)
        and not mine & groups.get(name, frozenset())
    )
    if not eligible:
        return None
    salt = f"fail:{decoy}:{candidate}".encode("utf-8")
    return eligible[(zlib.crc32(salt) + repeat) % len(eligible)]


def _load_groups(path: pathlib.Path) -> dict[str, frozenset[str]]:
    """``--groups``: file name → its group, as ``{name: group}`` — a string or
    number, or a list for a page in several (it then shares a group with
    every page in any of them). A page that is not listed is in no group:
    only itself is excluded as its own reference."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected {{filename: group}}")
    groups: dict[str, frozenset[str]] = {}
    for name, group in data.items():
        values = group if isinstance(group, list) else [group]
        if not values or not all(isinstance(v, (str, int)) and not isinstance(v, bool)
                                 for v in values):
            raise SystemExit(f"{path}: {name!r}: a group is a string, a number or a list of them")
        groups[name] = frozenset(str(v) for v in values)
    return groups


def _groups_from_selection(path: pathlib.Path) -> dict[str, object]:
    """``--groups-from-selection``: the ``--groups`` mapping a test set's
    ``selection.json`` implies — each ``pages[].image`` → the project id of
    its ``links[].project_id`` (a sorted list when it links to several). A
    project's pages are likely the same utility's bills, so a page is never
    another's example within one project. Ids only: nothing else is read."""
    data = json.loads(path.read_text(encoding="utf-8"))
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        raise SystemExit(f"{path}: expected {{\"pages\": [{{\"image\", \"links\": [...]}}, ...]}}")
    groups: dict[str, object] = {}
    for page in pages:
        image = page.get("image") if isinstance(page, dict) else None
        if not isinstance(image, str):
            raise SystemExit(f"{path}: a page without an 'image' file name")
        ids = sorted({link.get("project_id") for link in page.get("links") or []
                      if isinstance(link, dict) and link.get("project_id") is not None})
        if ids:
            groups[image] = ids[0] if len(ids) == 1 else ids
    return groups


def _load_subset(path: pathlib.Path) -> dict[str, list[str]]:
    """``--subset`` (and what ``--write-failures`` writes): file name → the
    criteria to ask on it."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(v, list) and all(isinstance(c, str) for c in v) for v in data.values()
    ):
        raise SystemExit(f"{path}: expected {{filename: [criterion, ...]}}")
    return data


# A pair whose baseline (``off``) hit rate is under this goes to
# ``--write-failures`` — the pairs the guided configurations are run on next.
FAILURE_HIT_RATE = 0.5


def _failures(records: list[dict]) -> tuple[dict[str, list[str]], int]:
    """``--write-failures``: the ``--subset`` of pairs whose ``off`` runs hit
    in under FAILURE_HIT_RATE of their repeats (a run that accepted nothing
    is a miss) — and of ``absent`` pairs whose ``off`` runs were CORRECT (no
    box accepted) in under FAILURE_HIT_RATE of theirs — in run order, and how
    many pairs were measured. Pairs with no truth at all are not measured."""
    runs: dict[tuple[str, str], list[bool]] = {}
    for r in records:
        if r["config"] != "off":
            continue
        if r.get("expected") == "absent":
            good = bool(r["correct"])
        elif r["hit"] is not None:
            good = bool(r["hit"])
        else:
            continue
        runs.setdefault((r["image"], r["criterion"]), []).append(good)
    failing: dict[str, list[str]] = {}
    for (image, criterion), good in runs.items():
        if _rate(good) < FAILURE_HIT_RATE:
            failing.setdefault(image, []).append(criterion)
    return failing, len(runs)


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
    """``{expected, correct, hit, truth_copy, iou, center_error_grid,
    landed_on}`` for one run. ``expected`` is ``present`` when this image has
    truth boxes for ``label``, ``absent`` when its truth lists ``label`` as
    absent, and None (every other key None too) when it has neither. An
    unaccepted present run is not a hit and has no IoU.

    A HIT is the accepted box's centre inside ANY copy of the criterion (a
    bill repeats its logo, amount and account number on the payment slip, and
    a correct box on any of them is correct). IoU and the centre error (grid
    units) are against the best-matching copy — the highest IoU, the closest
    centre on a tie — and ``truth_copy`` is its index. ``landed_on`` names,
    for a miss, the OTHER criterion or decoy with a box holding the centre
    (criteria first, then decoys; None when it landed on nothing listed).

    An ABSENT run has no copy to hit: ``correct`` is True when nothing was
    accepted (``bbox: null``, or every attempt rejected) and False when a box
    was — that box is wrong by definition, and ``landed_on`` names what it
    landed on, the same way. ``hit`` and the copy keys stay None, so an
    absent run can never reach a hit rate; ``correct`` is None on a present
    run."""
    if label in truth.get("absent", ()):
        return _scored("absent", correct=accepted is None,
                       landed_on=None if accepted is None else _landed_on(accepted, label, truth))
    copies = truth["criteria"].get(label)
    if not copies:
        return _scored(None)
    if accepted is None:
        return _scored("present", hit=False)
    centre = _centre(accepted)

    def error(box: list[float]) -> float:
        tx, ty = _centre(box)
        return math.hypot(
            (centre[0] - tx) / geometry.width * LLM_BBOX_GRID,
            (centre[1] - ty) / geometry.height * LLM_BBOX_GRID,
        )

    best = max(range(len(copies)), key=lambda i: (_iou(accepted, copies[i]), -error(copies[i])))
    hit = any(_inside(centre, box) for box in copies)
    return _scored("present", hit=hit, truth_copy=best,
                   iou=round(_iou(accepted, copies[best]), 4),
                   center_error_grid=round(error(copies[best]), 1),
                   landed_on=None if hit else _landed_on(accepted, label, truth))


def _scored(expected: str | None, *, correct: bool | None = None, hit: bool | None = None,
            truth_copy: int | None = None, iou: float | None = None,
            center_error_grid: float | None = None, landed_on: str | None = None) -> dict:
    """One run's truth keys, always all of them, in one order."""
    return {"expected": expected, "correct": correct, "hit": hit, "truth_copy": truth_copy,
            "iou": iou, "center_error_grid": center_error_grid, "landed_on": landed_on}


def _landed_on(accepted: list[float], label: str, truth: Truth) -> str | None:
    """The OTHER criterion or decoy whose box holds ``accepted``'s centre —
    criteria first, then decoys — or None when it landed on nothing listed."""
    centre = _centre(accepted)
    others = [(name, boxes) for name, boxes in truth["criteria"].items() if name != label]
    others += list(truth["decoys"].items())
    return next(
        (name for name, boxes in others if any(_inside(centre, b) for b in boxes)), None,
    )


# The loop's call labels: "bbox/<criterion>#<attempt>", "refine/…" (the marks
# refine: "refine/marks/…"), "verify/…".
_LABEL = re.compile(r"^(?:bbox|refine|verify)/.*#(\d+)$")
_MARKS_LABEL = "refine/marks/"


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
        if "partial" in parsed:
            out["partial"] = parsed.get("partial")
    elif (fields.get("label") or "").startswith(_MARKS_LABEL):
        out["marks"] = parsed.get("marks")
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
        # The criteria are asked, and so are the absent ones (the right answer
        # there is no box); decoys only name where a miss landed.
        criteria_map = {
            p.name: [*(truth.get(p.name) or {}).get("criteria", {}),
                     *(truth.get(p.name) or {}).get("absent", [])]
            for p in paths
        }
    else:
        criteria_map = _load_criteria(
            pathlib.Path(args.criteria).resolve() if args.criteria else None,
            [p.name for p in paths],
        )
    if args.subset:
        # Only the listed pairs; an image not listed is skipped.
        subset = _load_subset(pathlib.Path(args.subset).resolve())
        wanted = sum(len(set(v)) for v in subset.values())
        criteria_map = {
            name: [c for c in labels if c in set(subset.get(name, []))]
            for name, labels in criteria_map.items()
        }
        kept = sum(len(v) for v in criteria_map.values())
        print(f"subset  : {kept} of the {wanted} pair(s) in {args.subset} found among the "
              "images and their criteria")

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
    if sum(bool(x) for x in (reference_path, args.references, args.references_from_truth)) > 1:
        raise SystemExit(
            "give one of --reference-image, --references, --references-from-truth"
        )
    if args.references_from_truth and not truth:
        raise SystemExit("--references-from-truth draws its examples from --truth: give one")
    if args.groups and not args.references_from_truth:
        raise SystemExit("--groups only says which pages may not be each other's example: "
                         "it needs --references-from-truth")
    if args.write_failures and not truth:
        raise SystemExit("--write-failures needs --truth: a failure is a missed hit")
    if args.fail_from_decoys and not args.references_from_truth:
        raise SystemExit("--fail-from-decoys adds a FAIL example beside the PASS one "
                         "--references-from-truth picks: it needs --references-from-truth")
    # {criterion: decoy}: which criteria get a FAIL example, from which decoy.
    fail_map = _fail_map(args.fail_from_decoys, truth) if args.fail_from_decoys else {}
    fail_room = spec.max_images_per_prompt >= 3
    groups = _load_groups(pathlib.Path(args.groups).resolve()) if args.groups else {}
    by_name = {p.name: p for p in paths}
    truth_examples: dict[tuple[str, str], tuple[list[BoxExample], list[BoxExample]] | None] = {}
    fail_examples: dict[tuple[str, str, str], tuple[list[BoxExample], list[BoxExample]] | None] = {}
    if reference_path is not None:
        if not (truth.get(reference_path.name) or {}).get("criteria"):
            raise SystemExit(
                f"--reference-image: {reference_path.name} has no truth boxes in --truth — "
                "they are what the examples are drawn from"
            )
        if spec.max_images_per_prompt < 2:
            raise SystemExit(f"--model {spec.id} takes 1 image per prompt: no room for an example")
        image_examples = _image_examples(reference_path,
                                         truth[reference_path.name]["criteria"],
                                         with_text=args.reference_text)
    elif args.references:
        stored, stored_pages = _stored_examples(args.references)
    elif args.references_from_truth and spec.max_images_per_prompt < 2:
        raise SystemExit(f"--model {spec.id} takes 1 image per prompt: no room for an example")
    examples_given = bool(image_examples or stored or args.references_from_truth)
    if not examples_given and configs != ["off"]:
        print("no --reference-image / --references / --references-from-truth: only the "
              "'off' configuration is measured")
        configs = ["off"]
    if args.write_failures and "off" not in configs:
        raise SystemExit("--write-failures reads the 'off' runs: include off in --configs")

    def truth_example(
        page: str, label: str
    ) -> tuple[list[BoxExample], list[BoxExample]] | None:
        """``page``'s example for ``label`` (its first box), built once per
        (page, criterion) as ``--reference-image`` builds it; None when the
        page will not load (the pair then runs unguided — never fatal)."""
        key = (page, label)
        if key not in truth_examples:
            try:
                truth_examples[key] = _image_examples(
                    by_name[page], {label: truth[page]["criteria"][label]},
                    with_text=args.reference_text,
                )[label]
            except SystemExit as exc:   # _image_examples: "unreadable"
                print(f"    {exc}", file=sys.stderr)
                truth_examples[key] = None
        return truth_examples[key]

    def fail_example(
        page: str, label: str, decoy: str
    ) -> tuple[list[BoxExample], list[BoxExample]] | None:
        """``page``'s FAIL example for ``label``: its FIRST ``decoy`` box,
        built once per (page, criterion, decoy) exactly as a PASS example is
        built from a truth box, captioned FAIL; None when the page will not
        load (the pair then shows the PASS only — never fatal)."""
        key = (page, label, decoy)
        if key not in fail_examples:
            try:
                fail_examples[key] = _image_examples(
                    by_name[page], {label: truth[page]["decoys"][decoy]},
                    with_text=args.reference_text, verdict="FAIL",
                )[label]
            except SystemExit as exc:   # _image_examples: "unreadable"
                print(f"    {exc}", file=sys.stderr)
                fail_examples[key] = None
        return fail_examples[key]

    print(f"model   : {spec.id} ({spec.served_name}) at {spec.url}")
    print(f"detector: {'on — ' + str(detector_client.status()['url_host']) if detector_client.is_configured() else 'off'}")
    print(f"loop    : {args.attempts} attempt(s), verify >= {LLM_BBOX_VERIFY_PASS}, "
          f"area {LLM_BBOX_MIN_AREA:.3%}-{LLM_BBOX_MAX_AREA:.0%}, refine {LLM_BBOX_REFINE}")
    if args.references_from_truth:
        excluded = (f"outside its group (--groups {args.groups}, {len(groups)} page(s) "
                    "grouped)" if args.groups else "other than itself (no --groups)")
        print(f"examples: from --truth: one other page per candidate x criterion x repeat, "
              f"{excluded}")
    else:
        print(f"examples: {reference_path.name if reference_path else (', '.join(args.references) if args.references else 'none')}")
    for criterion, decoy in fail_map.items():
        print(f"fail    : {criterion!r} also shown a FAIL example — the first {decoy!r} decoy box "
              "of one other page per candidate x repeat, same group rule, own rotation")
    if fail_map and not fail_room:
        print(f"fail    : --model {spec.id} takes {spec.max_images_per_prompt} image(s) per "
              "prompt; PASS + FAIL + the candidate need 3 — every pair shows the PASS only")
    print(f"configs : {', '.join(configs)}, {args.repeats} repeat(s) each")
    print(f"verify  : shown {args.verify_images}s when it carries examples")
    aids = _aids(args)
    print(f"aids    : refine mode {aids['refine_mode']}"
          f"{' (at most ' + str(LLM_BBOX_MARKS_MAX) + ' marks)' if aids['refine_mode'] == 'marks' else ''}, "
          f"reference text {'on' if aids['reference_text'] else 'off'}, "
          f"verify partial {'on' if aids['verify_partial'] else 'off'}, "
          f"verify min span {aids['verify_min_span']:g}")
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
        boxes: Truth = truth.get(path.name) or {"criteria": {}, "decoys": {}, "absent": []}
        print(f"{path.name} ({geometry.width}x{geometry.height})")
        # The marks refine's lines: one OCR pass per image, as the service's
        # text layer is one pass per page.
        text_lines = _ocr_lines(path, original) if aids["refine_mode"] == "marks" else None

        for label in labels:
            if image_examples:
                ask_examples, crop_examples = image_examples.get(label, ([], []))
            elif stored:
                ask_examples, crop_examples = await _stored_guide(
                    label, stored, spec, with_text=args.reference_text,
                )
            else:
                ask_examples, crop_examples = [], []
            for repeat in range(1, args.repeats + 1):
                # One example per (candidate, criterion, repeat), the same in
                # every configuration so the configurations compare fairly.
                reference_image = reference_path.name if reference_path else None
                reference_note = None
                if args.references_from_truth:
                    reference_image = _truth_reference(
                        path.name, label, repeat, truth, groups, set(by_name)
                    )
                    built = (truth_example(reference_image, label)
                             if reference_image is not None else None)
                    if reference_image is None:
                        reference_note = NO_TRUTH_REFERENCE
                    elif built is None:
                        reference_note = f"{reference_image} would not load — run unguided"
                    ask_examples, crop_examples = built or ([], [])
                    print(f"    {label[:30]} #{repeat}: example {reference_image or 'none'} "
                          f"{'(' + reference_note + ')' if reference_note else ''}".rstrip())
                # --fail-from-decoys: a FAIL example AFTER the PASS one, in
                # every step that shows examples (new lists — the PASS pair
                # is cached and shared across repeats).
                fail_reference_image = None
                fail_reference_note = None
                decoy = fail_map.get(label)
                if decoy is not None:
                    if not ask_examples:
                        fail_reference_note = FAIL_NEEDS_PASS
                    elif not fail_room:
                        fail_reference_note = FAIL_NEEDS_ROOM.format(
                            model=spec.id, images=spec.max_images_per_prompt)
                    else:
                        fail_reference_image = _truth_fail_reference(
                            path.name, decoy, repeat, truth, groups, set(by_name)
                        )
                        built_fail = (fail_example(fail_reference_image, label, decoy)
                                      if fail_reference_image is not None else None)
                        if fail_reference_image is None:
                            fail_reference_note = NO_FAIL_REFERENCE.format(decoy=decoy)
                        elif built_fail is None:
                            fail_reference_note = (
                                f"{fail_reference_image} would not load — PASS only")
                        else:
                            ask_examples = list(ask_examples) + built_fail[0]
                            crop_examples = list(crop_examples) + built_fail[1]
                    print(f"    {label[:30]} #{repeat}: FAIL example "
                          f"{fail_reference_image or 'none'} "
                          f"{'(' + fail_reference_note + ')' if fail_reference_note else ''}"
                          .rstrip())
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
                        text_lines=text_lines,
                        refine_mode=aids["refine_mode"],
                        verify_partial=aids["verify_partial"],
                        verify_min_span=aids["verify_min_span"],
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
                        **aids,
                        "reference_image": reference_image,
                        "reference_note": reference_note,
                        # --fail-from-decoys: the FAIL example's page (null:
                        # none shown, or an unmapped criterion) and why not.
                        "fail_reference_image": fail_reference_image,
                        "fail_reference_note": fail_reference_note,
                        # What the example captions quoted (any guided step's
                        # examples are the same; null when none was shown).
                        "example_text": (
                            next((e.text for e in crop_examples + ask_examples if e.text), None)
                            if aids["reference_text"] and any(CONFIGS[config]) else None
                        ),
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
                    if record["expected"] == "absent":
                        hit = "  absent: right" if record["correct"] else "  absent: WRONG"
                    else:
                        hit = "" if record["hit"] is None else (
                            "  HIT" if record["hit"] else "  miss")
                    iou = (
                        f", iou {record['detector_iou']:.2f}"
                        if record["detector_iou"] is not None
                        else ""
                    )
                    print(f"  {mark} {config:<7}#{repeat} {label[:30]:<31} {detail}{iou}{hit}"
                          f"  ({elapsed:.1f}s)")
        print()

    summary = _summarise(records, time.monotonic() - started, spec.id, configs, bool(truth),
                         args.verify_images, aids, fail_map, spec.max_images_per_prompt)
    _print_summary(summary)
    if args.write_failures:
        failing, measured = _failures(records)
        pathlib.Path(args.write_failures).write_text(
            json.dumps(failing, indent=2), encoding="utf-8"
        )
        print(f"\n{sum(len(v) for v in failing.values())} of {measured} pair(s) hit in under "
              f"{FAILURE_HIT_RATE:.0%} of their 'off' runs — wrote {args.write_failures} "
              "(use it as --subset)")
    if args.out:
        out = pathlib.Path(args.out)
        out.write_text(
            json.dumps({"summary": summary, "records": records}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {out}")
    return summary


def _aids(args) -> dict:
    """The loop's opt-in aids this run sets — passed to the loop explicitly,
    printed in the header, kept in ``summary.config`` and on every record."""
    return {
        "refine_mode": "marks" if args.refine_mode == "marks" else "grid",
        "reference_text": bool(args.reference_text),
        "verify_partial": bool(args.verify_partial),
        "verify_min_span": float(args.verify_min_span),
    }


def _rate(values: list[bool]) -> float:
    return (sum(1 for v in values if v) / len(values)) if values else 0.0


def _landed_rates(rows: list[dict]) -> dict[str, float]:
    """``{name: share of rows}`` for the criteria / decoys accepted boxes
    landed on (``landed_on``), sorted by name."""
    landed: dict[str, int] = {}
    for r in rows:
        if r["landed_on"]:
            landed[r["landed_on"]] = landed.get(r["landed_on"], 0) + 1
    return {other: round(count / len(rows), 3) for other, count in sorted(landed.items())}


def _mean(values: list, digits: int = 3):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), digits) if values else None


def _summarise(
    records: list[dict], elapsed: float, model_id: str, configs: list[str], scored: bool,
    verify_images: str, aids: dict | None = None, fail_map: dict | None = None,
    max_images: int | None = None,
) -> dict:
    """Per configuration: the rates overall and per criterion, and what each
    call kind cost. ``fail_map`` (``--fail-from-decoys``, ``{criterion:
    decoy}``) and the model's ``max_images`` go into ``config``."""

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
        # Marks mode: of the attempts that went through a refine, how many
        # the marks pass refined (the rest fell back to the grid).
        refines = [a.get("refine_mode") for r in rows for a in r["attempt_log"]
                   if a.get("refine_mode")]
        if refines:
            out["marks_refined_rate"] = round(_rate([m == "marks" for m in refines]), 3)
        # --fail-from-decoys: of the runs of a MAPPED criterion in a config
        # that shows examples, how many carried the FAIL beside the PASS (the
        # rest have a fail_reference_note saying why not).
        mapped = [r for r in rows if any(CONFIGS[r["config"]])
                  and (r.get("fail_reference_image") or r.get("fail_reference_note"))]
        if mapped:
            out["fail_example_rate"] = round(_rate([
                bool(r.get("fail_reference_image")) and not r.get("fail_reference_note")
                for r in mapped
            ]), 3)
        # Present pairs only (an absent run's `hit` is None): the hit rate
        # never sees a page without the feature.
        truthed = [r for r in rows if r["hit"] is not None]
        if scored and truthed:
            out.update({
                "hit_rate": round(_rate([r["hit"] for r in truthed]), 3),
                "false_accept_rate": round(
                    _rate([r["accepted"] and not r["hit"] for r in truthed]), 3
                ),
                "mean_truth_iou": _mean([r["iou"] for r in truthed]),
                "mean_center_error_grid": _mean([r["center_error_grid"] for r in truthed], 1),
                "landed_on_rates": _landed_rates(truthed),
            })
        # Absent pairs on their own: right is no box at all.
        absent = [r for r in rows if r.get("expected") == "absent"]
        if scored and absent:
            out["absent"] = {
                "n": len(absent),
                "correct_rate": round(_rate([r["correct"] for r in absent]), 3),
                "wrong_accept_rate": round(_rate([not r["correct"] for r in absent]), 3),
                "bbox_null_rate": round(_rate([r["bbox_null"] for r in absent]), 3),
                "landed_on_rates": _landed_rates(absent),
            }
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
            **(aids or {}),
            "marks_max": LLM_BBOX_MARKS_MAX,
            "fail_from_decoys": dict(fail_map or {}),
            "max_images_per_prompt": max_images,
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


def _print_absent(row: dict) -> None:
    """A block's absent pairs, under its row — never in its hit columns
    (which count present pairs only)."""
    absent = row.get("absent")
    if not absent:
        return
    print(f"    absent: {absent['n']} run(s) — right (no box) {absent['correct_rate']:.0%}, "
          f"wrong accept {absent['wrong_accept_rate']:.0%}, "
          f"said bbox null {absent['bbox_null_rate']:.0%}")
    for other, rate in absent["landed_on_rates"].items():
        print(f"      wrong box landed on {other!r}: {rate:.0%} of absent runs")


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
            _print_absent(row)
        print("-" * len(header))
        print(_row("OVERALL", block["overall"]))
        _print_absent(block["overall"])
        marks_rate = block["overall"].get("marks_refined_rate")
        if marks_rate is not None:
            print(f"  marks refined {marks_rate:.0%} of the refines (the rest fell back to "
                  "the grid — attempt_log[].marks_fallback says why)")
        fail_rate = block["overall"].get("fail_example_rate")
        if fail_rate is not None:
            print(f"  FAIL example beside the PASS in {fail_rate:.0%} of the mapped runs "
                  "(the rest: fail_reference_note says why)")
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
             '"decoys": {"name": [...]}, "absent": ["name", ...]}} in original page '
             "pixels, every copy listed; its criteria and absent names are asked (decoys "
             "never are) and every accepted box is scored against them (hit on any copy, "
             "false accept, IoU, centre error, landed on); an absent pair is right when "
             "no box is accepted, reported apart from the hit rate",
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
        "--references-from-truth", action="store_true",
        help="for each candidate x criterion x repeat, take the example from ONE other page "
             "of --truth with a box for it, outside the candidate's --groups group; rotates "
             "across repeats",
    )
    parser.add_argument(
        "--fail-from-decoys", action="append", default=None, metavar="DECOY=CRITERION",
        help="with --references-from-truth: also show a FAIL example after the PASS one, "
             "the first DECOY box of one other page (outside the candidate's group, its own "
             "rotation) — 'the outlined thing is NOT <criterion>'. CRITERION is exact, or a "
             "case-insensitive substring of exactly one truth criterion. Needs 3 images per "
             "prompt (else PASS only, noted). Repeatable, one decoy per criterion",
    )
    parser.add_argument(
        "--groups", default=None,
        help='JSON {"file.jpg": group}: with --references-from-truth, a page never serves '
             "as the example for a page of its own group (default: only itself is excluded)",
    )
    parser.add_argument(
        "--groups-from-selection", default=None, metavar="SELECTION_JSON",
        help="write the --groups file a test set's selection.json implies (pages[].image -> "
             "its links[].project_id) to --write-groups, and exit — no model call",
    )
    parser.add_argument(
        "--write-groups", default=None, metavar="GROUPS_JSON",
        help="where --groups-from-selection writes",
    )
    parser.add_argument(
        "--subset", default=None,
        help='JSON {"file.jpg": ["criterion", ...]}: ask only these pairs (what '
             "--write-failures writes); images not listed are skipped",
    )
    parser.add_argument(
        "--write-failures", default=None, metavar="SUBSET_JSON",
        help="after the run, write the --subset of pairs whose 'off' runs hit in under "
             # argparse %-formats help: the format's own % is doubled into %%.
             f"{FAILURE_HIT_RATE:.0%}% of their repeats — or, for an absent pair, were right "
             "(no box) in under that share (needs --truth and the off config)",
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
        "--refine-mode", choices=("grid", "marks"),
        default="marks" if LLM_BBOX_REFINE_MODE == "marks" else "grid",
        help="how the refine pass asks: grid, or marks — the OCR lines in the window "
             "numbered (each image OCR'd once); default: CLASSIFIER_LLM_BBOX_REFINE_MODE",
    )
    parser.add_argument(
        "--reference-text", action=argparse.BooleanOptionalAction, default=REFERENCE_BOX_TEXT,
        help="quote each example's region text in its caption (OCR'd as the service does); "
             "default: CLASSIFIER_REFERENCE_BOX_TEXT",
    )
    parser.add_argument(
        "--verify-partial", action=argparse.BooleanOptionalAction,
        default=LLM_BBOX_VERIFY_PARTIAL,
        help="the verify also answers 'partial' (the crop cut the feature off); a rejected "
             "partial crop's retry says 'same place, larger'; default: "
             "CLASSIFIER_LLM_BBOX_VERIFY_PARTIAL",
    )
    parser.add_argument(
        "--verify-min-span", type=float, default=LLM_BBOX_VERIFY_MIN_SPAN, metavar="F",
        help="grow the verify crop to at least F (0-1) of the page on each axis; default: "
             "CLASSIFIER_LLM_BBOX_VERIFY_MIN_SPAN",
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
    if not 0.0 <= args.verify_min_span <= 1.0:
        parser.error("--verify-min-span is a fraction of the page, 0-1")
    if bool(args.groups_from_selection) != bool(args.write_groups):
        parser.error("--groups-from-selection and --write-groups go together")
    if args.groups_from_selection:
        groups = _groups_from_selection(pathlib.Path(args.groups_from_selection))
        pathlib.Path(args.write_groups).write_text(json.dumps(groups, indent=2),
                                                   encoding="utf-8")
        projects = {str(g) for v in groups.values() for g in (v if isinstance(v, list) else [v])}
        print(f"wrote {args.write_groups}: {len(groups)} page(s) in {len(projects)} group(s)")
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
