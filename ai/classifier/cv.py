import numpy as np

from config import BLUR_THRESHOLD, EXPOSURE_LOW, EXPOSURE_HIGH
from logger import logger


def check_blur(image) -> dict:
    import cv2
    logger.debug("check_blur: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    result = {
        "criterion": "sharpness",
        "score": min(10, int(10 * min(variance / (BLUR_THRESHOLD * 3), 1.0))),
        "verdict": "PASS" if variance >= BLUR_THRESHOLD else "FAIL",
        "confidence": 100,
        "detail": f"Laplacian variance: {variance:.1f} (threshold: {BLUR_THRESHOLD})",
    }
    logger.debug("check_blur: returning score=%s verdict=%s variance=%.1f",
                 result["score"], result["verdict"], variance)
    return result


def check_exposure(image) -> dict:
    import cv2
    logger.debug("check_exposure: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    mean = float(np.mean(gray))

    if mean < EXPOSURE_LOW:
        result = {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                  "detail": f"Underexposed (mean: {mean:.1f}, min: {EXPOSURE_LOW})"}
    elif mean > EXPOSURE_HIGH:
        result = {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                  "detail": f"Overexposed (mean: {mean:.1f}, max: {EXPOSURE_HIGH})"}
    else:
        score = int(1 + 9 * (mean - EXPOSURE_LOW) / (EXPOSURE_HIGH - EXPOSURE_LOW))
        result = {"criterion": "exposure", "score": score, "verdict": "PASS", "confidence": 100,
                  "detail": f"Normal exposure (mean: {mean:.1f})"}

    logger.debug("check_exposure: returning score=%s verdict=%s mean=%.1f",
                 result["score"], result["verdict"], mean)
    return result
