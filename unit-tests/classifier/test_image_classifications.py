#!/usr/bin/env python3
"""
Image-classification unit tests.

One pytest function per field type from field_criteria.json. Each function
submits the field's criteria against a known-good image and a known-bad
image and asserts the classifier's overall verdict matches expectation.

Expected on-disk layout::

    unit-tests/classifier/
      field_criteria.json
      test_image_classifications.py     <-- this file
      images/
        _shared_fail.jpg                <-- one obviously-wrong photo
        safetyForm/pass.jpg
        exclusionZones/pass.jpg
        ...
        <fieldName>/pass.jpg            <-- one per field
        <fieldName>/pass (1).jpg        <-- additional pass photos, evaluated separately
        <fieldName>/pass (2).png
        <fieldName>/fail.jpg            <-- optional per-field override

A field with no pass images (empty folder) is `pytest.skip`ped. A field
with multiple pass images (`pass.jpg`, `pass (1).jpg`, ...) is evaluated
against every image individually; each image's full weighted breakdown is
printed, then the test asserts that every one produced `PASS`.

The fail image resolution order is:
    1. images/<fieldName>/fail.jpg   (manual override if you have one)
    2. images/_shared_fail.jpg  degraded to be blurry + dark
       so both CV quality criteria and LLM content criteria fail.

Run::

    pytest unit-tests/classifier/test_image_classifications.py
    pytest unit-tests/classifier/test_image_classifications.py -k safetyForm -s

The `-s` flag surfaces the full weighted-score breakdown printed by each
test, which is useful when tuning weights or debugging a failure.

Env / CLI:
    CLASSIFIER_BASE_URL  base URL, default http://192.168.5.233:4001
    CLASSIFIER_API_KEY   API key, else read from ../../.env
"""

from __future__ import annotations

import atexit
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import pytest

try:
    import requests
except ImportError:
    pytest.skip("requests is required: pip install requests", allow_module_level=True)


SCRIPT_DIR = Path(__file__).parent
IMAGES_DIR = SCRIPT_DIR / "images"
CRITERIA_FILE = SCRIPT_DIR / "field_criteria.json"
RESULTS_FILE = SCRIPT_DIR / "test_results.txt"

FIELD_CRITERIA: dict = json.loads(CRITERIA_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tee logging — everything printed via _log() goes to stdout AND
# test_results.txt so the file survives pytest's stdout capture.
# ---------------------------------------------------------------------------

_results_fh = None


def _log(line: str = "") -> None:
    global _results_fh
    print(line)
    if _results_fh is None:
        _results_fh = RESULTS_FILE.open("w", encoding="utf-8")
        atexit.register(_results_fh.close)
    _results_fh.write(line + "\n")
    _results_fh.flush()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _load_api_key_from_env_file() -> Optional[str]:
    env_file = (SCRIPT_DIR / "../../.env").resolve()
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("DEFAULT_LITELLM_MASTER_KEY="):
            value = line.split("=", 1)[1].strip()
            if " #" in value:
                value = value[: value.index(" #")].strip()
            return value
    return None


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("CLASSIFIER_BASE_URL", "http://192.168.5.240:4001")


@pytest.fixture(scope="session")
def api_key() -> str:
    key = os.environ.get("CLASSIFIER_API_KEY") or _load_api_key_from_env_file()
    if not key:
        pytest.skip(
            "no API key: set CLASSIFIER_API_KEY or DEFAULT_LITELLM_MASTER_KEY in .env"
        )
    return key


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _submit_job(base_url: str, api_key: str, image_path: Path, criteria: list) -> str:
    url = f"{base_url}/v1/classifier/assess"
    with image_path.open("rb") as fh:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"image": (image_path.name, fh, "image/jpeg")},
            data={"criteria": json.dumps(criteria)},
            timeout=30,
        )
    if not resp.ok:
        raise RuntimeError(f"submit failed ({resp.status_code}): {resp.text}")
    return resp.json()["job_id"]


