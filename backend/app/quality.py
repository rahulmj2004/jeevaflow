from pathlib import Path

import cv2
import numpy as np
from PIL import Image


MIN_WIDTH = 800
MIN_HEIGHT = 800

RETAKE_MIN_SIDE = 500

# Variance of the Laplacian (sharpness). A sharp but sparse printed
# report scores well above 100; a lightly blurred photo drops below
# 15 and a heavily blurred or blank image is close to 0.
RETAKE_SHARPNESS = 3.0
WARN_SHARPNESS = 15.0

RETAKE_DARK_MEAN = 40.0


def assess_image_quality(file_path: str):
    """
    Deterministic image quality gate.

    GOOD:
        Image is sufficiently large, sharp and bright.

    WARN:
        Image is usable but below preferred quality.
        Extraction proceeds; everything stays in REVIEW.

    RETAKE:
        Image cannot be read reliably. No extraction is attempted.
    """

    path = Path(file_path)

    try:
        with Image.open(path) as image:
            return assess_image(image)

    except Exception:
        return {
            "quality_status": "RETAKE",
            "quality_reason": (
                "Unable to read the image."
            ),
        }


def assess_image(image: Image.Image):
    """
    Quality gate on an already-open image (used by the isolated
    processing worker, which never writes the image to disk).
    """

    try:
        width, height = image.size
        grayscale = np.array(image.convert("L"))

    except Exception:
        return {
            "quality_status": "RETAKE",
            "quality_reason": (
                "Unable to read the image."
            ),
        }

    if width < RETAKE_MIN_SIDE or height < RETAKE_MIN_SIDE:
        return {
            "quality_status": "RETAKE",
            "quality_reason": (
                "Image resolution is too low "
                "for reliable medical document extraction."
            ),
        }

    brightness = float(grayscale.mean())

    if brightness < RETAKE_DARK_MEAN:
        return {
            "quality_status": "RETAKE",
            "quality_reason": (
                "Image is too dark to read reliably."
            ),
        }

    sharpness = float(
        cv2.Laplacian(grayscale, cv2.CV_64F).var()
    )

    if sharpness < RETAKE_SHARPNESS:
        return {
            "quality_status": "RETAKE",
            "quality_reason": (
                "Image is blurred or contains no readable "
                "detail."
            ),
        }

    reasons = []

    if width < MIN_WIDTH or height < MIN_HEIGHT:
        reasons.append(
            "Image resolution is below the "
            "preferred extraction quality."
        )

    if sharpness < WARN_SHARPNESS:
        reasons.append(
            "Image appears slightly blurred."
        )

    if reasons:
        return {
            "quality_status": "WARN",
            "quality_reason": " ".join(reasons),
        }

    return {
        "quality_status": "GOOD",
        "quality_reason": (
            "Image quality is acceptable."
        ),
    }
