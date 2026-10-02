"""
First-line file validation (no parsing): size, declared type and
magic bytes. Structural validation and sanitisation happen in the
isolated worker; malware scanning in security/scanner.py.
"""

import hashlib
from typing import Optional

from .config import settings


ALLOWED_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}

CONTENT_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "application/x-pdf": "application/pdf",
}

class FileValidationError(Exception):
    """
    Raised when an incoming file cannot be accepted.

    code is one of:
        EMPTY_FILE | FILE_TOO_LARGE | UNSUPPORTED_TYPE |
        CONTENT_MISMATCH | CORRUPTED_FILE | SUSPICIOUS_PDF |
        ENCRYPTED_PDF | TOO_MANY_PAGES | INVALID_DIMENSIONS |
        DECOMPRESSION_BOMB | ANIMATED_IMAGE | MALWARE_DETECTED
    """

    def __init__(self, code: str, message: str, security_scan: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # Structured PDF active-content result, when the rejection
        # came from that scan.
        self.security_scan = security_scan


def normalize_content_type(content_type: Optional[str]) -> str:
    value = (content_type or "").split(";")[0].strip().lower()

    return CONTENT_TYPE_ALIASES.get(value, value)


def sniff_content_type(content: bytes) -> Optional[str]:
    if content.startswith(b"%PDF"):
        return "application/pdf"

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    return None


def validate_file(
    content: bytes,
    declared_content_type: Optional[str],
) -> str:
    """
    Validate size, declared type and actual file signature.

    Returns the verified content type.
    """

    if not content:
        raise FileValidationError(
            "EMPTY_FILE",
            "The file is empty.",
        )

    if len(content) > settings.max_upload_bytes:
        raise FileValidationError(
            "FILE_TOO_LARGE",
            "The file is too large.",
        )

    declared = normalize_content_type(declared_content_type)

    if declared not in ALLOWED_TYPES:
        raise FileValidationError(
            "UNSUPPORTED_TYPE",
            "Only PDF, JPG and PNG files are supported.",
        )

    actual = sniff_content_type(content)

    if actual != declared:
        raise FileValidationError(
            "CONTENT_MISMATCH",
            "The file content does not match its declared type.",
        )

    return actual


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
