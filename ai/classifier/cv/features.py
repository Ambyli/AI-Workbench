"""Feature detectors: is X in this page, and where.

Five "is it there?" detectors, each of which localises what it scored:

    detect_vegetation — HSV green masking
    detect_sky        — upper-region blue/grey analysis
    detect_faces      — OpenCV Haar cascade (frontal faces)
    detect_water      — blue/teal hue + flat-texture validation
    detect_text       — Sobel edge density per block, merged into columns

Every one of them emits ``regions`` built from the SAME mask or boxes the
score was measured from, so the overlay and the number can never describe
different things — a blue car excluded from ``detect_water``'s score is
excluded from its regions too.

Coordinates are in the WORKING image the detector was handed;
``analysis.cv_eval`` rescales them into original page pixels.

Process flow position: registered in ``cv/__init__.py``'s REGISTRY and run by
``analysis.cv_eval._run_cv_criterion``.
"""

import cv2
import numpy as np

from config import (
    CV_FACE_MIN_NEIGHBORS_HIGH,
    CV_FACE_MIN_NEIGHBORS_LOW,
    CV_FACE_MIN_SIZE,
    CV_FACE_SCALE_FACTOR,
    CV_REGION_MAX_PER_DETECTOR,
    CV_REGION_MIN_AREA_FRAC,
    CV_REGION_POLY_EPSILON_FRAC,
    CV_SKY_BLUE_HSV_LOWER,
    CV_SKY_BLUE_HSV_UPPER,
    CV_SKY_GREY_HSV_LOWER,
    CV_SKY_GREY_HSV_UPPER,
    CV_SKY_TOP_FRACTION,
    CV_TEXT_BLOCK_DENSITY,
    CV_TEXT_BLOCK_SIZE,
    CV_TEXT_EDGE_THRESHOLD,
    CV_TEXT_MERGE_KERNEL,
    CV_TEXT_MIN_BLOCKS,
    CV_VEGETATION_HSV_LOWER,
    CV_VEGETATION_HSV_UPPER,
    CV_VEGETATION_MORPH_KERNEL,
    CV_WATER_HSV_LOWER,
    CV_WATER_HSV_UPPER,
    CV_WATER_MAX_TEXTURE_VARIANCE,
    CV_WATER_MIN_CONTOUR_AREA_PX,
)
from cv.regions import _box_region, _mask_regions
from cv.result import DetectorSpec, Measurement, cv_result, describes
from logger import logger

# The coverage detectors share their threshold vocabulary.
_COVERAGE_THRESHOLDS = {
    "pass_above": "PASS when the ratio is above this",
    "marginal_from": "MARGINAL from this ratio up to pass_above; FAIL below it",
}

# Measurement parameters (hue ranges, cascade settings, block sizes, area and
# texture floors) live in config.py § CV detectors. The scoring curves below —
# how a coverage ratio becomes 1-10 — are each detector's definition and stay
# next to the docstring that explains them.


