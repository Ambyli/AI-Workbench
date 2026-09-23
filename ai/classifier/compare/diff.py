"""Change detection — what is different between a reference and a subject.

`/assess/compare` already answers "how similar are these two documents?" as a
number. With ``regions.diff`` it also answers *where*: the regions that
appeared, disappeared, or changed between a reference photo and the subject.

Deliberately classical CV, no model:

    align     ORB keypoints + a brute-force Hamming matcher + a RANSAC
              homography. Under CLASSIFIER_DIFF_MIN_INLIERS inliers the two
              photos were not taken from close enough to the same place for a
              pixel difference to mean anything — the result then says
              ``aligned: false``, carries a note, and returns NO regions.
              Different framing is REPORTED, never guessed at; drawing boxes
              around parallax is the failure mode this threshold exists for.
    difference  warp the reference onto the subject, blur both to grayscale
              (CLASSIFIER_DIFF_BLUR), absolute-difference them, adaptively
              threshold, morphologically close the speckle, and take contours
              over CLASSIFIER_DIFF_MIN_AREA.
    classify  compare local EDGE DENSITY inside each blob in the two images:
              structure in the subject only means something was ADDED,
              structure in the reference only means it was REMOVED, structure
              in both means it CHANGED. That is the one judgement here, and it
              is a ratio of two Laplacian variances, not an opinion.

**Diff regions are not criteria.** They are filed under a synthetic name
(``_diff:e0``) so they have somewhere to live in ``regions.json`` and a slug
for their layer file, and they never reach ``compute_weighted_score`` or the
similarity comparison. Nothing a diff finds can move a score.

**Coordinates.** ``detect_changes`` works on, and returns, coordinates in the
SUBJECT's working image — the same ≤1000-px frame every other detector uses.
``jobs.runners.run_compare`` rescales them into original page pixels through
the subject's PageGeometry, exactly like the CV detectors' output.

Everything here is blocking OpenCV work, so the caller runs it in a thread.

Process flow position: called by ``jobs.runners.run_compare`` after the
subject and its examples have been analysed, once per live single-image
example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from config import (
    DIFF_BLUR,
    DIFF_CHANGE_ADDED,
    DIFF_CHANGE_CHANGED,
    DIFF_CHANGE_REMOVED,
    DIFF_CLOSE_ITERATIONS,
    DIFF_CLOSE_KERNEL,
    DIFF_EDGE_RATIO,
    DIFF_FRAME_MARGIN_FRAC,
    DIFF_FRAME_MARGIN_MIN_PX,
    DIFF_LOWE_RATIO,
    DIFF_MAX_REGIONS,
    DIFF_MIN_AREA,
    DIFF_MIN_INLIERS,
    DIFF_OPEN_KERNEL,
    DIFF_ORB_FEATURES,
    DIFF_POLY_EPSILON_FRAC,
    DIFF_RANSAC_REPROJ_PX,
    DIFF_VALID_ERODE_KERNEL,
)
from logger import logger

# Every tunable — the change labels, the edge-energy ratio, the ORB budget,
# Lowe's ratio, the RANSAC tolerance, the frame margin and the mask kernels —
# lives in config.py § Change detection. Nothing numeric is defined here.


@dataclass
class DiffResult:
    """What change detection could say about one reference/subject pair.

    Attributes:
        aligned:    Whether the homography had enough inliers to be trusted.
                    False means the regions list is EMPTY on purpose.
        inliers:    RANSAC inliers behind the homography (0 when there was
                    none).
        homography: The 3×3 matrix as nested lists, or None. Kept because it
                    is the whole basis of the comparison and an operator
                    debugging a surprising diff wants to see it.
        regions:    ``[{kind, points, score, attrs}, ...]`` in the SUBJECT's
                    WORKING image coordinates — the caller rescales.
        note:       One caller-facing sentence, always set when something was
                    not done.
    """

    aligned: bool = False
    inliers: int = 0
    homography: Optional[list[list[float]]] = None
    regions: list[dict] = field(default_factory=list)
    note: Optional[str] = None

    def as_dict(self, max_regions: int = DIFF_MAX_REGIONS) -> dict[str, Any]:
        """JSON-safe view for ``example_results[i].diff``."""
        return {
            "aligned": self.aligned,
            "inliers": self.inliers,
            "homography": self.homography,
            "regions": self.regions[:max_regions],
            "regions_truncated": len(self.regions) > max_regions,
            "note": self.note,
        }


def detect_changes(
    reference_bgr: Any,
    subject_bgr: Any,
    *,
    min_inliers: int = DIFF_MIN_INLIERS,
    min_area: float = DIFF_MIN_AREA,
    blur: int = DIFF_BLUR,
    max_regions: int = DIFF_MAX_REGIONS,
) -> DiffResult:
    """Find what changed between a reference image and a subject image.

    Args:
        reference_bgr: The example/"before" image, BGR.
        subject_bgr:   The subject/"after" image, BGR. Its pixel frame is the
                       one the returned regions are expressed in.
        min_inliers:   RANSAC inliers required before the alignment is
                       trusted at all.
        min_area:      Contour area floor as a fraction of the subject image.
        blur:          Gaussian kernel (odd) applied before the difference.
        max_regions:   Cap on returned regions, largest first.

    Returns:
        A :class:`DiffResult`. It never raises for an unusable pair — a
        featureless image, a size mismatch, a degenerate homography all come
        back as ``aligned=False`` with a note, because a compare job must not
        fail because an enrichment could not run.
    """
    import cv2
    import numpy as np

    if reference_bgr is None or subject_bgr is None:
        return DiffResult(note="One of the two images had no pixels to compare.")
    if getattr(reference_bgr, "size", 0) == 0 or getattr(subject_bgr, "size", 0) == 0:
        return DiffResult(note="One of the two images was empty.")

    subject_gray = cv2.cvtColor(subject_bgr, cv2.COLOR_BGR2GRAY)
    reference_gray = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2GRAY)
    height, width = subject_gray.shape[:2]

    # Step 1 — align the reference onto the subject.
    homography, inliers, note = _homography(reference_gray, subject_gray, min_inliers)
    if homography is None:
        logger.info("detect_changes: not aligned (%d inlier(s)) — %s", inliers, note)
        return DiffResult(aligned=False, inliers=inliers, note=note)

    warped = cv2.warpPerspective(reference_gray, homography, (width, height))
    # Where the warp had no source pixels there is nothing to compare, and the
    # black border it leaves would otherwise read as one enormous change. The
    # same goes for a margin around the SUBJECT's own edge: two photos taken
    # from slightly different places simply do not overlap there, and a strip
    # of "change" hugging the frame is the commonest false positive in this
    # whole method.
    margin = max(
        DIFF_FRAME_MARGIN_MIN_PX,
        int(round(DIFF_FRAME_MARGIN_FRAC * min(height, width))),
    )
    valid = cv2.warpPerspective(
        np.full(reference_gray.shape, 255, dtype=np.uint8), homography, (width, height)
    )
    erode_kernel = np.ones((DIFF_VALID_ERODE_KERNEL, DIFF_VALID_ERODE_KERNEL), np.uint8)
    valid = cv2.erode(valid, erode_kernel, iterations=1)
    valid[:margin, :] = 0
    valid[-margin:, :] = 0
    valid[:, :margin] = 0
    valid[:, -margin:] = 0

    # Step 2 — difference the blurred grayscales.
    kernel = (blur | 1, blur | 1)
    delta = cv2.absdiff(
        cv2.GaussianBlur(warped, kernel, 0), cv2.GaussianBlur(subject_gray, kernel, 0)
    )
    delta = cv2.bitwise_and(delta, delta, mask=valid)

    # Adaptive rather than a fixed level: a re-shot photo differs globally in
    # exposure, and a fixed threshold either lights the whole frame or misses
    # a change in shadow. Otsu picks the level from this pair's own histogram.
    _, mask = cv2.threshold(delta, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        np.ones((DIFF_CLOSE_KERNEL, DIFF_CLOSE_KERNEL), np.uint8),
        iterations=DIFF_CLOSE_ITERATIONS,
    )
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN, np.ones((DIFF_OPEN_KERNEL, DIFF_OPEN_KERNEL), np.uint8)
    )

    # Step 3 — contours → regions, biggest first.
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    floor = max(1.0, min_area * width * height)
    regions: list[dict] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        area = cv2.contourArea(contour)
        if area < floor:
            break  # sorted, so everything after this is smaller too
        epsilon = DIFF_POLY_EPSILON_FRAC * cv2.arcLength(contour, True)
        points = [
            (float(p[0][0]), float(p[0][1]))
            for p in cv2.approxPolyDP(contour, epsilon, True)
        ]
        if len(points) < 3:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        blob = np.zeros((height, width), dtype=np.uint8)
        cv2.drawContours(blob, [contour], -1, 255, thickness=cv2.FILLED)
        change = _classify(warped, subject_gray, blob, (x, y, w, h))
        score = float(cv2.mean(delta, mask=blob)[0]) / 255.0
        regions.append(
            {
                "kind": "polygon",
                "points": points,
                "score": round(score, 4),
                "attrs": {
                    "change": change,
                    "area_px": int(area),
                    "bbox": [int(x), int(y), int(x + w), int(y + h)],
                },
            }
        )
        if len(regions) >= max_regions:
            break

    logger.info(
        "detect_changes: aligned on %d inlier(s) — %d region(s) %s",
        inliers,
        len(regions),
        {c: sum(1 for r in regions if r["attrs"]["change"] == c)
         for c in (DIFF_CHANGE_ADDED, DIFF_CHANGE_REMOVED, DIFF_CHANGE_CHANGED)},
    )
    return DiffResult(
        aligned=True,
        inliers=inliers,
        homography=[[float(v) for v in row] for row in homography],
        regions=regions,
        note=None if regions else (
            "The two images aligned and no change above the "
            f"{min_area:.1%} area floor was found."
        ),
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _homography(
    reference_gray: Any, subject_gray: Any, min_inliers: int
) -> tuple[Any, int, Optional[str]]:
    """ORB + RANSAC: the matrix that puts the reference on the subject.

    Returns ``(homography, inliers, note)``. A ``None`` homography always
    comes with a note written for a caller, not for a log — it ends up in
    ``example_results[i].diff.note``.
    """
    import cv2
    import numpy as np

    orb = cv2.ORB_create(nfeatures=DIFF_ORB_FEATURES)
    ref_kp, ref_desc = orb.detectAndCompute(reference_gray, None)
    sub_kp, sub_desc = orb.detectAndCompute(subject_gray, None)
    if ref_desc is None or sub_desc is None or len(ref_kp) < 4 or len(sub_kp) < 4:
        return None, 0, (
            "Not enough image features to align the two documents — one of "
            "them is nearly featureless (a flat colour, a blank page), so a "
            "pixel difference would be meaningless."
        )

    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    # kNN + Lowe's ratio rather than crossCheck: the ratio test is what drops
    # the matches a repeating texture (siding, shingles, a fence) produces,
    # and those are exactly the textures in the photos this runs on.
    pairs = matcher.knnMatch(ref_desc, sub_desc, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < DIFF_LOWE_RATIO * n.distance]
    if len(good) < 4:
        # Four is findHomography's own minimum. Below it there is nothing to
        # run RANSAC on, and the honest reading is that these are not two
        # pictures of the same thing.
        return None, len(good), (
            f"Only {len(good)} reliable feature match(es) between the two "
            "images — too few even to attempt an alignment. They are probably "
            "not two views of the same scene."
        )
    if len(good) < min_inliers:
        # Inliers are a subset of the matches, so this can only end one way.
        # Said with the same wording as the post-RANSAC check below, because
        # it is the same finding arrived at one step earlier.
        return None, len(good), _shortfall(len(good), min_inliers)

    src = np.float32([ref_kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([sub_kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    homography, mask = cv2.findHomography(src, dst, cv2.RANSAC, DIFF_RANSAC_REPROJ_PX)
    inliers = int(mask.sum()) if mask is not None else 0
    if homography is None:
        return None, inliers, (
            "The two images could not be aligned — no consistent geometric "
            "transform fits their matching features."
        )
    if inliers < min_inliers:
        return None, inliers, _shortfall(inliers, min_inliers)
    return homography, inliers, None


def _shortfall(found: int, required: int) -> str:
    """The "not enough agreement to trust this" sentence, said once."""
    return (
        f"The two images agreed on only {found} matching point(s), under the "
        f"{required} required (CLASSIFIER_DIFF_MIN_INLIERS). The camera moved "
        "too much for a pixel difference to mean anything, so no change "
        "regions were produced."
    )


def _classify(
    warped_reference: Any, subject_gray: Any, blob: Any, bounds: tuple[int, int, int, int]
) -> str:
    """added / removed / changed, from local edge density on each side.

    The question "did something appear here or disappear here?" is really
    "which of these two images has STRUCTURE in this patch". A Laplacian
    variance inside the blob answers it: an object has edges, the empty
    ground it stood on does not.

    Args:
        warped_reference: The reference, already warped into subject space.
        subject_gray:     The subject.
        blob:             uint8 mask of this change, subject-sized.
        bounds:           ``(x, y, w, h)`` of the blob, to avoid measuring the
                          whole page for a small patch.

    Returns:
        One of ``added`` / ``removed`` / ``changed``.
    """
    import cv2

    x, y, w, h = bounds
    sub_patch = subject_gray[y : y + h, x : x + w]
    ref_patch = warped_reference[y : y + h, x : x + w]
    mask_patch = blob[y : y + h, x : x + w]
    if sub_patch.size == 0 or ref_patch.size == 0:
        return DIFF_CHANGE_CHANGED

    sub_edges = _edge_energy(cv2, sub_patch, mask_patch)
    ref_edges = _edge_energy(cv2, ref_patch, mask_patch)
    if sub_edges > ref_edges * DIFF_EDGE_RATIO:
        return DIFF_CHANGE_ADDED
    if ref_edges > sub_edges * DIFF_EDGE_RATIO:
        return DIFF_CHANGE_REMOVED
    return DIFF_CHANGE_CHANGED


def _edge_energy(cv2, patch: Any, mask_patch: Any) -> float:
    """Mean absolute Laplacian inside the mask — "how much structure is here".

    The mean of |Laplacian| rather than its variance: variance over a masked
    region needs the mask's pixel count anyway, and the mean is the number
    that survives a blob being mostly background.
    """
    laplacian = cv2.convertScaleAbs(cv2.Laplacian(patch, cv2.CV_32F))
    return float(cv2.mean(laplacian, mask=mask_patch)[0])
