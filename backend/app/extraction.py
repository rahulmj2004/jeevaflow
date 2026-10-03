"""
Deterministic, rule-based extraction.

Every item returned here carries the exact quote, page number and
character span it was read from, so provenance can verify it before
anything is stored.
"""

import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pymupdf
from PIL import Image

from .ocr import ocr_image


# ============================================================
# PDF PAGE EXTRACTION
# ============================================================

OCR_RENDER_DPI = 200


def extract_pdf_pages(file_path: str, ocr_fallback: bool = False):
    """
    Extract text page-by-page.

    When ocr_fallback is True, pages with no selectable text
    (scanned PDFs) are rendered and passed through OCR.

    Returns:
        [
            {
                "page_number": 1,
                "text": "...",
                "method": "TEXT" | "OCR",
            },
            ...
        ]
    """

    path = Path(file_path)

    document = pymupdf.open(path)

    pages = []

    try:
        for page_number, page in enumerate(document, start=1):
            text = page.get_text() or ""
            method = "TEXT"

            if ocr_fallback and not text.strip():
                pixmap = page.get_pixmap(dpi=OCR_RENDER_DPI)

                with Image.open(
                    io.BytesIO(pixmap.tobytes("png"))
                ) as image:
                    text = ocr_image(image)

                method = "OCR"

            pages.append(
                {
                    "page_number": page_number,
                    "text": text,
                    "method": method,
                }
            )

    finally:
        document.close()

    return pages


# ============================================================
# BACKWARD-COMPATIBLE FULL TEXT EXTRACTION
# ============================================================

def extract_pdf_text(file_path: str) -> str:
    """
    Extract the complete PDF text.

    Kept for compatibility with the existing application.
    """

    pages = extract_pdf_pages(file_path)

    return "\n".join(
        page["text"]
        for page in pages
        if page["text"].strip()
    )


# ============================================================
# OBSERVATION EXTRACTION
# ============================================================

# (observation_type, pattern, default unit, plausible range)
# The first capture group is the value. A plausibility range only
# rejects obvious misreads (e.g. OCR noise); it is not clinical logic.
OBSERVATION_PATTERNS = [
    (
        "HbA1c",
        # OCR often misreads the "1" in HbA1c ("HbAtc", "HbAt1c").
        # Only the label is tolerant; the value must match exactly and
        # the stored quote is the literal OCR text for the reviewer.
        r"\b(?:HbA[1Iil|t]{1,2}c|A1c)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*%?",
        "%",
        (2.0, 25.0),
    ),
    (
        "Glucose",
        r"\b(?:glucose|blood sugar)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:mg/dL)?",
        "mg/dL",
        (10.0, 1500.0),
    ),
    (
        "Creatinine",
        r"\bcreatinine\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:mg/dL)?",
        "mg/dL",
        (0.05, 30.0),
    ),
    (
        "LDL Cholesterol",
        r"\bLDL(?:\s*cholesterol)?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:mg/dL)?",
        "mg/dL",
        (5.0, 1000.0),
    ),
    (
        "Blood Pressure",
        r"\b(?:blood pressure|BP)\s*[:\-]?\s*(\d{2,3}\s*/\s*\d{2,3})\s*(?:mmHg)?",
        "mmHg",
        None,
    ),
]


def _plausible(value: str, valid_range) -> bool:
    if valid_range is None:
        return True

    try:
        number = float(value)
    except ValueError:
        return False

    low, high = valid_range

    return low <= number <= high


