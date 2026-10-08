"""OpenCV detector results are structured: numbers in ``detail``, words in ``reason``.

Every detector returns the ``cv.result`` shape — ``detail`` carries
``detector`` / ``metric`` / ``value`` / ``measurements`` / ``thresholds`` /
``parameters`` (and ``state`` where the outcome is categorical), ``reason``
carries the sentence ``detail`` used to be. What is pinned here:

  * the common keys, on every registered detector, and that ``value`` is
    ``measurements[metric]`` — the one pair a consumer can rely on without
    knowing which detector ran;
  * each detector's own measurements on a synthetic image with a known
    answer, so a number that is merely present but wrong is caught;
  * everything is plain JSON (no numpy scalars leaking into a job result);
  * through ``/assess``, the result carries ``detail.image`` — the working
    frame the ``*_px`` figures are in — and the sentence in ``reason``.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_cv_measurements.py -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import config
from config import (
    BLUR_FULL_SCORE_MULTIPLE,
    BLUR_THRESHOLD,
    CV_FACE_PASS_MIN_COUNT,
    CV_VEGETATION_MARGINAL_FROM,
    CV_VEGETATION_PASS_ABOVE,
    DETAIL_FLOAT_DECIMALS,
    EXPOSURE_HIGH,
    EXPOSURE_LOW,
)
from cv import REGISTRY
from cv.features import detect_faces, detect_sky, detect_text, detect_vegetation, detect_water
from cv.quality import check_blur, check_exposure
from cv.result import cv_result

COMMON = {"detector", "metric", "value", "measurements", "thresholds", "parameters"}


def _flat(value=128, w=400, h=300):
    return np.full((h, w, 3), value, dtype=np.uint8)


def _noise(w=400, h=300, seed=7):
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


def _stripes(w=400, h=300, period=4):
    """Fine black/white vertical stripes: dense edges everywhere."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for x in range(0, w, period):
        img[:, x:x + period // 2] = 255
    return img


def _bgr(b, g, r, w=400, h=300):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (b, g, r)
    return img


def _assert_shape(result: dict, detector: str, metric: str) -> dict:
    assert result["method"] == "cv"
    assert isinstance(result["reason"], str) and result["reason"]
    assert isinstance(result["score"], int) and 1 <= result["score"] <= 10
    assert result["verdict"] in ("PASS", "MARGINAL", "FAIL")
    detail = result["detail"]
    assert COMMON <= set(detail)
    assert detail["detector"] == detector and detail["metric"] == metric
    assert detail["value"] == detail["measurements"][metric]
    json.dumps(result.get("regions", []))
    json.dumps(detail)  # plain JSON — no numpy scalars
    return detail


# ---------------------------------------------------------------------------
# Every registered detector has the common shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", sorted(set(REGISTRY.values()), key=lambda f: f.__name__))
def test_every_registered_detector_is_structured(fn):
    result = fn(_noise())
    detail = result["detail"]
    assert COMMON <= set(detail), fn.__name__
    assert detail["detector"] == fn.__name__
    assert detail["value"] == detail["measurements"][detail["metric"]]
    assert isinstance(result["reason"], str)
    json.dumps(detail)


# ---------------------------------------------------------------------------
# The declared specs (GET /cv-detectors) match what the detectors return
# ---------------------------------------------------------------------------

# Images chosen to drive each detector down different branches, so a key or
# state that only appears on one branch is still checked.
_SPEC_IMAGES = [
    _flat(5), _flat(128), _flat(250), _noise(), _stripes(),
    _bgr(40, 160, 40), _bgr(200, 150, 30), _bgr(230, 180, 120),
]


@pytest.mark.parametrize("fn", sorted(set(REGISTRY.values()), key=lambda f: f.__name__))
def test_declared_spec_matches_real_output(fn):
    """Every key and state a detector emits is declared, and every declared
    measurement / threshold / parameter key is emitted — so the endpoint that
    serves the spec cannot drift from the code."""
    from cv.result import spec_of

    spec = spec_of(fn)
    assert spec is not None, f"{fn.__name__} declares no spec"
    seen_states = set()
    for image in _SPEC_IMAGES:
        detail = fn(image)["detail"]
        assert detail["metric"] == spec.metric
        assert set(detail["measurements"]) == set(spec.measurements), fn.__name__
        assert set(detail["thresholds"]) == set(spec.thresholds), fn.__name__
        assert set(detail["parameters"]) == set(spec.parameters), fn.__name__
        if "state" in detail:
            assert detail["state"] in spec.states, (fn.__name__, detail["state"])
            seen_states.add(detail["state"])
        else:
            assert not spec.states, f"{fn.__name__} declares states but returned none"
    # A detector with states must have hit at least one of them here.
    assert not spec.states or seen_states


def test_exposure_hits_all_three_declared_states():
    from cv.result import spec_of

    seen = {check_exposure(img)["detail"]["state"] for img in (_flat(5), _flat(128), _flat(250))}
    assert seen == set(spec_of(check_exposure).states)


def test_units_are_from_the_documented_vocabulary():
    from cv.result import spec_of

    allowed = {"ratio", "px", "count", "variance", "intensity"}
    for fn in set(REGISTRY.values()):
        for key, m in spec_of(fn).measurements.items():
            assert m.unit in allowed, (fn.__name__, key, m.unit)
            assert m.description


def test_cv_detectors_endpoint_lists_measurements_and_states(client):
    body = client.get("/cv-detectors").json()
    by_fn = {d["function"]: d for d in body["detectors"]}
    assert set(by_fn) == {fn.__name__ for fn in REGISTRY.values()}
    for d in by_fn.values():
        assert {"names", "technique", "metric", "measurements", "thresholds",
                "parameters", "states", "regions"} <= set(d)
        assert d["metric"] in d["measurements"]
        for m in d["measurements"].values():
            assert set(m) == {"unit", "description"}
    assert by_fn["check_exposure"]["states"].keys() == {"underexposed", "normal", "overexposed"}
    assert set(by_fn["detect_faces"]["states"]) == {"high_confidence", "low_confidence",
                                                     "none", "unavailable"}
    assert by_fn["check_blur"]["states"] == {} and by_fn["check_blur"]["regions"] is None
    assert by_fn["detect_water"]["measurements"]["rejected_textured_count"]["unit"] == "count"
    assert "has pool" in by_fn["detect_water"]["names"]
    assert body["total_names"] == len(REGISTRY)


def test_a_metric_that_was_not_measured_is_a_bug():
    with pytest.raises(KeyError):
        cv_result(detector="x", score=1, verdict="FAIL", confidence=0, reason="r",
                  metric="missing", measurements={"present": 1})


def test_numpy_scalars_and_floats_are_normalised():
    r = cv_result(detector="x", score=np.int64(5), verdict="MARGINAL", confidence=np.int32(60),
                  reason="r", metric="m",
                  measurements={"m": np.float64(0.123456789), "n": np.int64(3)},
                  parameters={"range": (np.int64(1), 2.123456789)})
    places = DETAIL_FLOAT_DECIMALS
    assert r["detail"]["measurements"] == {"m": round(0.123456789, places), "n": 3}
    assert r["detail"]["parameters"] == {"range": [1, round(2.123456789, places)]}
    assert type(r["score"]) is int and type(r["confidence"]) is int


# ---------------------------------------------------------------------------
# Each detector's measurements mean what they say
# ---------------------------------------------------------------------------


def test_blur_flat_fails_and_noise_passes():
    flat = _assert_shape(check_blur(_flat()), "check_blur", "laplacian_variance")
    assert flat["value"] == 0.0
    assert flat["thresholds"] == {"pass_at_or_above": BLUR_THRESHOLD,
                                  "full_score_at": BLUR_THRESHOLD * BLUR_FULL_SCORE_MULTIPLE}
    sharp = check_blur(_noise())
    assert sharp["verdict"] == "PASS"
    assert sharp["detail"]["value"] >= BLUR_THRESHOLD
    assert "Laplacian variance" in sharp["reason"]


@pytest.mark.parametrize(
    "value, state, verdict",
    [(5, "underexposed", "FAIL"), (250, "overexposed", "FAIL"), (128, "normal", "PASS")],
)
def test_exposure_states(value, state, verdict):
    result = check_exposure(_flat(value))
    detail = _assert_shape(result, "check_exposure", "mean_intensity")
    assert detail["state"] == state and result["verdict"] == verdict
    assert detail["value"] == float(value)
    assert detail["thresholds"] == {"normal_min": EXPOSURE_LOW, "normal_max": EXPOSURE_HIGH}


def test_vegetation_counts_its_green_pixels():
    green = _assert_shape(detect_vegetation(_bgr(40, 160, 40)), "detect_vegetation", "green_ratio")
    m = green["measurements"]
    assert m["green_ratio"] == 1.0 and m["green_px"] == m["total_px"] == 400 * 300
    assert green["thresholds"] == {"pass_above": CV_VEGETATION_PASS_ABOVE,
                                   "marginal_from": CV_VEGETATION_MARGINAL_FROM}
    assert {"hsv_lower", "hsv_upper", "morph_kernel_px"} <= set(green["parameters"])
    grey = detect_vegetation(_flat(128))["detail"]["measurements"]
    assert grey["green_ratio"] == 0.0 and grey["green_px"] == 0


def test_sky_measures_the_top_band_only():
    img = _flat(40)
    band = int(300 * 0.35)
    img[:band] = (230, 180, 120)  # a clear-blue top band, in BGR
    detail = _assert_shape(detect_sky(img), "detect_sky", "sky_ratio")
    m = detail["measurements"]
    assert m["analysed_px"] == int(300 * detail["parameters"]["top_fraction"]) * 400
    assert m["sky_px"] <= m["analysed_px"]
    assert m["sky_ratio"] == pytest.approx(m["sky_px"] / m["analysed_px"], abs=1e-4)
    assert m["blue_px"] > 0


def test_faces_none_reports_both_passes():
    result = detect_faces(_flat(128))
    detail = _assert_shape(result, "detect_faces", "faces_count")
    assert detail["state"] == "none" and result["verdict"] == "FAIL"
    assert detail["measurements"] == {"faces_count": 0, "faces_high_count": 0,
                                      "faces_low_count": 0}
    assert {"scale_factor", "min_neighbors_high", "min_neighbors_low",
            "min_size_px"} <= set(detail["parameters"])


def test_water_accounts_for_every_blue_blob():
    img = _flat(40)
    img[100:250, 50:350] = (200, 150, 30)  # a flat blue/teal pool, in BGR
    detail = _assert_shape(detect_water(img), "detect_water", "water_ratio")
    m = detail["measurements"]
    assert m["candidates_count"] >= 1
    assert m["flat_count"] + m["rejected_textured_count"] == m["candidates_count"]
    assert m["water_ratio"] == pytest.approx(m["water_px"] / m["total_px"], abs=1e-4)
    assert m["water_px"] > 0
    assert detail["parameters"]["max_texture_variance"] > 0


def test_text_counts_dense_blocks():
    detail = _assert_shape(detect_text(_stripes()), "detect_text", "dense_block_ratio")
    m = detail["measurements"]
    assert m["dense_blocks_count"] <= m["total_blocks_count"]
    assert m["dense_block_ratio"] == pytest.approx(
        m["dense_blocks_count"] / m["total_blocks_count"], abs=1e-4
    )
    assert m["dense_blocks_count"] > 0 and m["text_regions_count"] >= 1
    assert detect_text(_flat())["detail"]["measurements"]["dense_blocks_count"] == 0


# ---------------------------------------------------------------------------
# Through /assess: the frame is attached, the sentence is the reason
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(need_postgres):
    import main

    with TestClient(main.app) as c:
        yield c


def test_assess_result_carries_structured_cv_detail(client):
    img = np.full((1500, 2000, 3), 128, dtype=np.uint8)  # bigger than the working size
    png = cv2.imencode(".png", img)[1].tobytes()
    r = client.post(
        "/assess",
        files={"file": ("grey.png", png, "image/png")},
        data={"criteria": json.dumps([{"name": "sharpness", "type": "cv"},
                                      {"name": "exposure", "type": "cv"}])},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    for _ in range(200):
        job = client.get(f"/jobs/{job_id}").json()
        if job["phase"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert job["phase"] == "completed", job
    per = job["result"]["assessment"]["per_criterion_scores"]

    sharp = per["sharpness"]
    assert sharp["detail"]["detector"] == "check_blur"
    assert sharp["detail"]["metric"] == "laplacian_variance"
    assert "Laplacian variance" in sharp["reason"]
    image = sharp["detail"]["image"]
    assert image["frame"] == "working"
    assert max(image["width"], image["height"]) <= 1000       # the working image
    assert image["working_scale"] == pytest.approx(image["width"] / 2000, abs=1e-3)

    assert per["exposure"]["detail"]["state"] == "normal"
    # Every per-item entry carries the same structured detail.
    assert per["exposure"]["items"][0]["detail"]["metric"] == "mean_intensity"


# ---------------------------------------------------------------------------
# The reported thresholds ARE the ones the verdict uses
# ---------------------------------------------------------------------------
#
# The cut points live once, in config.py, and both the scoring branch and the
# reported detail.thresholds read them. These tests pin that: every detector
# reports config's values, and a coverage detector's verdict flips exactly
# where its reported threshold says.

_CUTS = {
    "detect_vegetation": ("CV_VEGETATION_PASS_ABOVE", "CV_VEGETATION_MARGINAL_FROM"),
    "detect_sky": ("CV_SKY_PASS_ABOVE", "CV_SKY_MARGINAL_FROM"),
    "detect_water": ("CV_WATER_PASS_ABOVE", "CV_WATER_MARGINAL_FROM"),
    "detect_text": ("CV_TEXT_PASS_ABOVE", "CV_TEXT_MARGINAL_FROM"),
}


@pytest.mark.parametrize("fn", [detect_vegetation, detect_sky, detect_water, detect_text],
                         ids=lambda f: f.__name__)
def test_coverage_detectors_report_configs_cut_points(fn):
    pass_name, marginal_name = _CUTS[fn.__name__]
    assert fn(_noise())["detail"]["thresholds"] == {
        "pass_above": getattr(config, pass_name),
        "marginal_from": getattr(config, marginal_name),
    }


def test_faces_and_blur_report_configs_values():
    assert detect_faces(_flat())["detail"]["thresholds"] == {
        "pass_at_or_above": CV_FACE_PASS_MIN_COUNT
    }
    assert check_blur(_flat())["detail"]["thresholds"]["full_score_at"] == (
        BLUR_THRESHOLD * BLUR_FULL_SCORE_MULTIPLE
    )


def _green_rows(rows, w=400, h=300):
    """A grey page whose top ``rows`` rows are solid green: green_ratio = rows / h."""
    img = _flat(128, w, h)
    img[:rows] = (40, 160, 40)
    return img


@pytest.mark.parametrize("cut, above, below", [
    (CV_VEGETATION_PASS_ABOVE, "PASS", "MARGINAL"),
    (CV_VEGETATION_MARGINAL_FROM, "MARGINAL", "FAIL"),
])
def test_vegetation_verdict_flips_at_the_reported_threshold(cut, above, below):
    h = 300
    rows_above = int(cut * h) + 2
    rows_below = int(cut * h) - 2
    hi = detect_vegetation(_green_rows(rows_above, h=h))
    lo = detect_vegetation(_green_rows(rows_below, h=h))
    assert hi["detail"]["value"] > cut > lo["detail"]["value"]
    assert hi["verdict"] == above and lo["verdict"] == below