def _poll_job(
    base_url: str,
    api_key: str,
    job_id: str,
    max_wait: int = 300,
    poll_interval: int = 3,
) -> dict:
    url = f"{base_url}/v1/classifier/jobs/{job_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    elapsed = 0
    status = "pending"
    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval
        resp = requests.get(url, headers=headers, timeout=10)
        if not resp.ok:
            raise RuntimeError(f"poll failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        status = data["status"]
        if status in ("completed", "failed"):
            return data
    raise TimeoutError(
        f"job {job_id} did not finish within {max_wait}s (last status: {status})"
    )


def _assess(base_url: str, api_key: str, image_path: Path, criteria: list) -> dict:
    job_id = _submit_job(base_url, api_key, image_path, criteria)
    job = _poll_job(base_url, api_key, job_id)
    if job["status"] != "completed":
        raise RuntimeError(f"job failed: {job.get('error', 'unknown error')}")
    return job.get("result", {})


# ---------------------------------------------------------------------------
# Image resolution
# ---------------------------------------------------------------------------

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_PASS_STEM_RE = re.compile(r"^pass(?:\s*\(\d+\)|\s*\d+)?$", re.IGNORECASE)


def _pass_images(field_name: str) -> list[Path]:
    """
    All pass images for a field: pass.<ext>, pass (N).<ext>, passN.<ext>.
    Sorted so pass.jpg comes first, then pass (1), pass (2), ...
    """
    folder = IMAGES_DIR / field_name
    if not folder.is_dir():
        return []

    def _sort_key(p: Path) -> tuple:
        m = re.search(r"\d+", p.stem)
        return (0 if m is None else 1, int(m.group()) if m else 0, p.name.lower())

    matches = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in _IMAGE_EXTS
        and _PASS_STEM_RE.match(p.stem)
    ]
    return sorted(matches, key=_sort_key)


def _find_shared_fail_source() -> Optional[Path]:
    """Locate images/_shared_fail.<ext> in any supported extension."""
    if not IMAGES_DIR.is_dir():
        return None
    for p in sorted(IMAGES_DIR.iterdir()):
        if p.is_file() and p.stem == "_shared_fail" and p.suffix.lower() in _IMAGE_EXTS:
            return p
    return None


def _degraded_shared_fail_image() -> Path:
    """
    Return a path to a copy of _shared_fail.<ext> that has been blurred and
    darkened so it fails CV sharpness/exposure and LLM content criteria.

    Cached next to the source so successive tests reuse it instead of
    re-degrading every call.
    """
    source = _find_shared_fail_source()
    if source is None:
        pytest.skip(
            f"no shared fail image at {IMAGES_DIR}/_shared_fail.<jpg|png|...> — "
            "drop one obviously-wrong photo there to enable fail-case tests"
        )

    cached = IMAGES_DIR / "_shared_fail.degraded.jpg"
    if cached.exists() and cached.stat().st_mtime >= source.stat().st_mtime:
        return cached

    try:
        from PIL import Image, ImageFilter
    except ImportError:
        # No Pillow — fall back to the raw shared image. CV criteria may still
        # pass on it, but LLM content criteria will still fail overall.
        return source

    img = Image.open(source).convert("RGB")
    img = img.filter(ImageFilter.GaussianBlur(radius=8))
    img = Image.eval(img, lambda px: int(px * 0.25))  # underexpose
    img.save(cached, format="JPEG", quality=85)
    return cached


def _fail_image(field_name: str) -> Path:
    manual = IMAGES_DIR / field_name / "fail.jpg"
    if manual.exists():
        return manual
    return _degraded_shared_fail_image()


# ---------------------------------------------------------------------------
# Reporting + assertion
# ---------------------------------------------------------------------------


def _print_breakdown(label: str, field: str, image_path: Path, result: dict) -> None:
    display = FIELD_CRITERIA[field].get("displayName", field)
    assessment = result.get("assessment", {})
    per = assessment.get("per_criterion_scores", {})
    breakdown = assessment.get("weighted_score_breakdown")

    _log(f"\n{'=' * 70}")
    _log(f"  {label}  |  field: {display}  |  image: {image_path.name}")
    _log(f"{'=' * 70}")
    _log(
        f"  Overall verdict: {result.get('verdict', '?')} "
        f"(score {assessment.get('overall_score', '-')})"
    )

    if per:
        _log("  Per-criterion:")
        for name, val in per.items():
            if not isinstance(val, dict):
                continue
            v = val.get("verdict", "?")
            s = val.get("score", "?")
            m = val.get("method", "?")
            _log(f"    {name:<55} {v:<9} score={s:<3}  [{m}]")

    if breakdown:
        ws = breakdown.get("weighted_sum")
        tw = breakdown.get("total_weight")
        fs = breakdown.get("final_score")
        _log(f"  Weighted: sum={ws}  total_weight={tw}  final={fs}")


