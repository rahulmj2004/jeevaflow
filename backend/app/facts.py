"""
Extractive, rule-based reading of prescriptions.

Nothing here invents information. Every field is either read from
the source line or explicitly marked:

    SOURCE_FACT  read directly from the document text
    AI_INFERRED  a normalisation of source text (e.g. "BD" -> twice
                 daily, "1-0-1" -> twice daily); the source text is
                 kept alongside
    UNCERTAIN    low OCR confidence, unknown medicine, implausible
                 dose/unit/duration, or conflicting statements
    MISSING      not present in the source (value is null)

Validation (formulary, dose range, unit, frequency, duration,
cross-field consistency) can only lower confidence, never add data.
Doctor confirmation is the final clinical gate.

This module must stay free of configuration and database imports:
it runs inside the isolated processing worker.
"""

import re
from typing import Optional


SOURCE_FACT = "SOURCE_FACT"
AI_INFERRED = "AI_INFERRED"
UNCERTAIN = "UNCERTAIN"
MISSING = "MISSING"

OCR_CONFIDENCE_THRESHOLD = 0.80

# Small reference formulary for validation (not prescribing advice).
# name -> (allowed units, plausible single-dose range)
FORMULARY = {
    "metformin": ({"mg"}, (250, 1000)),
    "glimepiride": ({"mg"}, (0.5, 8)),
    "gliclazide": ({"mg"}, (30, 320)),
    "sitagliptin": ({"mg"}, (25, 100)),
    "atorvastatin": ({"mg"}, (5, 80)),
    "rosuvastatin": ({"mg"}, (5, 40)),
    "amlodipine": ({"mg"}, (2.5, 10)),
    "telmisartan": ({"mg"}, (20, 80)),
    "losartan": ({"mg"}, (25, 100)),
    "metoprolol": ({"mg"}, (12.5, 200)),
    "aspirin": ({"mg"}, (75, 650)),
    "clopidogrel": ({"mg"}, (75, 75)),
    "paracetamol": ({"mg"}, (325, 1000)),
    "amoxicillin": ({"mg"}, (250, 1000)),
    "azithromycin": ({"mg"}, (250, 500)),
    "pantoprazole": ({"mg"}, (20, 40)),
    "omeprazole": ({"mg"}, (10, 40)),
    "levothyroxine": ({"mcg"}, (12.5, 300)),
    "insulin glargine": ({"units"}, (2, 100)),
    "vitamin d3": ({"iu"}, (400, 60000)),
}

UNIT_LABELS = {"mg": "mg", "mcg": "mcg", "g": "g", "ml": "ml", "iu": "IU", "units": "units", "unit": "units"}

_FORM = r"(?:tab(?:let)?s?\.?|cap(?:sule)?s?\.?|syp\.?|syrup|inj\.?|injection)"

