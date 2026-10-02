"""
Hallucination protection: extractive-first reading of prescriptions.
Nothing may be invented; absent information is MISSING (null).
"""

from app.facts import (
    AI_INFERRED,
    MISSING,
    SOURCE_FACT,
    UNCERTAIN,
    apply_confidence,
    extract_allergies,
    extract_facts,
    extract_medications,
    extract_prescriber,
)


def page(text):
    return [{"page_number": 1, "text": text}]


def med(text):
    items = extract_medications(page(text))
    assert len(items) == 1, items
    return items[0]


def test_literal_fields_are_source_facts():
    item = med("Tab Atorvastatin 10 mg once daily for 30 days")

    assert item["fields"]["name"] == {"value": "Atorvastatin", "state": SOURCE_FACT, "source_text": "Atorvastatin", "note": None}
    assert item["fields"]["dose"]["value"] == "10 mg"
    assert item["fields"]["frequency"]["value"] == "once daily"
    assert item["fields"]["frequency"]["state"] == SOURCE_FACT
    assert item["fields"]["duration"]["value"] == "30 days"
    assert item["state"] == SOURCE_FACT
    assert item["quote"] == "Tab Atorvastatin 10 mg once daily for 30 days"


def test_abbreviations_are_marked_ai_inferred_with_source_text():
    bd = med("Tab Metformin 500 mg BD x 30 days")
    assert bd["fields"]["frequency"]["value"] == "twice daily"
    assert bd["fields"]["frequency"]["state"] == AI_INFERRED
    assert bd["fields"]["frequency"]["source_text"] == "BD"

    pattern = med("Tab Metformin 500 mg 1-0-1 x 30 days")
    assert pattern["fields"]["frequency"]["value"] == "twice daily"
    assert pattern["fields"]["frequency"]["state"] == AI_INFERRED
    assert pattern["state"] == AI_INFERRED


def test_never_invents_frequency_or_duration():
    item = med("Metformin 500mg")

    assert item["fields"]["dose"]["value"] == "500 mg"
    assert item["fields"]["frequency"] == {
        "value": None, "state": MISSING, "source_text": None,
        "note": "Frequency not stated in the source.",
    }
    assert item["fields"]["duration"]["value"] is None
    assert item["fields"]["duration"]["state"] == MISSING


def test_unknown_medicine_is_uncertain():
    item = med("Tab Zyxorin 50 mg BD x 5 days")

    assert item["fields"]["name"]["state"] == UNCERTAIN
    assert item["state"] == UNCERTAIN


def test_dose_and_unit_validation():
    assert med("Tab Metformin 5000 mg once daily")["fields"]["dose"]["state"] == UNCERTAIN
    assert med("Tab Metformin 500 mcg once daily")["fields"]["dose"]["state"] == UNCERTAIN
    assert med("Inj Insulin glargine 10 units at night")["fields"]["dose"]["state"] == SOURCE_FACT


def test_conflicting_frequency_is_uncertain():
    item = med("Tab Metformin 500 mg once daily BD")

    assert item["fields"]["frequency"]["state"] == UNCERTAIN
    assert item["fields"]["frequency"]["value"] is None


def test_once_daily_at_night_is_one_instruction():
    item = med("Tab Atorvastatin 10 mg once daily at night")

    assert item["fields"]["frequency"]["value"] == "once daily at night"
    assert item["fields"]["frequency"]["state"] == SOURCE_FACT


def test_implausible_duration_is_uncertain():
    assert med("Tab Metformin 500 mg BD x 900 days")["fields"]["duration"]["state"] == UNCERTAIN


def test_low_ocr_confidence_downgrades_to_uncertain():
    item = apply_confidence(med("Tab Metformin 500 mg once daily"), 0.42)

    assert item["state"] == UNCERTAIN
    assert all(field["state"] in {UNCERTAIN, MISSING} for field in item["fields"].values())


def test_lab_lines_are_not_read_as_medicines():
    assert extract_medications(page("Glucose 186 mg\nHbA1c: 9.4 %\nCreatinine 1.1 mg")) == []


def test_allergies():
    items = extract_allergies(page("Allergies: Penicillin (rash), Sulfa"))

    assert [item["label"] for item in items] == ["Penicillin", "Sulfa"]
    assert items[0]["fields"]["reaction"]["value"] == "rash"
    assert items[1]["fields"]["reaction"]["state"] == MISSING

    none = extract_allergies(page("Allergies: NKDA"))
    assert none[0]["label"] == "No known allergies"


def test_prescriber_registration_missing_is_flagged():
    items = extract_prescriber(page("Dr. Synthetic Person\nClinic"))

    assert items[0]["fields"]["name"]["value"] == "Dr. Synthetic Person"
    assert items[0]["fields"]["registration"]["state"] == MISSING


def test_every_fact_has_an_exact_quote():
    text = "Dr. Synthetic Person\nReg. No: SYN-1\nAllergies: Penicillin\nTab Metformin 500 mg BD\n"

    for item in extract_facts(page(text)):
        assert text[item["start_position"]:item["end_position"]] == item["quote"]