def _run_field(base_url: str, api_key: str, field: str) -> None:
    criteria = FIELD_CRITERIA[field]["classifierCriteria"]

    pass_imgs = _pass_images(field)
    if not pass_imgs:
        pytest.skip(
            f"no pass image(s) at {IMAGES_DIR / field}/ — "
            "drop pass.jpg or pass (N).<ext> to enable this test"
        )

    # Evaluate every pass image; keep each result so the assertion at the
    # end reports which specific image(s) failed rather than short-circuiting.
    pass_results: list[tuple[Path, dict]] = []
    total = len(pass_imgs)
    for i, img in enumerate(pass_imgs, start=1):
        result = _assess(base_url, api_key, img, criteria)
        _print_breakdown(f"PASS IMAGE {i}/{total}", field, img, result)
        pass_results.append((img, result))

    fail_img = _fail_image(field)
    fail_result = _assess(base_url, api_key, fail_img, criteria)
    _print_breakdown("FAIL IMAGE", field, fail_img, fail_result)
    fail_verdict = fail_result.get("verdict")

    pass_failures = [
        (img, r.get("verdict")) for img, r in pass_results if r.get("verdict") != "PASS"
    ]
    assert not pass_failures, (
        f"[{field}] {len(pass_failures)}/{total} pass image(s) did not PASS: "
        + ", ".join(f"{img.name}={verdict}" for img, verdict in pass_failures)
    )
    assert fail_verdict in (
        "FAIL",
        "MARGINAL",
    ), f"[{field}] expected FAIL/MARGINAL on {fail_img.name}, got {fail_verdict}"


# ---------------------------------------------------------------------------
# One test per field
# ---------------------------------------------------------------------------
# Ordered to match the source config for readability. Each test is thin so
# the file structure mirrors the classification config one-to-one and it's
# easy to jump to a field with `pytest -k <fieldName>`.


def test_safetyForm(base_url, api_key):
    _run_field(base_url, api_key, "safetyForm")


def test_exclusionZones(base_url, api_key):
    _run_field(base_url, api_key, "exclusionZones")


def test_ladderSetup(base_url, api_key):
    _run_field(base_url, api_key, "ladderSetup")


def test_roofAnchor(base_url, api_key):
    _run_field(base_url, api_key, "roofAnchor")


def test_crewHarnessed(base_url, api_key):
    _run_field(base_url, api_key, "crewHarnessed")


def test_ladderAnchorPatches(base_url, api_key):
    _run_field(base_url, api_key, "ladderAnchorPatches")


def test_frontOfHouseWithAddress(base_url, api_key):
    _run_field(base_url, api_key, "frontOfHouseWithAddress")


def test_moduleManufacturerLabel(base_url, api_key):
    _run_field(base_url, api_key, "moduleManufacturerLabel")


def test_moduleSerialNumber(base_url, api_key):
    _run_field(base_url, api_key, "moduleSerialNumber")


def test_mlpeManufactureLabel(base_url, api_key):
    _run_field(base_url, api_key, "mlpeManufactureLabel")


def test_mlpeSerialNumber(base_url, api_key):
    _run_field(base_url, api_key, "mlpeSerialNumber")


def test_gatewaySerialNumber(base_url, api_key):
    _run_field(base_url, api_key, "gatewaySerialNumber")


def test_railAttachment(base_url, api_key):
    _run_field(base_url, api_key, "railAttachment")


def test_railEnd(base_url, api_key):
    _run_field(base_url, api_key, "railEnd")


def test_railSplice(base_url, api_key):
    _run_field(base_url, api_key, "railSplice")


def test_mlpeAttachment(base_url, api_key):
    _run_field(base_url, api_key, "mlpeAttachment")


def test_midclampsEndclamps(base_url, api_key):
    _run_field(base_url, api_key, "midclampsEndclamps")


def test_preExistingSiteDamageDocumentation(base_url, api_key):
    _run_field(base_url, api_key, "preExistingSiteDamageDocumentation")


def test_engineeringApproval(base_url, api_key):
    _run_field(base_url, api_key, "engineeringApproval")


def test_deviationForm(base_url, api_key):
    _run_field(base_url, api_key, "deviationForm")


def test_teslaMciLocationAndPhoto(base_url, api_key):
    _run_field(base_url, api_key, "teslaMciLocationAndPhoto")


def test_arrayMap(base_url, api_key):
    _run_field(base_url, api_key, "arrayMap")


def test_roofInstallationCloseup(base_url, api_key):
    _run_field(base_url, api_key, "roofInstallationCloseup")


