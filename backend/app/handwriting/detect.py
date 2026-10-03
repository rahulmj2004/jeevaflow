"""
Lightweight handwriting detector (OpenCV / numpy heuristics only).

Two signals, combined into a score in [0, 1]:

    OCR confidence   Tesseract reads print with high word confidence;
                     handwriting gives few words at low confidence.
    stroke width     printed glyphs have near-constant stroke width;
                     pen strokes vary (pressure, speed, angle).

This is a routing signal, not a classifier with known accuracy: a
HANDWRITTEN result only sends the document to doctor review, and an
unclear result does the same (see pipeline.process_document).
"""

import cv2
import numpy as np


HANDWRITTEN_THRESHOLD = 0.5
MIN_INK_RATIO = 0.002

# Calibrated on synthetic print and handwriting-style renders.
CONF_PRINT = 0.85
CONF_SPAN = 0.45
STROKE_CV_PRINT = 0.35
STROKE_CV_SPAN = 0.35


def _ink_mask(gray: np.ndarray) -> np.ndarray:
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return mask


def stroke_width_cv(mask: np.ndarray) -> float:
    """
    Coefficient of variation of stroke half-widths, sampled on the
    ridge (local maxima) of the distance transform.
    """

    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    ridge = (distance > 0) & (distance >= cv2.dilate(distance, np.ones((3, 3), np.uint8)))
    widths = distance[ridge]

    if widths.size < 50:
        return 0.0

    return float(widths.std() / widths.mean())


def detect(gray: np.ndarray, words: list[dict]) -> dict:
    """
    gray: 2-D uint8 page image. words: the worker's OCR words
    (each with "conf" in 0..1), possibly none: handwriting often gives
    Tesseract nothing at all, which counts as the lowest confidence.
    Returns {is_handwritten, score, has_ink}.
    """

    mask = _ink_mask(gray)

    if float((mask > 0).mean()) < MIN_INK_RATIO:
        # Blank page: nothing to call handwriting.
        return {"is_handwritten": False, "score": 0.0, "has_ink": False}

    confident = [word["conf"] for word in words if len(word.get("norm", "")) >= 2]
    mean_conf = sum(confident) / len(confident) if confident else 0.0

    conf_term = float(np.clip((CONF_PRINT - mean_conf) / CONF_SPAN, 0.0, 1.0))
    stroke_term = float(np.clip((stroke_width_cv(mask) - STROKE_CV_PRINT) / STROKE_CV_SPAN, 0.0, 1.0))

    score = round(0.6 * conf_term + 0.4 * stroke_term, 3)

    return {"is_handwritten": score >= HANDWRITTEN_THRESHOLD, "score": score, "has_ink": True}