# Any other "Label: value unit" result line, so that follow-through
# is not limited to the analytes above. The value and unit are read
# exactly as printed (quote-anchored); the label is kept as written and
# never interpreted, and nothing is judged normal or abnormal.
_RESULT_UNITS = (
    r"mg/dL|mg/L|g/dL|g/L|mmol/L|µmol/L|umol/L|mIU/L|mIU/mL|µIU/mL|uIU/mL|IU/L|IU/mL|U/L|"
    r"ng/mL|ng/dL|pg/mL|µg/dL|ug/dL|mcg/dL|mEq/L|mL/min(?:/1\.73\s?m2)?|mg/g|mg/mmol|"
    r"mm/hr|mm/1st\s?hr|lakh/cumm|cells/cumm|million/cumm|/cumm|fL|%"
)
_RESULT_LINE = re.compile(
    r"^[ \t]*(?:[-*•]|\d+[.)])?[ \t]*"
    r"(?P<label>[A-Za-z][A-Za-z0-9 ()\-/,.'+]{1,48}?)[ \t]*[:\-]?[ \t]+"
    r"(?P<value>\d+(?:\.\d+)?)[ \t]*(?P<unit>" + _RESULT_UNITS + r")(?![A-Za-z])",
    re.MULTILINE,
)
_NOT_A_RESULT = re.compile(
    r"\b(?:date|age|reg|registration|phone|mobile|uhid|mrn|id|no|bill|amount|rs|weight|height|"
    r"page|ref|reference|range|time|bed|ward|tab|cap|inj|syp)\b",
    re.IGNORECASE,
)


def _result_lines(page: dict, taken: list[tuple[int, int]]) -> list[dict]:
    found = []

    for match in _RESULT_LINE.finditer(page["text"]):
        label = re.sub(r"\s+", " ", match.group("label")).strip(" -:")
        start, end = match.start("label"), match.end("unit")

        if len(label) < 2 or _NOT_A_RESULT.search(label):
            continue

        if any(start < taken_end and taken_start < end for taken_start, taken_end in taken):
            continue

        quote = page["text"][start:end]

        found.append({
            "observation_type": label[:100],
            "value": match.group("value"),
            "unit": match.group("unit"),
            "quote": quote,
            "page_number": page["page_number"],
            "start_position": start,
            "end_position": end,
        })

    return found


def extract_observations_from_pages(pages):
    """
    Extract observations while preserving the source page.

    Every extracted observation contains:
    - observation_type
    - value
    - unit
    - quote
    - page_number
    - start_position / end_position
    """

    observations = []

    for page in pages:

        page_number = page["page_number"]
        text = page["text"]

        for (
            observation_type,
            pattern,
            unit,
            valid_range,
        ) in OBSERVATION_PATTERNS:

            matches = re.finditer(
                pattern,
                text,
                re.IGNORECASE,
            )

            for match in matches:

                value = re.sub(r"\s+", "", match.group(1))

                if not _plausible(value, valid_range):
                    continue

                quote = match.group(0).rstrip()

                observations.append(
                    {
                        "observation_type": observation_type,
                        "value": value,
                        "unit": unit,
                        "quote": quote,
                        "page_number": page_number,
                        "start_position": match.start(),
                        "end_position": match.start() + len(quote),
                    }
                )

        taken = [
            (item["start_position"], item["end_position"])
            for item in observations
            if item["page_number"] == page_number
        ]
        observations.extend(_result_lines(page, taken))

    return observations


# ============================================================
# BACKWARD-COMPATIBLE OBSERVATION EXTRACTION
# ============================================================

def extract_observations(text: str):
    """
    Backward-compatible helper.

    Treats the supplied text as page 1.
    """

    pages = [
        {
            "page_number": 1,
            "text": text,
        }
    ]

    return extract_observations_from_pages(pages)


# ============================================================
# COMMITMENT / DOCTOR INSTRUCTION EXTRACTION
# ============================================================

# An instruction must start a line or sentence (optionally after a
# bullet/number) and begin with an instruction verb. Word boundaries
# stop "refer" matching "Reference range" and "review" matching
# "Reviewed by".
_LINE_START = r"(?:^|(?<=[.!?]\s))[ \t]*(?:[-*•]|\d+[.)])?[ \t]*"

COMMITMENT_PATTERNS = [
    r"repeat\b[^.\n]*",
    r"follow[\s-]?up\s+(?:with|in|after|on|at|visit)\b[^.\n]*",
    r"review\b[^.\n]*",
    r"refer(?:red)?\s+to\b[^.\n]*",
    r"(?:recheck|re-check)\b[^.\n]*",
    r"(?:check|monitor|screen\s+for|test\s+for)\b[^.\n]*",
]

