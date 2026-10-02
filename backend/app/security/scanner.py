"""
Malware scanning.

ClamAV (clamdscan or clamscan) is used when installed. Without it,
a built-in heuristic scanner runs on the raw bytes. The heuristic
scanner is a DEMO IMPLEMENTATION; signature-based scanning
(ClamAV or an equivalent service) is PRODUCTION REQUIRED, and the
security dashboard reports which engine is active.

Heuristics (on bytes, no parsing):
    EICAR test signature
    embedded executables (PE, ELF, Mach-O) or archives
    script / HTML markup inside images
    PDF active-content tripwire (long, unambiguous action names only)

The authoritative PDF active-content check is structural and runs in
the isolated worker (security/pdf_active_content.py), before this
scan. The byte-level tripwire here is defence in depth only; it
deliberately omits:
    /JS            three bytes: matches random compressed stream data
                   (roughly 1 in 5 multi-megabyte scans)
    /EmbeddedFile  attachments are judged structurally in the worker
                   (inert C2PA provenance manifests are allowed there
                   and stripped by sanitisation; everything else is
                   rejected)
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from ..config import settings


EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

_EMBEDDED_EXECUTABLE = [
    (re.compile(rb"This program cannot be run in DOS mode"), "EMBEDDED_PE"),
    (re.compile(rb"\x7fELF[\x01\x02][\x01\x02]\x01"), "EMBEDDED_ELF"),
    # Multi-byte headers, so random compressed data rarely matches.
    (re.compile(rb"\xcf\xfa\xed\xfe(?:\x07|\x0c)\x00\x00\x01"), "EMBEDDED_MACHO"),
    (re.compile(rb"PK\x03\x04[\x0a\x14\x2d]\x00[\x00-\x0f][\x00\x08]"), "EMBEDDED_ARCHIVE"),
]

_IMAGE_SCRIPT = re.compile(rb"<\s*(script|html|iframe|svg|\?php)\b", re.IGNORECASE)

PDF_ACTIVE_CONTENT = [
    b"/JavaScript", b"/Launch", b"/RichMedia", b"/XFA",
    b"/SubmitForm", b"/ImportData", b"/GoToE",
]

_PDF_ACTIVE = re.compile(
    rb"/(JavaScript|Launch|RichMedia|XFA|SubmitForm|ImportData|GoToE)\b"
)


@dataclass
class ScanResult:
    clean: bool
    engine: str
    code: Optional[str] = None


def clamav_binary() -> Optional[str]:
    if settings.clamav_binary:
        return settings.clamav_binary

    return shutil.which("clamdscan") or shutil.which("clamscan")


def engine_name() -> str:
    binary = clamav_binary()

    return f"clamav:{binary.rsplit('/', 1)[-1]}" if binary else "heuristic"


def _heuristic(content: bytes, content_type: str) -> ScanResult:
    if EICAR in content:
        return ScanResult(False, "heuristic", "EICAR_TEST_SIGNATURE")

    for pattern, code in _EMBEDDED_EXECUTABLE:
        if pattern.search(content):
            return ScanResult(False, "heuristic", code)

    if content_type.startswith("image/") and _IMAGE_SCRIPT.search(content):
        return ScanResult(False, "heuristic", "SCRIPT_IN_IMAGE")

    if content_type == "application/pdf" and _PDF_ACTIVE.search(content):
        return ScanResult(False, "heuristic", "PDF_ACTIVE_CONTENT")

    return ScanResult(True, "heuristic")


def scan(content: bytes, content_type: str) -> ScanResult:
    heuristic = _heuristic(content, content_type)

    if not heuristic.clean:
        return heuristic

    binary = clamav_binary()

    if not binary:
        return heuristic

    try:
        completed = subprocess.run(
            [binary, "--no-summary", "-"],
            input=content,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        # Fail closed: a scanner error never lets a file through.
        return ScanResult(False, engine_name(), "SCANNER_ERROR")

    if completed.returncode == 0:
        return ScanResult(True, engine_name())

    if completed.returncode == 1:
        return ScanResult(False, engine_name(), "MALWARE_SIGNATURE")

    return ScanResult(False, engine_name(), "SCANNER_ERROR")