_MEDICATION_LINE = re.compile(
    r"^[ \t]*(?:\d+[.)][ \t]*)?"
    r"(?P<form>" + _FORM + r")?[ \t]*"
    r"(?P<name>[A-Za-z][A-Za-z\-]{2,}(?:[ \t]+[A-Za-z][A-Za-z0-9\-]{1,})?)"
    r"[ \t]+(?P<dose>\d+(?:\.\d+)?)[ \t]*(?P<unit>mcg|mg|g|ml|iu|units?)\b"
    r"(?P<rest>[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)

# (pattern, normalised value, literal?) - literal words are a
# SOURCE_FACT; abbreviations are an AI_INFERRED normalisation.
_FREQUENCIES = [
    (r"\bonce (?:daily|a day)\b", "once daily", True),
    (r"\btwice (?:daily|a day)\b", "twice daily", True),
    (r"\b(?:thrice daily|three times (?:daily|a day))\b", "three times daily", True),
    (r"\bfour times (?:daily|a day)\b", "four times daily", True),
    (r"\b(?:at night|at bedtime)\b", "at night", True),
    (r"\b(?:as needed|when required)\b", "as needed", True),
    (r"\bOD\b", "once daily", False),
    (r"\b(?:BD|BID)\b", "twice daily", False),
    (r"\b(?:TDS|TID)\b", "three times daily", False),
    (r"\bQID\b", "four times daily", False),
    (r"\bHS\b", "at night", False),
    (r"\b(?:SOS|PRN)\b", "as needed", False),
]

_DOSE_PATTERN = re.compile(r"\b([0-2])\s*-\s*([0-2])\s*-\s*([0-2])\b")

_PATTERN_COUNT = {1: "once daily", 2: "twice daily", 3: "three times daily"}

_DURATION = re.compile(
    r"(?:\bx\b|×|\bfor\b)\s*(\d{1,3})\s*(day|days|week|weeks|month|months)\b",
    re.IGNORECASE,
)

_ALLERGY_LINE = re.compile(
    r"^[ \t]*(?:known[ \t]+)?(?:drug[ \t]+)?allerg(?:y|ies)[ \t]*[:\-][ \t]*(?P<value>[^\n]+)$",
    re.IGNORECASE | re.MULTILINE,
)

_NO_ALLERGY = re.compile(r"^(?:none|nil|nkda|no known (?:drug )?allergies)\.?$", re.IGNORECASE)

_PRESCRIBER = re.compile(
    r"^[ \t]*(?P<name>Dr\.?[ \t]+[A-Z][A-Za-z.\-]+(?:[ \t]+[A-Z][A-Za-z.\-]+){0,3})[ \t]*(?P<rest>[^\n]*)$",
    re.MULTILINE,
)

_REGISTRATION = re.compile(r"\bReg(?:istration)?\.?\s*No\.?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{2,19})", re.IGNORECASE)


def field(value, state: str, source_text: Optional[str] = None, note: Optional[str] = None) -> dict:
    return {"value": value, "state": state, "source_text": source_text, "note": note}


def _number(text: str) -> float:
    return float(text)


def _frequency(rest: str) -> dict:
    found = []

    for pattern, normalised, literal in _FREQUENCIES:
        match = re.search(pattern, rest, re.IGNORECASE)

        if match:
            found.append((normalised, literal, match.group(0)))

    pattern_match = _DOSE_PATTERN.search(rest)

    if pattern_match:
        count = sum(int(part) for part in pattern_match.groups())

        if count in _PATTERN_COUNT:
            found.append((_PATTERN_COUNT[count], False, pattern_match.group(0)))
        else:
            return field(None, UNCERTAIN, pattern_match.group(0), "Dosing pattern could not be read reliably.")

    if not found:
        return field(None, MISSING, None, "Frequency not stated in the source.")

    values = {item[0] for item in found}

    # "once daily ... at night" is one instruction, not a conflict.
    if values == {"once daily", "at night"}:
        literal = all(item[1] for item in found)
        source = ", ".join(dict.fromkeys(item[2] for item in found))

        if literal:
            return field("once daily at night", SOURCE_FACT, source)

        return field("once daily at night", AI_INFERRED, source, f"Normalised from \u201c{source}\u201d.")

    if len(values) > 1:
        return field(
            None, UNCERTAIN, ", ".join(item[2] for item in found),
            "Conflicting frequency statements.",
        )

    value = found[0][0]
    literal = any(item[1] for item in found)
    source = ", ".join(dict.fromkeys(item[2] for item in found))

    if literal:
        return field(value, SOURCE_FACT, source)

    return field(value, AI_INFERRED, source, f"Normalised from “{source}”.")


def _duration(rest: str) -> dict:
    match = _DURATION.search(rest)

    if not match:
        return field(None, MISSING, None, "Duration not stated in the source.")

    amount = int(match.group(1))
    unit = match.group(2).lower().rstrip("s")
    days = amount * {"day": 1, "week": 7, "month": 30}[unit]
    value = f"{amount} {unit}{'' if amount == 1 else 's'}"

    if amount == 0 or days > 365:
        return field(value, UNCERTAIN, match.group(0), "Implausible duration.")

    return field(value, SOURCE_FACT, match.group(0))


def _overall(fields: dict) -> str:
    states = {item["state"] for item in fields.values()}

    if UNCERTAIN in states:
        return UNCERTAIN

    if AI_INFERRED in states:
        return AI_INFERRED

    return SOURCE_FACT


def extract_medications(pages: list[dict]) -> list[dict]:
    items = []

    for page in pages:
        text = page["text"]

        for match in _MEDICATION_LINE.finditer(text):
            name_raw = re.sub(r"\s+", " ", match.group("name")).strip()
            name_key = name_raw.lower()
            known = FORMULARY.get(name_key)

            # A line counts as a medication only with a dosage form
            # prefix or a formulary name, so lab lines are not read
            # as medicines.
            if not match.group("form") and known is None:
                continue

            dose_value = match.group("dose")
            unit_key = match.group("unit").lower()
            unit = UNIT_LABELS.get(unit_key, unit_key)
            dose_text = f"{dose_value} {unit}"

            if known is None:
                name = field(
                    name_raw, UNCERTAIN, name_raw,
                    "Not in the reference formulary; confirm the medicine name.",
                )
                dose = field(dose_text, SOURCE_FACT, match.group(0).strip()[:80])
            else:
                name = field(name_raw, SOURCE_FACT, name_raw)
                units, (low, high) = known

                if UNIT_LABELS.get(unit_key, unit_key).lower() not in units:
                    dose = field(dose_text, UNCERTAIN, dose_text, "Unexpected unit for this medicine.")
                elif not low <= _number(dose_value) <= high:
                    dose = field(dose_text, UNCERTAIN, dose_text, "Dose outside the expected range.")
                else:
                    dose = field(dose_text, SOURCE_FACT, dose_text)

            rest = match.group("rest")
            fields = {
                "name": name,
                "dose": dose,
                "frequency": _frequency(rest),
                "duration": _duration(rest),
            }

            quote = match.group(0).strip()
            start = text.find(quote, match.start())

            items.append({
                "category": "MEDICATION",
                "label": f"{name_raw} {dose_text}",
                "fields": fields,
                "state": _overall(fields),
                "quote": quote,
                "page_number": page["page_number"],
                "start_position": start,
                "end_position": start + len(quote),
            })

    return items


def extract_allergies(pages: list[dict]) -> list[dict]:
    items = []

    for page in pages:
        text = page["text"]

        for match in _ALLERGY_LINE.finditer(text):
            value = match.group("value").strip().rstrip(".")
            quote = match.group(0).strip()
            start = text.find(quote, match.start())

            if _NO_ALLERGY.match(value):
                entries = [("No known allergies", None)]
            else:
                entries = []

                for part in re.split(r"[;,]", value):
                    part = part.strip()

                    if not part:
                        continue

                    reaction = re.search(r"\(([^)]+)\)", part)
                    substance = re.sub(r"\s*\([^)]*\)", "", part).strip()
                    entries.append((substance, reaction.group(1).strip() if reaction else None))

            for substance, reaction in entries:
                fields = {
                    "substance": field(substance, SOURCE_FACT, quote),
                    "reaction": (
                        field(reaction, SOURCE_FACT, quote)
                        if reaction
                        else field(None, MISSING, None, "Reaction not stated in the source.")
                        if substance != "No known allergies"
                        else field(None, SOURCE_FACT, None)
                    ),
                }

                items.append({
                    "category": "ALLERGY",
                    "label": substance,
                    "fields": fields,
                    "state": SOURCE_FACT,
                    "quote": quote,
                    "page_number": page["page_number"],
                    "start_position": start,
                    "end_position": start + len(quote),
                })

    return items


def extract_prescriber(pages: list[dict]) -> list[dict]:
    items = []

    for page in pages:
        text = page["text"]
        match = _PRESCRIBER.search(text)

        if not match:
            continue

        name = match.group("name").strip()
        quote = match.group(0).strip()
        start = text.find(quote, match.start())
        registration = _REGISTRATION.search(text)

        fields = {
            "name": field(name, SOURCE_FACT, name),
            "registration": (
                field(registration.group(1), SOURCE_FACT, registration.group(0))
                if registration
                else field(None, MISSING, None, "Registration number not found.")
            ),
        }

        items.append({
            "category": "PRESCRIBER",
            "label": name,
            "fields": fields,
            "state": SOURCE_FACT,
            "quote": quote,
            "page_number": page["page_number"],
            "start_position": start,
            "end_position": start + len(quote),
        })

        break

    return items


def apply_confidence(item: dict, confidence: Optional[float]) -> dict:
    """
    Low OCR confidence downgrades every read field to UNCERTAIN.
    """

    item["confidence"] = confidence

    if confidence is not None and confidence < OCR_CONFIDENCE_THRESHOLD:
        for value in item["fields"].values():
            if value["state"] in {SOURCE_FACT, AI_INFERRED}:
                value["state"] = UNCERTAIN
                value["note"] = "Low OCR confidence; confirm against the source."

        item["state"] = UNCERTAIN

    return item


def extract_facts(pages: list[dict]) -> list[dict]:
    return extract_medications(pages) + extract_allergies(pages) + extract_prescriber(pages)