def test_mlpeAttachedToPanelOrRail(base_url, api_key):
    _run_field(base_url, api_key, "mlpeAttachedToPanelOrRail")


def test_arrayPanelReady(base_url, api_key):
    _run_field(base_url, api_key, "arrayPanelReady")


def test_egcRuns(base_url, api_key):
    _run_field(base_url, api_key, "egcRuns")


def test_arraySoladeckPhotos(base_url, api_key):
    _run_field(base_url, api_key, "arraySoladeckPhotos")


def test_arrayPanelInstall(base_url, api_key):
    _run_field(base_url, api_key, "arrayPanelInstall")


def test_tiltMeasurement(base_url, api_key):
    _run_field(base_url, api_key, "tiltMeasurement")


def test_junctionBoxPhotos(base_url, api_key):
    _run_field(base_url, api_key, "junctionBoxPhotos")


def test_rooftopConduitRun(base_url, api_key):
    _run_field(base_url, api_key, "rooftopConduitRun")


def test_atticRuns(base_url, api_key):
    _run_field(base_url, api_key, "atticRuns")


def test_atticPhotoUnderEachArray(base_url, api_key):
    _run_field(base_url, api_key, "atticPhotoUnderEachArray")


def test_gatewayPhotos(base_url, api_key):
    _run_field(base_url, api_key, "gatewayPhotos")


def test_splitSystemCombiner(base_url, api_key):
    _run_field(base_url, api_key, "splitSystemCombiner")


def test_productionMeterPhotos(base_url, api_key):
    _run_field(base_url, api_key, "productionMeterPhotos")


def test_secondaryDisconnectPhotos(base_url, api_key):
    _run_field(base_url, api_key, "secondaryDisconnectPhotos")


def test_primaryACDisconnectInteriorBreakerEnclosure(base_url, api_key):
    _run_field(base_url, api_key, "primaryACDisconnectInteriorBreakerEnclosure")


def test_primaryACDisconnect(base_url, api_key):
    _run_field(base_url, api_key, "primaryACDisconnect")


def test_poiPhotos(base_url, api_key):
    _run_field(base_url, api_key, "poiPhotos")


def test_consumptionCts(base_url, api_key):
    _run_field(base_url, api_key, "consumptionCts")


def test_subPanelPhoto(base_url, api_key):
    _run_field(base_url, api_key, "subPanelPhoto")


def test_mspPhotos(base_url, api_key):
    _run_field(base_url, api_key, "mspPhotos")


def test_gesGroundingMethod(base_url, api_key):
    _run_field(base_url, api_key, "gesGroundingMethod")


def test_meterLabeledPhoto(base_url, api_key):
    _run_field(base_url, api_key, "meterLabeledPhoto")


def test_homerunConduitRun(base_url, api_key):
    _run_field(base_url, api_key, "homerunConduitRun")


def test_bosPullback(base_url, api_key):
    _run_field(base_url, api_key, "bosPullback")


def test_enphaseSystemController(base_url, api_key):
    _run_field(base_url, api_key, "enphaseSystemController")


def test_enphaseLoadController(base_url, api_key):
    _run_field(base_url, api_key, "enphaseLoadController")


def test_batteryCountPhotos(base_url, api_key):
    _run_field(base_url, api_key, "batteryCountPhotos")


def test_batteryCommsCable(base_url, api_key):
    _run_field(base_url, api_key, "batteryCommsCable")


def test_batteryCt(base_url, api_key):
    _run_field(base_url, api_key, "batteryCt")


def test_criticalLoadPanelPhotos(base_url, api_key):
    _run_field(base_url, api_key, "criticalLoadPanelPhotos")


def test_genericCommissioningPhotos(base_url, api_key):
    _run_field(base_url, api_key, "genericCommissioningPhotos")


def test_mpuTrenchPhotos(base_url, api_key):
    _run_field(base_url, api_key, "mpuTrenchPhotos")


def test_mpuTreeRemovalPhoto(base_url, api_key):
    _run_field(base_url, api_key, "mpuTreeRemovalPhoto")


def test_overhead(base_url, api_key):
    _run_field(base_url, api_key, "overhead")


def test_postInstallRoofView(base_url, api_key):
    _run_field(base_url, api_key, "postInstallRoofView")


def test_paperworkOnSite(base_url, api_key):
    _run_field(base_url, api_key, "paperworkOnSite")


def test_miscellaneousPhotos(base_url, api_key):
    _run_field(base_url, api_key, "miscellaneousPhotos")
