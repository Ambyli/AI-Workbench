"""Measure how well the vision model actually grounds — before you trust it.

Step 0 of phase 3 of the regions plan (kept outside the repo, in the operator's Claude project folder), kept as a script because the question
it answers is empirical and deployment-specific: *does asking Muse Glimmer for
a bounding box produce a usable one often enough to build on?* The enforcement
loop (`llm/boxes.py`) is designed for the answer being "not always", but the
numbers decide how to configure it — and whether the open-vocabulary detector
should be the primary source with the LLM as the fallback rather than the
other way round.

**This has to run where the model is.** It talks to `VISION_LLM_API` directly
(the `muse-glimmer` container on `ai_shared`), so it runs ON the box — in the
classifier container, or on the host with the port published. It cannot be run
from a laptop with no GPU behind it.

Usage::

    # Inside the classifier container, against the bundled fixtures
    docker compose exec classifier python bin/grounding_experiment.py

    # A directory of your own photos plus the criteria to look for
    python bin/grounding_experiment.py \\
        --images /data/roof-photos \\
        --criteria /data/roof-criteria.json \\
        --attempts 3 --out /data/grounding.json

``--criteria`` is a JSON object mapping file name to the criteria to ask
about, e.g.::

    {
      "house-01.jpg": ["solar panels", "a satellite dish"],
      "house-02.jpg": ["solar panels"]
    }

A bare list instead of an object applies those criteria to every image.

What it prints, per criterion and overall:

    attempt-1 valid     how often the FIRST box survived validation. This is
                        the headline number: a low one means most of the
                        model's answers are full-frame non-answers, and every
                        localisation costs at least two round trips.
    verify pass         how often a valid box's crop actually showed the
                        feature. A high attempt-1-valid with a low verify pass
                        means the model draws tidy boxes in the wrong place —
                        the worst case, because it looks right until you open
                        the crop.
    accepted            how often the loop ended with a box at all.
    mean attempts       how many rounds an accepted criterion needed.
    detector IoU        with DETECTOR_URL set, mean overlap between the
                        accepted box and the detector's best box for the same
                        label. Under ~0.5 the two sources disagree about what
                        the criterion means, which is a prompt problem, not a
                        geometry one.

Rule of thumb from the plan: if attempt-1 validity is under ~50%, make the
detector the primary source and the LLM loop the fallback.

Process flow position: none — a standalone operator tool. It imports the same
`llm.boxes` loop the pipeline runs, so what it measures is what production
does, not a reimplementation of it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import sys
import time

# The classifier's modules import each other by bare name (the container's
# WORKDIR). Running this from `bin/` needs the parent on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from common.vision import PageGeometry  # noqa: E402

from detector import client as detector_client  # noqa: E402
from llm import boxes as llm_boxes  # noqa: E402
from config import (  # noqa: E402
    GROUNDING_DEFAULT_CRITERIA,
    GROUNDING_IMAGE_SUFFIXES,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_MAX_AREA,
    LLM_BBOX_MIN_AREA,
    LLM_BBOX_VERIFY_PASS,
    MAX_WORKING_DIMENSION,
    VISION_LLM_API,
    VISION_LLM_MODEL,
)
from llm.client import encode_image_to_base64  # noqa: E402

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


async def run(args) -> dict:
    directory = pathlib.Path(args.images).resolve()
    paths = _images(directory)
    criteria_map = _load_criteria(
        pathlib.Path(args.criteria).resolve() if args.criteria else None,
        [p.name for p in paths],
    )

    print(f"model   : {VISION_LLM_MODEL} at {VISION_LLM_API}")
    print(f"detector: {'on — ' + str(detector_client.status()['url_host']) if detector_client.is_configured() else 'off'}")
    print(f"loop    : {args.attempts} attempt(s), verify >= {LLM_BBOX_VERIFY_PASS}, "
          f"area {LLM_BBOX_MIN_AREA:.3%}-{LLM_BBOX_MAX_AREA:.0%}")
    print(f"images  : {len(paths)} under {directory}\n")

    records: list[dict] = []
    started = time.monotonic()

    for path in paths:
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
        print(f"{path.name} ({geometry.width}x{geometry.height})")

        for label in labels:
            t0 = time.monotonic()
            regions, loc = await llm_boxes.locate_criterion(
                label,
                image_b64=image_b64,
                original_image=original,
                geometry=geometry,
                detector_regions=detector.get(label),
                max_attempts=args.attempts,
            )
            elapsed = time.monotonic() - t0
            first = loc.attempts[0] if loc.attempts else None
            accepted = next((a for a in loc.attempts if a.accepted), None)
            record = {
                "image": path.name,
                "criterion": label,
                "attempt_1_valid": bool(first and first.valid),
                "attempt_1_reject": first.reject if first else "no attempt made",
                "any_valid": any(a.valid for a in loc.attempts),
                "verify_scores": [a.verify_score for a in loc.attempts if a.valid],
                "accepted": accepted is not None,
                "accepted_attempt": loc.accepted_attempt,
                "attempts": len(loc.attempts),
                "calls": loc.calls,
                "detector_iou": accepted.detector_iou if accepted else None,
                "detector_boxes": len(detector.get(label) or []),
                "seconds": round(elapsed, 2),
                "bbox_px": accepted.bbox_px if accepted else None,
            }
            records.append(record)
            mark = "OK " if record["accepted"] else "-- "
            detail = (
                f"attempt {loc.accepted_attempt}, verify {accepted.verify_score}"
                if accepted
                else (first.reject if first else "no answer")
            )
            iou = (
                f", iou {record['detector_iou']:.2f}"
                if record["detector_iou"] is not None
                else ""
            )
            print(f"  {mark} {label:<26} {detail}{iou}  ({elapsed:.1f}s)")
        print()

    summary = _summarise(records, time.monotonic() - started)
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


def _summarise(records: list[dict], elapsed: float) -> dict:
    """Per-criterion and overall rates — the four numbers the plan asks for."""

    def block(rows: list[dict]) -> dict:
        valid_first = [r["attempt_1_valid"] for r in rows]
        verified = [s for r in rows for s in r["verify_scores"] if s is not None]
        accepted = [r for r in rows if r["accepted"]]
        ious = [r["detector_iou"] for r in accepted if r["detector_iou"] is not None]
        return {
            "n": len(rows),
            "attempt_1_valid_rate": round(_rate(valid_first), 3),
            "verify_pass_rate": round(
                _rate([s >= LLM_BBOX_VERIFY_PASS for s in verified]), 3
            ),
            "accepted_rate": round(_rate([r["accepted"] for r in rows]), 3),
            "mean_attempts_to_accept": (
                round(statistics.fmean(r["accepted_attempt"] for r in accepted), 2)
                if accepted else None
            ),
            "mean_calls": round(statistics.fmean(r["calls"] for r in rows), 2) if rows else None,
            "mean_detector_iou": round(statistics.fmean(ious), 3) if ious else None,
            "iou_at_least_0_5_rate": round(_rate([i >= 0.5 for i in ious]), 3) if ious else None,
        }

    names = sorted({r["criterion"] for r in records})
    return {
        "overall": block(records),
        "per_criterion": {
            name: block([r for r in records if r["criterion"] == name])
            for name in names
        },
        "elapsed_seconds": round(elapsed, 1),
        "config": {
            "model": VISION_LLM_MODEL,
            "verify_pass": LLM_BBOX_VERIFY_PASS,
            "min_area": LLM_BBOX_MIN_AREA,
            "max_area": LLM_BBOX_MAX_AREA,
            "detector": detector_client.status(),
        },
    }


def _row(name: str, row: dict) -> str:
    """One table line. Deliberately no nested f-strings — the container runs
    Python 3.11, where an f-string cannot reuse its own quote character."""
    iou = row["mean_detector_iou"]
    iou_text = "n/a" if iou is None else format(iou, ".2f")
    attempts = row["mean_attempts_to_accept"] or 0
    return (
        f"{name[:27]:<28}{row['n']:>4}{row['attempt_1_valid_rate']:>10.0%}"
        f"{row['verify_pass_rate']:>9.0%}{row['accepted_rate']:>10.0%}"
        f"{attempts:>10.2f}{iou_text:>8}"
    )


def _print_summary(summary: dict) -> None:
    header = (
        f"{'criterion':<28}{'n':>4}{'a1 valid':>10}{'verify':>9}"
        f"{'accepted':>10}{'attempts':>10}{'iou':>8}"
    )
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for name, row in summary["per_criterion"].items():
        print(_row(name, row))
    print("-" * len(header))
    row = summary["overall"]
    print(_row("OVERALL", row))
    print("=" * len(header))
    print(f"{row['mean_calls']} LLM calls per criterion on average, "
          f"{summary['elapsed_seconds']}s total.")
    if row["attempt_1_valid_rate"] < 0.5:
        print(
            "\nAttempt-1 validity is under 50%: most first answers are not usable "
            "boxes. Per the regions plan § 3.3, prefer the open-vocabulary "
            "detector (regions.detector) as the primary source and keep "
            "regions.llm_boxes as the fallback for labels it cannot name."
        )
    if row["mean_detector_iou"] is not None and row["mean_detector_iou"] < 0.5:
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
        "--attempts", type=int, default=LLM_BBOX_MAX_ATTEMPTS,
        help=f"loop attempts per criterion (default: {LLM_BBOX_MAX_ATTEMPTS})",
    )
    parser.add_argument("--out", default=None, help="write the full JSON report here")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
