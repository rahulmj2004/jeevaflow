from pathlib import Path

import pytesseract
from PIL import Image


SUPPORTED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
}


def ocr_image(image: Image.Image) -> str:
    """
    Run Tesseract OCR on an in-memory image.
    """

    # Convert to RGB for reliable OCR.
    return pytesseract.image_to_string(
        image.convert("RGB")
    ).strip()


def extract_image_text(file_path: str) -> str:
    """
    Extract text from a medical document image
    using Tesseract OCR.
    """

    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(
            f"Image not found: {file_path}"
        )

    with Image.open(path) as image:
        return ocr_image(image)
