"""
Doctor-side pseudonymization: case alias, PII masking of free text
and blacked-out identity in the evidence viewer. Synthetic data only.
"""

import io
import json
import re

import pytest
import pytesseract
from PIL import Image

from app.security.masking import MASK, identity_terms, mask_pii

from tests.conftest import case_of, grant_consent, ingest, make_pdf, new_patient


NAME = "Anjali Varghese"
PHONE = "+91 98470 12345"

DOCUMENT = f"""SYNTHETIC TEST PRESCRIPTION
Patient Name: {NAME}
Mobile: {PHONE}
DOB: 01/02/1990
Dr. Synthetic Prescriber
Date: 20/09/2026
Allergies: Penicillin (rash)
1. Tab Metformin 500 mg 1-0-1 x 30 days
Review with {NAME} after HbA1c in 3 months
"""

LEAKS = [NAME, "Anjali", "Varghese", "9847012345", "98470 12345", "1990-01-01", "01/02/1990"]


@pytest.fixture()
def case(client):
    patient = new_patient(NAME, PHONE)
    grant_consent(patient.ref)
    ingest(patient.ref, make_pdf(DOCUMENT))
    return patient


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _assert_no_identity(payload, patient_ref):
    for text in _strings(payload):
        for leak in LEAKS + [patient_ref]:
            assert leak.lower() not in text.lower(), (leak, text)


def test_doctor_responses_contain_no_identity(doctor, case):
    alias = case_of(case.ref)
    assert re.fullmatch(r"CASE-[A-Z2-9]{4}-[A-Z2-9]{4}", alias)

    patients = doctor.get("/api/v1/doctor/patients").json()
    assert [item["case_alias"] for item in patients] == [alias]
    _assert_no_identity(patients, case.ref)

    form = doctor.get(f"/api/v1/doctor/patients/{alias}/form").json()
    assert form["patient"]["case_alias"] == alias
    assert form["patient"]["age_band"]
    assert {item["label"] for item in form["medications"]} >= {"Metformin 500 mg"}
    assert any("Dr. Synthetic Prescriber" in text for text in _strings(form["prescribers"]))
    _assert_no_identity(form, case.ref)

    for path in ("journey", "loops"):
        response = doctor.get(f"/api/v1/doctor/patients/{alias}/{path}")
        assert response.status_code == 200, response.text
        _assert_no_identity(response.json(), case.ref)

    evidence_id = form["medications"][0]["evidence"]["evidence_id"]
    _assert_no_identity(doctor.get(f"/api/v1/doctor/evidence/{evidence_id}").json(), case.ref)

    # The internal ref is not accepted in place of the alias.
    assert doctor.get(f"/api/v1/doctor/patients/{case.ref}/form").status_code == 403


def test_audit_records_alias_not_identity(doctor, case):
    from app.database import SessionLocal
    from app.models import AuditEvent
    from app.security import audit

    alias = case_of(case.ref)
    doctor.get(f"/api/v1/doctor/patients/{alias}/form")

    db = SessionLocal()
    try:
        dump = json.dumps([audit._fields(row) for row in db.query(AuditEvent).all()], default=str)
    finally:
        db.close()

    assert alias in dump
    for leak in LEAKS:
        assert leak not in dump


@pytest.mark.parametrize("text, expected", [
    ("Patient Name: Ravi Kumar   Age: 45", f"Patient Name: {MASK}   Age: 45"),
    ("Name: Ravi K. Nair", f"Name: {MASK}"),
    ("Mrs. Lakshmi Menon", f"Mrs. {MASK}"),
    ("Mobile: +91 98765 43210", f"Mobile: {MASK}"),
    ("call 098765-43210 or 9876543210", f"call {MASK} or {MASK}"),
    ("DOB: 01/02/1980", f"DOB: {MASK}"),
    ("Date of Birth - 12 March 1980", f"Date of Birth - {MASK}"),
    ("Aadhaar No: 2345 6789 0123", f"Aadhaar No: {MASK}"),
    ("UHID: HSP-00123", f"UHID: {MASK}"),
    ("Address: 12 MG Road, Kochi", f"Address: {MASK}"),
    ("anjali  varghese reports", f"{MASK} reports"),
    # Clinical context is kept.
    ("Doctor Name: Dr. Anil Sharma", "Doctor Name: Dr. Anil Sharma"),
    ("Dr. Synthetic Prescriber, Reg. No: SYN-00001", "Dr. Synthetic Prescriber, Reg. No: SYN-00001"),
    ("HbA1c: 9.4 % on 15/06/2026", "HbA1c: 9.4 % on 15/06/2026"),
])
def test_mask_pii_variants(text, expected):
    assert mask_pii(text, identity_terms(NAME, PHONE)) == expected


def test_evidence_render_blacks_out_identity(doctor, case):
    alias = case_of(case.ref)
    form = doctor.get(f"/api/v1/doctor/patients/{alias}/form").json()
    evidence_id = form["medications"][0]["evidence"]["evidence_id"]

    token = doctor.post(f"/api/v1/doctor/evidence/{evidence_id}/view-token").json()["token"]
    view = doctor.get(f"/api/v1/doctor/evidence/view/{token}")

    assert view.status_code == 200
    assert view.headers["x-jeevaflow-masking"] == "applied"

    image = Image.open(io.BytesIO(view.content))
    assert image.mode == "RGB" and image.format == "PNG"

    text = pytesseract.image_to_string(image.resize((image.width * 2, image.height * 2))).lower()

    assert "synthetic prescriber" in text  # clinical context survives
    for leak in ("anjali", "varghese", "98470", "12345", "01/02/1990"):
        assert leak not in text, leak


def test_evidence_render_fails_closed(monkeypatch):
    from app import worker

    def broken(*args, **kwargs):
        raise RuntimeError("ocr down")

    monkeypatch.setattr(worker, "_ocr_lines", broken)

    buffer = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(buffer, format="PNG")

    import base64

    result = worker.op_render({
        "data": base64.b64encode(buffer.getvalue()).decode(),
        "content_type": "image/png", "mask_terms": [NAME], "watermark": "doc | now",
    })

    assert result["masked"] is False
