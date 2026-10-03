"""
Drift checks: does each extracted fact still mean what its source
evidence (the exact quote) says?

Two kinds of check, because each is reliable at different things:

    RULE   identity: medicine names, numbers and units must appear in
           the quote (the model is unreliable at name swaps); clinically
           relevant qualifiers (stop, SOS, ...) must not be dropped;
           facts in one document must not contradict each other;
           handwritten / low-confidence sources are flagged.
    MODEL  meaning: a local NLI model tests the extracted statement
           against the quote and flags contradictions (timing, dose,
           negation). "Neutral" is not a finding: abbreviations such
           as BD are often neutral to the model.

A finding never carries a corrected value. Its reason is a fixed
template, its evidence is the exact source quote, and its action is
always REVIEW REQUIRED. No config or database imports: this runs in
the isolated worker.
"""

import difflib
import re
from typing import Optional

from . import model as nli


REVIEW_REQUIRED = "REVIEW REQUIRED"

CONTRADICTION_THRESHOLD = 0.80
OCR_CONFIDENCE_THRESHOLD = 0.80
NAME_VARIANT_RATIO = 0.75

REASONS = {
    "TIMING_MISMATCH": "The extracted timing or frequency conflicts with the source evidence.",
    "DOSE_UNIT_MISMATCH": "The extracted {field} conflicts with the source evidence.",
    "DURATION_MISMATCH": "The extracted duration conflicts with the source evidence.",
    "VALUE_MISMATCH": "The extracted value conflicts with the source evidence.",
    "MEANING_CONFLICT": "The extracted {field} conflicts with the source evidence.",
    "OCR_NAME_VARIANT": "The extracted name is spelled differently in the source evidence (possible OCR change).",
    "UNSUPPORTED_EXTRACTION": "The extracted {field} cannot be found in the source evidence.",
    "MISSING_QUALIFIER": "The source evidence contains a clinically relevant word that the extracted fact does not carry.",
    "CONTRADICTORY_FACTS": "Another fact from this document gives a different {field} for the same item.",
    "OCR_UNCERTAINTY": "The source is handwritten or was read with low OCR confidence; the extraction may not match the original.",
}

MODEL_CODES = {
    "dose": "DOSE_UNIT_MISMATCH",
    "frequency": "TIMING_MISMATCH",
    "duration": "DURATION_MISMATCH",
    "value": "VALUE_MISMATCH",
}

# Words that change what a medication line means.
QUALIFIERS = [
    "stop", "discontinue", "discontinued", "withhold", "hold", "avoid", "not",
    "sos", "prn", "if needed", "if required", "taper",
]

UNIT_ALIASES = {
    "mg": ["mg"],
    "mcg": ["mcg", "µg", "ug"],
    "g": ["g", "gm", "gram", "grams"],
    "ml": ["ml"],
    "units": ["units", "unit", "u", "iu"],
    "%": ["%"],
    "mg/dl": ["mg/dl"],
    "mmol/l": ["mmol/l"],
}

_ALIAS_TO_UNIT = {alias: unit for unit, aliases in UNIT_ALIASES.items() for alias in aliases}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_UNIT_AFTER_NUMBER = re.compile(r"\d\s*(mg/dl|mmol/l|mcg|µg|ug|mg|ml|gm|grams?|g|units?|iu|u|%)(?![a-z])", re.I)
_CHECKED_STATES = {"SOURCE_FACT", "AI_INFERRED"}


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _numbers(text: Optional[str]) -> set[str]:
    return {str(float(number)) for number in _NUMBER.findall(text or "")}


def _units(text: Optional[str]) -> set[str]:
    return {_ALIAS_TO_UNIT[match.lower()] for match in _UNIT_AFTER_NUMBER.findall(text or "")}


def _has_word(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text) is not None


def _finding(code: str, field: str, quote: str, check: str, score: Optional[float] = None,
             source_word: Optional[str] = None) -> dict:
    return {
        "code": code,
        "field": field,
        "reason": REASONS[code].format(field=field),
        "evidence": quote,
        "check": check,
        "model_score": score,
        # A word quoted FROM the source (never a suggested value).
        "source_word": source_word,
        "action": REVIEW_REQUIRED,
    }


# ------------------------------------------------------------
# RULE checks
# ------------------------------------------------------------

def _name_check(field: str, value: str, quote: str) -> Optional[dict]:
    source = _norm(quote)
    words = re.findall(r"[a-z]+", source)

    for token in re.findall(r"[a-z]{3,}", _norm(value)):
        if _has_word(source, token):
            continue

        best = max((difflib.SequenceMatcher(None, token, word).ratio() for word in words), default=0.0)
        code = "OCR_NAME_VARIANT" if best >= NAME_VARIANT_RATIO else "UNSUPPORTED_EXTRACTION"
        return _finding(code, field, quote, "RULE")

    return None


def _quantity_check(field: str, value: str, quote: str) -> Optional[dict]:
    missing_number = not _numbers(value) <= _numbers(quote)
    missing_unit = not _units(value) <= _units(quote)

    if missing_number or missing_unit:
        code = {"dose": "DOSE_UNIT_MISMATCH", "value": "VALUE_MISMATCH"}.get(field, "UNSUPPORTED_EXTRACTION")
        return _finding(code, field, quote, "RULE")

    return None


def _qualifier_check(item: dict, quote: str) -> Optional[dict]:
    source = _norm(quote)
    carried = _norm(" ".join(
        str(part) for field in item["fields"].values()
        for part in (field.get("value"), field.get("source_text")) if part
    ))

    for word in QUALIFIERS:
        if _has_word(source, word) and not _has_word(carried, word):
            return _finding("MISSING_QUALIFIER", "instruction", quote, "RULE", source_word=word)

    return None