_COMMITMENT_REGEX = re.compile(
    _LINE_START + r"(" + "|".join(COMMITMENT_PATTERNS) + r")",
    re.IGNORECASE | re.MULTILINE,
)


def extract_commitments_from_pages(
    pages,
    reference_date: Optional[date] = None,
):
    """
    Extract doctor instructions while preserving
    their source page and exact position.

    If the instruction states an interval ("after 3 months") and a
    reference date is known, a due date is derived from it.
    """

    commitments = []

    for page in pages:

        page_number = page["page_number"]
        text = page["text"]

        for match in _COMMITMENT_REGEX.finditer(text):

            instruction = match.group(1).strip()

            if len(instruction) < 8:
                continue

            start = match.start(1)

            commitments.append(
                {
                    "instruction": instruction,
                    "quote": instruction,
                    "page_number": page_number,
                    "start_position": start,
                    "end_position": start + len(instruction),
                    "due_date": derive_due_date(
                        instruction,
                        reference_date,
                    ),
                }
            )

    return commitments


# ============================================================
# BACKWARD-COMPATIBLE COMMITMENT EXTRACTION
# ============================================================

def extract_commitments(text: str):
    """
    Backward-compatible helper.

    Treats the supplied text as page 1.
    """

    pages = [
        {
            "page_number": 1,
            "text": text,
        }
    ]

    return extract_commitments_from_pages(pages)


# ============================================================
# DATES
# ============================================================

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}

_INTERVAL_REGEX = re.compile(
    r"\b(?:after|in|within)\s+(\d{1,3}|"
    + "|".join(_NUMBER_WORDS)
    + r")\s+(day|week|month|year)s?\b",
    re.IGNORECASE,
)


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1

    # Clamp to the last valid day of the target month.
    for day in (start.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue

    raise ValueError("Invalid date")


def derive_due_date(
    instruction: str,
    reference_date: Optional[date],
) -> Optional[date]:
    """
    Turn a stated interval into a due date.

    This is arithmetic on what the document says, not a
    clinical recommendation. No interval -> no due date.
    """

    if reference_date is None:
        return None

    match = _INTERVAL_REGEX.search(instruction)

    if match is None:
        return None

    raw_amount, unit = match.group(1).lower(), match.group(2).lower()

    amount = (
        int(raw_amount)
        if raw_amount.isdigit()
        else _NUMBER_WORDS[raw_amount]
    )

    if unit == "day":
        return reference_date + timedelta(days=amount)

    if unit == "week":
        return reference_date + timedelta(weeks=amount)

    if unit == "month":
        return _add_months(reference_date, amount)

    return _add_months(reference_date, amount * 12)


_DATE_LABEL = (
    r"(?:report|collection|collected|sample|test|visit|reported)?"
    r"\s*date(?:\s+of\s+(?:report|collection|visit))?\s*[:\-]\s*"
)

_DATE_FORMATS = [
    (r"(\d{4}-\d{2}-\d{2})", ["%Y-%m-%d"]),
    (r"(\d{1,2}[/-]\d{1,2}[/-]\d{4})", ["%d/%m/%Y", "%d-%m-%Y"]),
    (
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        ["%d %b %Y", "%d %B %Y"],
    ),
]


def extract_document_date(pages) -> Optional[date]:
    """
    Find an explicitly labelled report/collection date.

    Day-first is assumed for numeric dates (DD/MM/YYYY).
    """

    for page in pages:
        text = page["text"]

        for value_pattern, formats in _DATE_FORMATS:
            match = re.search(
                _DATE_LABEL + value_pattern,
                text,
                re.IGNORECASE,
            )

            if match is None:
                continue

            for date_format in formats:
                try:
                    return datetime.strptime(
                        match.group(1),
                        date_format,
                    ).date()
                except ValueError:
                    continue

    return None