@describes(DetectorSpec(
    technique="HSV green masking",
    metric="green_ratio",
    measurements={
        "green_ratio": Measurement("ratio", "Share of the page's pixels inside the green mask"),
        "green_px": Measurement("px", "Pixels inside the green mask"),
        "total_px": Measurement("px", "Pixels in the page"),
    },
    thresholds=_COVERAGE_THRESHOLDS,
    parameters=("hsv_lower", "hsv_upper", "morph_kernel_px"),
    regions="polygons of the green mask",
))
def detect_vegetation(image) -> dict:
    """Detect green vegetation (trees, grass, shrubs) via HSV color masking.

    Converts to HSV and masks the typical green hue range (H 35-85).
    The coverage ratio of green pixels drives the score.

    Reliable for outdoor daylight images; may under-detect in poor lighting
    or over-detect artificial green objects (painted surfaces, signs).

    Score mapping:
        >15% green coverage → PASS  (score 7-10, scaled by coverage)
         5-15%              → MARGINAL (score 4-6)
        <5%                → FAIL  (score 1-3)
    """
    logger.debug("detect_vegetation: image shape=%s", image.shape)

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_green = np.array(CV_VEGETATION_HSV_LOWER)
    upper_green = np.array(CV_VEGETATION_HSV_UPPER)
    mask = cv2.inRange(hsv, lower_green, upper_green)

    kernel = np.ones((CV_VEGETATION_MORPH_KERNEL, CV_VEGETATION_MORPH_KERNEL), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    total_pixels = image.shape[0] * image.shape[1]
    green_pixels = cv2.countNonZero(mask)
    ratio = green_pixels / total_pixels

    if ratio > 0.15:
        score = min(10, int(7 + (ratio - 0.15) / 0.05))
        verdict, confidence = "PASS", min(95, int(60 + ratio * 150))
    elif ratio >= 0.05:
        score = int(4 + (ratio - 0.05) / 0.01)
        verdict, confidence = "MARGINAL", 65
    else:
        score = max(1, int(ratio / 0.05 * 3))
        verdict, confidence = "FAIL", 75

    result = cv_result(
        detector="detect_vegetation",
        score=score, verdict=verdict, confidence=confidence,
        reason=f"Green coverage: {ratio:.1%} of image ({green_pixels:,} px)",
        metric="green_ratio",
        measurements={
            "green_ratio": ratio,
            "green_px": green_pixels,
            "total_px": total_pixels,
        },
        thresholds={"pass_above": 0.15, "marginal_from": 0.05},
        parameters={
            "hsv_lower": CV_VEGETATION_HSV_LOWER,
            "hsv_upper": CV_VEGETATION_HSV_UPPER,
            "morph_kernel_px": CV_VEGETATION_MORPH_KERNEL,
        },
        # The same mask the ratio was measured from, as polygons.
        regions=_mask_regions(mask, image.shape),
    )
    logger.debug("detect_vegetation: returning score=%s verdict=%s ratio=%.3f regions=%d",
                 score, verdict, ratio, len(result["regions"]))
    return result


@describes(DetectorSpec(
    technique="Upper-region blue/grey analysis",
    metric="sky_ratio",
    measurements={
        "sky_ratio": Measurement("ratio", "Share of the analysed top band that is sky-coloured"),
        "sky_px": Measurement("px", "Sky-coloured pixels in the band (blue or grey)"),
        "blue_px": Measurement("px", "Pixels matching the clear-blue range"),
        "grey_px": Measurement("px", "Pixels matching the overcast-grey range"),
        "analysed_px": Measurement("px", "Pixels in the top band (top_fraction of the page)"),
    },
    thresholds=_COVERAGE_THRESHOLDS,
    parameters=("top_fraction", "blue_hsv_lower", "blue_hsv_upper",
                "grey_hsv_lower", "grey_hsv_upper"),
    regions="polygons of the sky mask in the top band",
))
def detect_sky(image) -> dict:
    """Detect sky in the upper portion of the image via blue/grey HSV analysis.

    Analyses the top 35% of the image for sky-like hues:
      - Clear blue sky:  H 100-130, moderate-high S, high V
      - Overcast/cloudy: any H, low S (<60), high V (>150)

    Score mapping:
        >60% sky coverage in upper region → PASS  (score 7-10)
        30-60%                            → MARGINAL (score 4-6)
        <30%                              → FAIL  (score 1-3)
    """
    logger.debug("detect_sky: image shape=%s", image.shape)

    h, w = image.shape[:2]
    sky_region = image[:int(h * CV_SKY_TOP_FRACTION), :]
    hsv = cv2.cvtColor(sky_region, cv2.COLOR_BGR2HSV)

    blue_mask = cv2.inRange(
        hsv, np.array(CV_SKY_BLUE_HSV_LOWER), np.array(CV_SKY_BLUE_HSV_UPPER)
    )
    grey_mask = cv2.inRange(
        hsv, np.array(CV_SKY_GREY_HSV_LOWER), np.array(CV_SKY_GREY_HSV_UPPER)
    )
    sky_mask = cv2.bitwise_or(blue_mask, grey_mask)

    total = sky_region.shape[0] * sky_region.shape[1]
    sky_pixels = cv2.countNonZero(sky_mask)
    ratio = sky_pixels / total

    if ratio > 0.60:
        score = min(10, int(7 + (ratio - 0.60) / 0.10))
        verdict, confidence = "PASS", min(90, int(70 + ratio * 20))
    elif ratio >= 0.30:
        score = int(4 + (ratio - 0.30) / 0.10)
        verdict, confidence = "MARGINAL", 65
    else:
        score = max(1, int(ratio / 0.30 * 3))
        verdict, confidence = "FAIL", 70

    result = cv_result(
        detector="detect_sky",
        score=score, verdict=verdict, confidence=confidence,
        reason=(
            f"Sky coverage (upper {CV_SKY_TOP_FRACTION:.0%}): {ratio:.1%} "
            f"({sky_pixels:,} px)"
        ),
        metric="sky_ratio",
        measurements={
            # Of the analysed band, not the whole page.
            "sky_ratio": ratio,
            "sky_px": sky_pixels,
            "blue_px": cv2.countNonZero(blue_mask),
            "grey_px": cv2.countNonZero(grey_mask),
            "analysed_px": total,
        },
        thresholds={"pass_above": 0.60, "marginal_from": 0.30},
        parameters={
            "top_fraction": CV_SKY_TOP_FRACTION,
            "blue_hsv_lower": CV_SKY_BLUE_HSV_LOWER,
            "blue_hsv_upper": CV_SKY_BLUE_HSV_UPPER,
            "grey_hsv_lower": CV_SKY_GREY_HSV_LOWER,
            "grey_hsv_upper": CV_SKY_GREY_HSV_UPPER,
        },
        # The mask covers only the top CV_SKY_TOP_FRACTION of the page, so the contour
        # coordinates are already page-relative — no offset needed — but the
        # area floor is measured against that crop, not the whole image.
        regions=_mask_regions(sky_mask, sky_region.shape),
    )
    logger.debug("detect_sky: returning score=%s verdict=%s ratio=%.3f regions=%d",
                 score, verdict, ratio, len(result["regions"]))
    return result


@describes(DetectorSpec(
    technique="OpenCV Haar cascade",
    metric="faces_count",
    measurements={
        "faces_count": Measurement(
            "count", "Faces the score is based on: the strict pass's, else the loose pass's"
        ),
        "faces_high_count": Measurement(
            "count", "Faces found by the strict pass (min_neighbors_high); null when unavailable"
        ),
        "faces_low_count": Measurement(
            "count", "Faces found by the loose pass (min_neighbors_low); null when it did not run"
        ),
    },
    thresholds={"pass_at_or_above": "PASS at or above this many strict-pass faces"},
    parameters=("scale_factor", "min_neighbors_high", "min_neighbors_low", "min_size_px"),
    states={
        "high_confidence": "the strict pass found at least one face (PASS)",
        "low_confidence": "only the loose pass found a face (MARGINAL)",
        "none": "neither pass found a face (FAIL)",
        "unavailable": "the Haar cascade file is missing (FAIL, confidence 0)",
    },
    regions="one box per detected face",
))
def detect_faces(image) -> dict:
    """Detect frontal human faces using OpenCV's Haar cascade classifier.

    Uses the built-in haarcascade_frontalface_default.xml which ships with
    cv2.  No external model download is required.

    Score mapping:
        ≥1 face (minNeighbors=5, high confidence) → PASS     (score 10)
        1 face  (minNeighbors=3, lower confidence) → MARGINAL (score 5)
        0 faces                                    → FAIL     (score 1)
    """
    logger.debug("detect_faces: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(cascade_path)

    parameters = {
        "scale_factor": CV_FACE_SCALE_FACTOR,
        "min_neighbors_high": CV_FACE_MIN_NEIGHBORS_HIGH,
        "min_neighbors_low": CV_FACE_MIN_NEIGHBORS_LOW,
        "min_size_px": list(CV_FACE_MIN_SIZE),
    }
    if face_cascade.empty():
        logger.error("detect_faces: Haar cascade file not found at %s", cascade_path)
        return cv_result(
            detector="detect_faces",
            score=1, verdict="FAIL", confidence=0,
            reason="Haar cascade file not found — face detection unavailable",
            metric="faces_count",
            measurements={"faces_count": 0, "faces_high_count": None, "faces_low_count": None},
            parameters=parameters,
            state="unavailable",
            regions=[],
        )

    faces_high = face_cascade.detectMultiScale(
        gray,
        scaleFactor=CV_FACE_SCALE_FACTOR,
        minNeighbors=CV_FACE_MIN_NEIGHBORS_HIGH,
        minSize=CV_FACE_MIN_SIZE,
    )
    n_high = len(faces_high) if not isinstance(faces_high, tuple) else 0

    # The boxes that produced the score become the regions. The second,
    # looser pass only runs when the strict one found nothing, so the regions
    # always describe the detections the reported score is based on.
    boxes = faces_high if n_high >= 1 else ()
    # None, not 0, when the loose pass never ran: "not looked" and "looked and
    # found none" are different facts.
    n_low = None
    if n_high >= 1:
        score, verdict, confidence = 10, "PASS", 85
        state, reason = "high_confidence", f"{n_high} face(s) detected (high confidence)"
    else:
        faces_low = face_cascade.detectMultiScale(
            gray,
            scaleFactor=CV_FACE_SCALE_FACTOR,
            minNeighbors=CV_FACE_MIN_NEIGHBORS_LOW,
            minSize=CV_FACE_MIN_SIZE,
        )
        n_low = len(faces_low) if not isinstance(faces_low, tuple) else 0
        if n_low >= 1:
            score, verdict, confidence = 5, "MARGINAL", 55
            state = "low_confidence"
            reason = f"{n_low} possible face(s) detected (lower confidence)"
            boxes = faces_low
        else:
            score, verdict, confidence = 1, "FAIL", 80
            state, reason = "none", "No faces detected"

    regions = [
        _box_region(x, y, w, h, confidence="high" if n_high >= 1 else "low")
        for x, y, w, h in list(boxes)[:CV_REGION_MAX_PER_DETECTOR]
    ]
    result = cv_result(
        detector="detect_faces",
        score=score, verdict=verdict, confidence=confidence,
        reason=reason,
        metric="faces_count",
        measurements={
            # The faces the score is based on: the strict pass's, else the loose one's.
            "faces_count": n_high if n_high >= 1 else (n_low or 0),
            "faces_high_count": n_high,
            "faces_low_count": n_low,
        },
        thresholds={"pass_at_or_above": 1},
        parameters=parameters,
        state=state,
        regions=regions,
    )
    logger.debug("detect_faces: returning score=%s verdict=%s regions=%d",
                 score, verdict, len(regions))
    return result


@describes(DetectorSpec(
    technique="Blue/teal hue + flat-texture",
    metric="water_ratio",
    measurements={
        "water_ratio": Measurement("ratio", "Share of the page in blue blobs that passed the flat-texture test"),
        "water_px": Measurement("px", "Area of those qualifying blobs"),
        "total_px": Measurement("px", "Pixels in the page"),
        "blue_blobs_count": Measurement("count", "Blue/teal blobs found, of any size"),
        "candidates_count": Measurement("count", "Blobs large enough to test (min_contour_area_px)"),
        "flat_count": Measurement("count", "Candidates flat enough to be water"),
        "rejected_textured_count": Measurement(
            "count", "Candidates rejected as too textured — a blue car, a shirt"
        ),
    },
    thresholds=_COVERAGE_THRESHOLDS,
    parameters=("hsv_lower", "hsv_upper", "min_contour_area_px", "max_texture_variance"),
    regions="polygons of the qualifying (flat) blobs",
))
def detect_water(image) -> dict:
    """Detect water or pools via blue/teal color masking + flat-texture validation.

    A blue region qualifies as water only if its Laplacian variance is below
    200 (flat, non-textured surface), rejecting blue cars, clothing, or signs.

    Score mapping:
        >15% qualifying blue+flat coverage → PASS  (score 7-10)
        5-15%                              → MARGINAL (score 4-6)
        <5%                                → FAIL  (score 1-3)
    """
    logger.debug("detect_water: image shape=%s", image.shape)

    hsv  = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    blue_mask = cv2.inRange(
        hsv, np.array(CV_WATER_HSV_LOWER), np.array(CV_WATER_HSV_UPPER)
    )
    contours, _ = cv2.findContours(blue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    total_pixels = image.shape[0] * image.shape[1]
    min_region_area = total_pixels * CV_REGION_MIN_AREA_FRAC

    qualifying_pixels = 0
    qualifying: list[tuple[float, dict]] = []
    # How many blue blobs were big enough to test, and how many the
    # flat-texture test threw out (a blue car, a shirt) — the part of the
    # decision a bare coverage figure hides.
    candidates = flat = 0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < CV_WATER_MIN_CONTOUR_AREA_PX:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        roi = gray[y:y + h, x:x + w]
        if roi.size == 0:
            continue
        candidates += 1
        if cv2.Laplacian(roi, cv2.CV_64F).var() < CV_WATER_MAX_TEXTURE_VARIANCE:
            flat += 1
            qualifying_pixels += area
            # Only the contours that passed the flat-texture test are
            # regions — a blue car is excluded from the score, so it must be
            # excluded from the overlay too.
            if area >= min_region_area:
                epsilon = CV_REGION_POLY_EPSILON_FRAC * cv2.arcLength(contour, True)
                simplified = cv2.approxPolyDP(contour, epsilon, True)
                points = [[float(p[0][0]), float(p[0][1])] for p in simplified]
                if len(points) >= 3:
                    qualifying.append(
                        (
                            float(area),
                            {
                                "kind": "polygon",
                                "points": points,
                                "score": round(area / (total_pixels or 1), 5),
                                "attrs": {"area_px": int(area), "flat": True},
                            },
                        )
                    )

    qualifying.sort(key=lambda item: -item[0])
    regions = [region for _, region in qualifying[:CV_REGION_MAX_PER_DETECTOR]]
    ratio = qualifying_pixels / total_pixels

    if ratio > 0.15:
        score = min(10, int(7 + (ratio - 0.15) / 0.05))
        verdict, confidence = "PASS", min(85, int(65 + ratio * 100))
    elif ratio >= 0.05:
        score = int(4 + (ratio - 0.05) / 0.033)
        verdict, confidence = "MARGINAL", 60
    else:
        score = max(1, int(ratio / 0.05 * 3))
        verdict, confidence = "FAIL", 70

    result = cv_result(
        detector="detect_water",
        score=score, verdict=verdict, confidence=confidence,
        reason=f"Qualifying water coverage: {ratio:.1%} ({int(qualifying_pixels):,} px)",
        metric="water_ratio",
        measurements={
            "water_ratio": ratio,
            "water_px": int(qualifying_pixels),
            "total_px": total_pixels,
            "blue_blobs_count": len(contours),
            "candidates_count": candidates,
            "flat_count": flat,
            "rejected_textured_count": candidates - flat,
        },
        thresholds={"pass_above": 0.15, "marginal_from": 0.05},
        parameters={
            "hsv_lower": CV_WATER_HSV_LOWER,
            "hsv_upper": CV_WATER_HSV_UPPER,
            "min_contour_area_px": CV_WATER_MIN_CONTOUR_AREA_PX,
            "max_texture_variance": CV_WATER_MAX_TEXTURE_VARIANCE,
        },
        regions=regions,
    )
    logger.debug("detect_water: returning score=%s verdict=%s ratio=%.3f regions=%d",
                 score, verdict, ratio, len(regions))
    return result


@describes(DetectorSpec(
    technique="Sobel edge density per block",
    metric="dense_block_ratio",
    measurements={
        "dense_block_ratio": Measurement("ratio", "Share of blocks whose edge density is above block_density"),
        "dense_blocks_count": Measurement("count", "Blocks above block_density"),
        "total_blocks_count": Measurement("count", "Blocks the page was divided into"),
        "text_regions_count": Measurement("count", "Merged text regions returned as boxes"),
    },
    thresholds=_COVERAGE_THRESHOLDS,
    parameters=("block_px", "block_density", "edge_threshold", "min_blocks_per_region"),
    regions="boxes around merged runs of dense blocks — roughly one per paragraph or column",
))
def detect_text(image) -> dict:
    """Detect text regions via Sobel edge density analysis.

    Text produces characteristically high edge density in small localised
    blocks (due to letter strokes).  Divides the image into 32×32 blocks
    and counts those with >35% edge density.

    Score mapping:
        >10% of blocks high-density → PASS  (score 7-10)
        3-10%                       → MARGINAL (score 4-6)
        <3%                         → FAIL  (score 1-3)
    """
    logger.debug("detect_text: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    sy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    magnitude = np.sqrt(sx ** 2 + sy ** 2)
    _, edges = cv2.threshold(magnitude, CV_TEXT_EDGE_THRESHOLD, 255, cv2.THRESH_BINARY)

    block = CV_TEXT_BLOCK_SIZE
    h, w = edges.shape
    # A grid of the same block×block cells the score counts, so the regions and the
    # score can never describe different things. Adjacent hot cells are then
    # merged into rectangles by connected components — one box per paragraph
    # or column, rather than one per 32-px tile.
    rows = (h + block - 1) // block
    cols = (w + block - 1) // block
    grid = np.zeros((rows, cols), dtype=np.uint8)

    total_blocks, high_density_blocks = 0, 0
    for row in range(rows):
        for col in range(cols):
            y, x = row * block, col * block
            tile = edges[y:y + block, x:x + block]
            if tile.size == 0:
                continue
            total_blocks += 1
            if np.count_nonzero(tile) / tile.size > CV_TEXT_BLOCK_DENSITY:
                high_density_blocks += 1
                grid[row, col] = 255

    ratio = high_density_blocks / total_blocks if total_blocks > 0 else 0.0

    regions: list[dict] = []
    if high_density_blocks:
        # Close one-cell gaps (the space between two words) before merging so
        # a line of text is one box rather than a row of them.
        merged = cv2.morphologyEx(grid, cv2.MORPH_CLOSE, np.ones(CV_TEXT_MERGE_KERNEL, np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
        blobs = []
        for i in range(1, count):  # 0 is the background
            bx, by, bw, bh, cells = stats[i]
            if cells < CV_TEXT_MIN_BLOCKS:
                continue  # a stray high-contrast edge, not a block of text
            blobs.append((cells, bx, by, bw, bh))
        blobs.sort(key=lambda b: -b[0])
        regions = [
            _box_region(
                bx * block, by * block,
                min(bw * block, w - bx * block), min(bh * block, h - by * block),
                score=round(cells / (total_blocks or 1), 5), blocks=int(cells),
            )
            for cells, bx, by, bw, bh in blobs[:CV_REGION_MAX_PER_DETECTOR]
        ]

    if ratio > 0.10:
        score = min(10, int(7 + (ratio - 0.10) / 0.05))
        verdict, confidence = "PASS", min(85, int(65 + ratio * 150))
    elif ratio >= 0.03:
        score = int(4 + (ratio - 0.03) / 0.023)
        verdict, confidence = "MARGINAL", 60
    else:
        score = max(1, int(ratio / 0.03 * 3))
        verdict, confidence = "FAIL", 75

    result = cv_result(
        detector="detect_text",
        score=score, verdict=verdict, confidence=confidence,
        reason=(f"High-density edge blocks: {high_density_blocks}/{total_blocks} "
                f"({ratio:.1%})"),
        metric="dense_block_ratio",
        measurements={
            "dense_block_ratio": ratio,
            "dense_blocks_count": high_density_blocks,
            "total_blocks_count": total_blocks,
            "text_regions_count": len(regions),
        },
        thresholds={"pass_above": 0.10, "marginal_from": 0.03},
        parameters={
            "block_px": CV_TEXT_BLOCK_SIZE,
            "block_density": CV_TEXT_BLOCK_DENSITY,
            "edge_threshold": CV_TEXT_EDGE_THRESHOLD,
            "min_blocks_per_region": CV_TEXT_MIN_BLOCKS,
        },
        regions=regions,
    )
    logger.debug("detect_text: returning score=%s verdict=%s ratio=%.3f regions=%d",
                 score, verdict, ratio, len(regions))
    return result