def _field_value(item: dict, name: str) -> Optional[str]:
    field = item["fields"].get(name) or {}
    return field.get("value") if field.get("state") in _CHECKED_STATES else None


# ------------------------------------------------------------
# MODEL checks
# ------------------------------------------------------------

def _hypotheses(item: dict, kind: str) -> list[tuple[str, str]]:
    """
    (field, statement) pairs restating the extraction in plain words.
    """

    if kind == "OBSERVATION":
        unit = f" {item['unit']}" if item.get("unit") else ""
        return [("value", f"The {item['observation_type']} result is {item['value']}{unit}.")]

    category = item["category"]

    if category == "MEDICATION":
        name = _field_value(item, "name") or item["label"]
        statements = {
            "dose": "The dose of {name} is {value}.",
            "frequency": "{name} is taken {value}.",
            "duration": "{name} is taken for {value}.",
        }
        return [
            (field, template.format(name=name, value=_field_value(item, field)))
            for field, template in statements.items()
            if _field_value(item, field)
        ]

    if category == "ALLERGY":
        substance = _field_value(item, "substance")

        if not substance:
            return []

        if substance == "No known allergies":
            return [("substance", "The patient has no known drug allergies.")]

        pairs = [("substance", f"The patient is allergic to {substance}.")]
        reaction = _field_value(item, "reaction")

        if reaction:
            pairs.append(("reaction", f"{substance} causes {reaction}."))

        return pairs

    return []


# ------------------------------------------------------------
# DOCUMENT
# ------------------------------------------------------------

def _check_item(item: dict, kind: str, content_kind: str, model) -> dict:
    quote = item.get("quote") or ""
    findings: list[dict] = []

    confidence = item.get("confidence")

    if content_kind != "PRINT" or (confidence is not None and confidence < OCR_CONFIDENCE_THRESHOLD):
        findings.append(_finding("OCR_UNCERTAINTY", "source", quote, "RULE"))

    if kind == "OBSERVATION":
        found = _quantity_check("value", f"{item['value']} {item.get('unit') or ''}", quote)
        findings += [found] if found else []
    else:
        for field_name in ("name", "substance"):
            value = _field_value(item, field_name)
            if value and value != "No known allergies":
                found = _name_check(field_name, value, quote)
                findings += [found] if found else []

        for field_name in ("dose", "duration"):
            value = _field_value(item, field_name)
            if value:
                found = _quantity_check(field_name, value, quote)
                findings += [found] if found else []

        if item["category"] == "MEDICATION":
            found = _qualifier_check(item, quote)
            findings += [found] if found else []

    checked_by_model = []

    if model is not None:
        for field_name, statement in _hypotheses(item, kind):
            scores = model.predict(quote, statement)
            checked_by_model.append(field_name)

            if scores["contradiction"] >= CONTRADICTION_THRESHOLD:
                code = MODEL_CODES.get(field_name, "MEANING_CONFLICT")
                findings.append(_finding(code, field_name, quote, "MODEL", scores["contradiction"]))

    # One finding per (code, field): when the rule and the model agree,
    # keep one finding that records both.
    unique: dict = {}
    for finding in findings:
        key = (finding["code"], finding["field"])
        if key in unique and unique[key]["check"] != finding["check"]:
            unique[key]["check"] = "RULE+MODEL"
            unique[key]["model_score"] = unique[key]["model_score"] or finding["model_score"]
        else:
            unique.setdefault(key, finding)

    findings = list(unique.values())

    return {
        "status": REVIEW_REQUIRED if findings else ("CONSISTENT" if model is not None else "RULES_ONLY"),
        "findings": findings,
        "model_checked_fields": checked_by_model,
    }


def _cross_fact_findings(facts: list[dict]) -> None:
    by_name: dict[str, list[dict]] = {}

    for item in facts:
        if item["category"] == "MEDICATION" and _field_value(item, "name"):
            by_name.setdefault(_norm(_field_value(item, "name")), []).append(item)

    for items in by_name.values():
        for field_name in ("dose", "frequency"):
            values = {_norm(_field_value(item, field_name)) for item in items if _field_value(item, field_name)}

            if len(values) > 1:
                for item in items:
                    item["drift"]["findings"].append(
                        _finding("CONTRADICTORY_FACTS", field_name, item.get("quote") or "", "RULE")
                    )
                    item["drift"]["status"] = REVIEW_REQUIRED


def check_document(facts: list[dict], observations: list[dict], content_kind: str = "PRINT",
                   model="load") -> dict:
    """
    Adds item["drift"] to every fact and observation and returns a
    document summary. model="load" loads the pinned local model; pass
    None to run rule checks only, or a model object (tests).
    """

    model_status = "ACTIVE"

    if model == "load":
        model, problem = nli.load()
        model_status = problem or "ACTIVE"
    elif model is None:
        model_status = "DISABLED"

    for item in facts:
        item["drift"] = _check_item(item, "FACT", content_kind, model)

    for item in observations:
        item["drift"] = _check_item(item, "OBSERVATION", content_kind, model)

    _cross_fact_findings(facts)

    items = facts + observations
    model_id = nli.MODEL_ID if model is not None else None

    for item in items:
        item["drift"]["model"] = model_id

    return {
        "model": model_id,
        "model_status": model_status,
        "checked": len(items),
        "flagged": sum(1 for item in items if item["drift"]["status"] == REVIEW_REQUIRED),
    }
