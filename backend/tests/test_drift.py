"""
Evidence-to-Clinical-Meaning Drift Detector: the local model and rule
checks flag extractions whose meaning drifted from the source quote,
point at the exact evidence, and never propose a corrected value.
"""

import json

import pytest

from app.database import SessionLocal
from app.drift import check_document
from app.drift import model as nli
from app.models import AuditEvent, ClinicalFact, Document, Observation

from tests.conftest import INITIAL_REPORT_TEXT, PRESCRIPTION_TEXT, case_of, ingest, make_image, make_pdf


needs_model = pytest.mark.skipif(
    nli.verify() is not None, reason="drift model not fetched (scripts/fetch_drift_model.py)"
)


@pytest.fixture(scope="module")
def model():
    loaded, problem = nli.load()
    assert problem is None, problem
    return loaded


def field(value, state="SOURCE_FACT"):
    return {"value": value, "state": state, "source_text": value, "note": None}


def medication(quote, name, dose=None, frequency=None, duration=None, confidence=0.95):
    missing = field(None, "MISSING")
    return {
        "category": "MEDICATION", "label": f"{name} {dose}", "quote": quote, "confidence": confidence,
        "fields": {
            "name": field(name),
            "dose": field(dose) if dose else missing,
            "frequency": field(frequency) if frequency else missing,
            "duration": field(duration) if duration else missing,
        },
    }


def codes(item):
    return {(finding["code"], finding["field"]) for finding in item["drift"]["findings"]}


def assert_never_corrects(item, *suggestions):
    """A finding carries a fixed reason and the source quote only."""
    for finding in item["drift"]["findings"]:
        assert finding["action"] == "REVIEW REQUIRED"
        assert finding["evidence"] == item["quote"]
        assert set(finding) == {
            "code", "field", "reason", "evidence", "check", "model_score", "source_word", "action",
        }
        for suggestion in suggestions:
            assert suggestion not in finding["reason"]


# ============================================================
# MODEL-BACKED MEANING CHECKS
# ============================================================

@needs_model
def test_timing_drift_is_flagged_with_exact_evidence(model):
    item = medication("Inj Insulin 20 units at night", "Insulin", "20 units", "once daily in the morning")
    check_document([item], [], model=model)

    assert item["drift"]["status"] == "REVIEW REQUIRED"
    assert ("TIMING_MISMATCH", "frequency") in codes(item)
    finding = item["drift"]["findings"][0]
    assert finding["check"] == "MODEL" and finding["model_score"] >= 0.8
    assert finding["reason"] == "The extracted timing or frequency conflicts with the source evidence."
    assert_never_corrects(item, "night", "morning")


@needs_model
def test_faithful_extraction_is_consistent(model):
    item = medication("Inj Insulin 20 units at night", "Insulin", "20 units", "at night")
    check_document([item], [], model=model)

    assert item["drift"]["status"] == "CONSISTENT"
    assert item["drift"]["model_checked_fields"] == ["dose", "frequency"]


@needs_model
def test_frequency_flip_and_negated_allergy(model):
    flipped = medication("Tab Metformin 500 mg twice daily", "Metformin", "500 mg", "once daily")
    allergy = {
        "category": "ALLERGY", "label": "Penicillin", "quote": "No known drug allergies", "confidence": 0.95,
        "fields": {"substance": field("Penicillin"), "reaction": field(None, "MISSING")},
    }
    check_document([flipped, allergy], [], model=model)

    assert ("TIMING_MISMATCH", "frequency") in codes(flipped)
    assert ("MEANING_CONFLICT", "substance") in codes(allergy)


@needs_model
def test_lab_value_drift_combines_rule_and_model(model):
    observation = {"observation_type": "HbA1c", "value": "94", "unit": "%", "quote": "HbA1c: 9.4 %", "confidence": 0.95}
    check_document([], [observation], model=model)

    finding = observation["drift"]["findings"][0]
    assert finding["code"] == "VALUE_MISMATCH"
    assert finding["check"] == "RULE+MODEL"
    assert_never_corrects(observation, "9.4")


@needs_model
def test_clean_prescription_has_no_false_positives(model):
    from app.extraction import extract_observations_from_pages
    from app.facts import extract_facts

    pages = [{"page_number": 1, "text": PRESCRIPTION_TEXT + "\n" + INITIAL_REPORT_TEXT, "method": "TEXT"}]
    facts, observations = extract_facts(pages), extract_observations_from_pages(pages)
    summary = check_document(facts, observations, model=model)

    assert summary["flagged"] == 0
    assert summary["checked"] == len(facts) + len(observations) > 0


# ============================================================
# RULE CHECKS (no model needed)
# ============================================================

@pytest.mark.parametrize(
    "item, expected",
    [
        (medication("Tab Metfornin 500 mg BD", "Metformin", "500 mg"), ("OCR_NAME_VARIANT", "name")),
        (medication("Tab Metformin 500 mg", "Glimepiride", "500 mg"), ("UNSUPPORTED_EXTRACTION", "name")),
        (medication("Tab Metformin 500 mg BD", "Metformin", "500 g"), ("DOSE_UNIT_MISMATCH", "dose")),
        (medication("Tab Metformin 500 mg BD", "Metformin", "5000 mg"), ("DOSE_UNIT_MISMATCH", "dose")),
        (medication("Stop Tab Metformin 500 mg", "Metformin", "500 mg"), ("MISSING_QUALIFIER", "instruction")),
        (medication("Tab Paracetamol 500 mg SOS", "Paracetamol", "500 mg"), ("MISSING_QUALIFIER", "instruction")),
        (medication("Tab Metformin 500 mg", "Metformin", "500 mg", confidence=0.5), ("OCR_UNCERTAINTY", "source")),
    ],
    ids=["ocr-name", "unsupported-name", "unit", "dose-10x", "stop", "sos", "low-ocr"],
)
def test_rule_checks(item, expected):
    summary = check_document([item], [], model=None)

    assert expected in codes(item)
    assert item["drift"]["status"] == "REVIEW REQUIRED"
    assert summary["model"] is None and summary["model_status"] == "DISABLED"
    assert_never_corrects(item, "Metformin" if expected[0] == "OCR_NAME_VARIANT" else "\0")


