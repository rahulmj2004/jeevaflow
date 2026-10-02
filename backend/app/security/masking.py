"""
Doctor-side PII masking (pseudonymization, not anonymization).

pii_spans() finds patient-identifying text: Indian phone formats,
date-of-birth values, ID numbers (Aadhaar, PAN, ABHA, UHID/MRN...),
label rules ("Patient Name:", "Name:", "Address:", Mr/Mrs/Ms ...)
and the known patient's own name and phone as exact-match terms.
mask_pii() replaces each span with [MASKED].

Prescriber/doctor names are clinical context and are NOT masked:
"Dr." is not an honorific rule, and a "Name:" label preceded by
doctor/prescriber/drug words is skipped.

Pure functions with no app.config import, so the isolated worker can
use the same rules to black out page regions.
"""

import re
from typing import Iterable, Optional


MASK = "[MASKED]"

_DATE = (
    r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
)

# Value of a "Label: value" pair: up to the end of the line, a column
# gap, or the next common label.
_VALUE = (
    r"([^\n|;]+?)(?=\s{2,}|\s+(?:Age|Sex|Gender|DOB|D\.O\.B|Date|UHID|MRN|Phone|Mobile|Ph|Address)\b"
    r"|[\n|;]|$)"
)

# (pattern, group to mask; 0 = whole match)
PATTERNS = [
    # Indian mobile numbers: +91 98765 43210, 098765-43210, 987 654 3210
    (re.compile(r"(?<![\w+])(?:\+?91[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\w)"), 0),
    (re.compile(r"(?<![\w+])(?:\+?91[\s\-]?|0)?[6-9]\d{2}[\s\-]\d{3}[\s\-]\d{4}(?!\w)"), 0),
    # Aadhaar (12 digits, 4-4-4), ABHA (14 digits, 2-4-4-4), PAN
    (re.compile(r"(?<![\w-])[2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4}(?![\w-])"), 0),
    (re.compile(r"(?<![\w-])\d{2}-?\d{4}-?\d{4}-?\d{4}(?![\w-])"), 0),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), 0),
    # Labelled identifiers
    (re.compile(
        r"(?i)\b(?:UHID|MRN|MR\s*No|IP\s*No|OP\s*No|Patient\s*ID|"
        r"Hospital\s*No|ABHA(?:\s*(?:No|Number|ID))?|Aadhaa?r(?:\s*No)?)\.?\s*[:#\-]?\s*([A-Za-z0-9/\-]*\d[A-Za-z0-9/\-]*)"
    ), 1),
    # Date of birth
    (re.compile(r"(?i)\b(?:D\.?\s?O\.?\s?B\.?|Date\s+of\s+Birth|Birth\s*Date)\s*[:\-]?\s*(" + _DATE + ")"), 1),
    # Contact labels
    (re.compile(r"(?i)\b(?:phone|mobile|mob|ph|contact|tel)\.?\s*(?:no\.?)?\s*[:\-]\s*([+\d][\d\s\-]{6,}\d)"), 1),
    # Address: rest of the line
    (re.compile(r"(?i)\baddress\s*[:\-]\s*([^\n]+)"), 1),
    # Honorifics (not Dr.)
    (re.compile(
        r"\b(?:Mr|Mrs|Ms|Miss|Smt|Shri|Sri|Kumari|Master)\.?\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]*\.?){0,2})"
    ), 1),
]

NAME_LABEL = re.compile(
    r"(?i)\b(?:patient(?:'s)?\s+name|name\s+of\s+(?:the\s+)?patient|pt\.?\s*name|name)\s*[:\-]\s*" + _VALUE
)

# A "Name:" label right after one of these words is not the patient.
NOT_PATIENT = re.compile(
    r"(?i)(?:doctor|dr\.?|prescriber|prescribing|consultant|physician|referred\s+by|drug|medicine|"
    r"medication|brand|generic|test|hospital|clinic|lab(?:oratory)?)\s*(?:'s)?\s*$"
)


def identity_terms(name: Optional[str], phone: Optional[str]) -> list[str]:
    """
    Exact-match terms for one patient: full name and phone. Single
    name tokens are not used, because they would also hit doctor
    names and common words.
    """

    terms = []

    if name and len(name.strip()) >= 3:
        terms.append(name.strip())

    digits = re.sub(r"\D", "", phone or "")

    if len(digits) >= 10:
        terms.append(digits[-10:])

    return terms


def _term_pattern(term: str) -> re.Pattern:
    if term.isdigit():
        body = r"[\s\-]?".join(term)
        return re.compile(r"(?<!\d)(?:\+?91[\s\-]?|0)?" + body + r"(?!\d)")

    body = r"[\s,.]+".join(re.escape(part) for part in term.split())
    return re.compile(r"(?i)(?<!\w)" + body + r"(?!\w)")


def pii_spans(text: str, terms: Iterable[str] = ()) -> list[tuple[int, int]]:
    if not text:
        return []

    spans = []

    for pattern, group in PATTERNS:
        for match in pattern.finditer(text):
            if match.group(group).strip():
                spans.append(match.span(group))

    for match in NAME_LABEL.finditer(text):
        if not NOT_PATIENT.search(text[max(0, match.start() - 30):match.start()]):
            spans.append(match.span(1))

    for term in terms:
        if term:
            spans.extend(match.span() for match in _term_pattern(term).finditer(text))

    merged: list[list[int]] = []

    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return [(start, end) for start, end in merged]


def mask_pii(text: Optional[str], terms: Iterable[str] = ()) -> Optional[str]:
    if not text:
        return text

    out, last = [], 0

    for start, end in pii_spans(text, terms):
        out.append(text[last:start])
        out.append(MASK)
        last = end

    out.append(text[last:])

    return "".join(out)


# Keys whose values are opaque references or hashes, never free text.
_KEEP = {"ref", "case_alias", "fact_ref", "document_ref", "consent_ref", "sha256",
         "document_sha256", "pipeline_version", "scan_engine", "token", "state",
         "review_status", "status", "scope", "scopes", "section", "type", "source",
         "document_source", "quality", "purpose", "observation_type", "unit"}


def mask_tree(value, terms: Iterable[str] = (), key: Optional[str] = None):
    """
    Mask every free-text string in a JSON-like response. Opaque refs,
    enums and hashes are left untouched.
    """

    terms = list(terms)

    if isinstance(value, dict):
        return {k: mask_tree(v, terms, k) for k, v in value.items()}

    if isinstance(value, list):
        return [mask_tree(v, terms, key) for v in value]

    if isinstance(value, str) and key not in _KEEP:
        return mask_pii(value, terms)

    return value