def test_dropped_qualifier_quotes_the_source_word():
    item = medication("Stop Tab Metformin 500 mg", "Metformin", "500 mg")
    check_document([item], [], model=None)

    assert item["drift"]["findings"][0]["source_word"] == "stop"


def test_contradictory_facts_in_one_document():
    first = medication("Tab Metformin 500 mg BD", "Metformin", "500 mg", "twice daily")
    second = medication("Tab Metformin 1000 mg OD", "Metformin", "1000 mg", "once daily")
    check_document([first, second], [], model=None)

    for item in (first, second):
        assert {("CONTRADICTORY_FACTS", "dose"), ("CONTRADICTORY_FACTS", "frequency")} <= codes(item)


def test_handwritten_source_is_flagged():
    item = medication("Tab Metformin 500 mg", "Metformin", "500 mg")
    check_document([item], [], content_kind="HANDWRITTEN", model=None)

    assert ("OCR_UNCERTAINTY", "source") in codes(item)


def test_tampered_model_is_refused(tmp_path):
    for name in nli.PINNED_FILES:
        (tmp_path / name).write_bytes(b"tampered")

    model, problem = nli.load(tmp_path)

    assert model is None and problem == "MODEL_CHECKSUM_MISMATCH"
    assert nli.load(tmp_path / "absent") == (None, "MODEL_MISSING")


def test_missing_model_falls_back_to_rules_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(nli, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(nli.load, "__defaults__", (tmp_path,))

    item = medication("Tab Metformin 500 mg", "Metformin", "500 mg")
    summary = check_document([item], [])

    assert summary == {"model": None, "model_status": "MODEL_MISSING", "checked": 1, "flagged": 0}
    assert item["drift"]["status"] == "RULES_ONLY"


# ============================================================
# PIPELINE: isolated worker, storage, doctor form
# ============================================================

@needs_model
def test_model_runs_inside_the_isolated_worker(patient_ref):
    result = ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))

    assert result["drift"]["model"] == nli.MODEL_ID
    assert result["drift"]["model_status"] == "ACTIVE"
    assert result["drift"]["flagged"] == 0
    assert any(stage["stage"] == "DRIFT_CHECK" for stage in result["stages"])

    db = SessionLocal()
    try:
        reasons = [e.reason for e in db.query(AuditEvent).filter(AuditEvent.action == "DRIFT_CHECKED")]
    finally:
        db.close()

    assert reasons == [f"0_OF_{result['drift']['checked']}:ACTIVE"]


def _drifted_extraction(monkeypatch):
    """
    Simulate an extractor that changed meaning: the worker's real
    output, with the Metformin dose and an HbA1c value altered, then
    re-checked by the real detector.
    """

    from app import pipeline

    real = pipeline.run_extraction

    def drifted(*args, **kwargs):
        extracted = real(*args, **kwargs)
        for item in extracted["facts"]:
            if item["category"] == "MEDICATION" and item["fields"]["name"]["value"] == "Metformin":
                item["fields"]["dose"]["value"] = "500 g"
        for item in extracted["observations"]:
            if item["observation_type"] == "HbA1c":
                item["value"] = "94"
        extracted["drift"] = check_document(extracted["facts"], extracted["observations"], model=None)
        return extracted

    monkeypatch.setattr(pipeline, "run_extraction", drifted)


def test_drifted_items_are_uncertain_encrypted_and_shown_to_doctor(patient_ref, doctor, monkeypatch):
    _drifted_extraction(monkeypatch)
    result = ingest(patient_ref, make_image(PRESCRIPTION_TEXT + "\n" + INITIAL_REPORT_TEXT), "image/png")

    assert result["drift"]["flagged"] >= 2

    db = SessionLocal()
    try:
        document = db.query(Document).filter(Document.ref == result["ref"]).one()
        metformin = [f for f in db.query(ClinicalFact).filter(ClinicalFact.document_id == document.id)
                     if f.category == "MEDICATION" and json.loads(f.fields)["name"]["value"] == "Metformin"][0]
        hba1c = db.query(Observation).filter(
            Observation.document_id == document.id, Observation.observation_type == "HbA1c"
        ).one()

        assert metformin.state == "UNCERTAIN"
        assert json.loads(metformin.fields)["dose"]["value"] == "500 g"  # value untouched, not corrected
        assert json.loads(metformin.drift)["status"] == "REVIEW REQUIRED"
        assert hba1c.fact_state == "UNCERTAIN"
        assert hba1c.fact_note == "Possible extraction drift: review required."

        raw = db.execute(
            __import__("sqlalchemy").text("SELECT drift FROM clinical_facts WHERE id = :id"), {"id": metformin.id}
        ).scalar()
        assert "DOSE_UNIT_MISMATCH" not in raw and "Metformin" not in raw  # encrypted at rest
    finally:
        db.close()

    form = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form").json()
    drifted = [m for m in form["medications"] if (m.get("drift") or {}).get("status") == "REVIEW REQUIRED"]
    assert drifted and drifted[0]["drift"]["findings"][0]["evidence"].startswith("1. Tab Metformin 500 mg")
    assert any(item.get("drift_code") == "DOSE_UNIT_MISMATCH" for item in form["uncertain"])
    assert any(item.get("drift_code") == "VALUE_MISMATCH" for item in form["uncertain"])
