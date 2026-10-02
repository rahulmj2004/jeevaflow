"""
Open Loops, conflicts, potential matching, human confirmation and
the doctor-ready form, all through the authenticated doctor API.
"""

from datetime import date, timedelta

from app.database import SessionLocal
from app.models import Commitment, LoopEvent

from tests.conftest import (
    case_of,
    FOLLOWUP_REPORT_TEXT,
    INITIAL_REPORT_TEXT,
    PRESCRIPTION_TEXT,
    grant_consent,
    ingest,
    make_pdf,
    new_patient,
)


def _loops(doctor, patient_ref):
    response = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/loops")
    assert response.status_code == 200, response.text
    return response.json()


def _hba1c_loop(doctor, patient_ref):
    return next(loop for loop in _loops(doctor, patient_ref) if "HbA1c" in loop["instruction"])


def test_open_loops_and_overdue(doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))

    loops = _loops(doctor, patient_ref)
    assert {loop["instruction"] for loop in loops} == {"Repeat HbA1c after 3 months", "Follow-up with physician"}

    hba1c = _hba1c_loop(doctor, patient_ref)
    assert hba1c["due_date"] == "2026-09-15"
    assert hba1c["evidence"]["quote"] == "Repeat HbA1c after 3 months"

    db = SessionLocal()
    try:
        commitment = db.get(Commitment, hba1c["id"])
        commitment.due_date = date.today() - timedelta(days=1)
        db.commit()
    finally:
        db.close()

    assert _hba1c_loop(doctor, patient_ref)["state"] == "OVERDUE"


def test_conflict_detection_and_rejection(doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))
    assert doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/conflicts").json() == []

    ingest(patient_ref, make_pdf("Report Date: 15/06/2026\nHbA1c: 8.2 %\n"))
    conflicts = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/conflicts").json()

    assert len(conflicts) == 1
    assert conflicts[0]["status"] == "HUMAN_REVIEW_REQUIRED"
    assert sorted(item["value"] for item in conflicts[0]["observations"]) == ["8.2", "9.4"]

    rejected = next(item for item in conflicts[0]["observations"] if item["value"] == "8.2")
    assert doctor.patch(f"/api/v1/doctor/observations/{rejected['id']}/reject").status_code == 200
    assert doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/conflicts").json() == []


def test_followup_on_later_date_is_not_a_conflict(doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))
    ingest(patient_ref, make_pdf(FOLLOWUP_REPORT_TEXT))

    assert doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/conflicts").json() == []


def test_potential_match_then_human_confirmation(doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))
    followup = ingest(patient_ref, make_pdf(FOLLOWUP_REPORT_TEXT))

    assert len(followup["potential_matches"]) == 1

    loop = _hba1c_loop(doctor, patient_ref)
    assert loop["state"] == "POTENTIAL_MATCH"

    confirmed = doctor.post(f"/api/v1/doctor/commitments/{loop['id']}/confirm-completion", json={})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()

    assert body["state"] == "CLOSED"
    # The reviewer is the authenticated doctor, not a typed name.
    assert body["confirmed_match"]["reviewed_by"] == "Dr. Example (synthetic)"
    assert [entry["actor_type"] for entry in body["history"]][-2:] == ["SYSTEM", "HUMAN"]

    again = doctor.post(f"/api/v1/doctor/commitments/{loop['id']}/confirm-completion", json={})
    assert again.status_code == 409


def test_confirmation_requires_potential_match(doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))
    loop = _hba1c_loop(doctor, patient_ref)

    response = doctor.post(f"/api/v1/doctor/commitments/{loop['id']}/confirm-completion", json={})
    assert response.status_code == 409


def test_rejecting_supporting_observation_withdraws_match(doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))
    followup = ingest(patient_ref, make_pdf(FOLLOWUP_REPORT_TEXT))

    observation_id = followup["potential_matches"][0]["observation"]["id"]
    assert doctor.patch(f"/api/v1/doctor/observations/{observation_id}/reject").status_code == 200

    assert _hba1c_loop(doctor, patient_ref)["state"] in {"OPEN", "OVERDUE"}


def test_keep_open_and_review(doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))
    ingest(patient_ref, make_pdf(FOLLOWUP_REPORT_TEXT))
    loop = _hba1c_loop(doctor, patient_ref)

    assert doctor.post(f"/api/v1/doctor/commitments/{loop['id']}/review", json={}).json()["state"] == "NEEDS_REVIEW"
    assert doctor.post(f"/api/v1/doctor/commitments/{loop['id']}/keep-open", json={}).json()["state"] in {"OPEN", "OVERDUE"}

    db = SessionLocal()
    try:
        assert db.query(LoopEvent).filter(LoopEvent.commitment_id == loop["id"]).count() >= 3
    finally:
        db.close()


def test_doctor_form_sections(doctor, patient_ref):
    ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))

    form = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form").json()

    assert form["patient"]["case_alias"] == case_of(patient_ref)
    assert "name" not in form["patient"] and "ref" not in form["patient"]

    medications = {item["label"]: item for item in form["medications"]}
    assert set(medications) == {"Metformin 500 mg", "Glimepiride 1 mg", "Zyxorin 50 mg"}
    assert medications["Metformin 500 mg"]["fields"]["frequency"]["state"] == "AI_INFERRED"
    assert all(item["evidence"]["has_region"] for item in form["medications"])

    assert [item["label"] for item in form["allergies"]] == ["Penicillin"]
    assert form["prescribers"][0]["fields"]["registration"]["value"] == "SYN-00001"
    assert {group["test"] for group in form["labs"]} == {"HbA1c", "Glucose"}
    assert {note["instruction"] for note in form["notes"]} >= {"Repeat HbA1c after 3 months"}

    assert ("Zyxorin 50 mg", "name") in {(item["item"], item["field"]) for item in form["uncertain"]}
    assert {("Glimepiride 1 mg", "frequency"), ("Glimepiride 1 mg", "duration")} <= {
        (item["item"], item["field"]) for item in form["missing"]
    }
    assert len(form["sources"]) == 2
    assert form["not_shared"] == []
    assert "does not diagnose" in form["notice"]


def test_doctor_form_respects_scope(doctor, client):
    patient = new_patient("Scoped Patient", "+91 90000 11111")
    grant_consent(patient.ref, scopes=["LABS"])
    ingest(patient.ref, make_pdf(PRESCRIPTION_TEXT + "\nHbA1c: 7.0 %\n"))

    form = doctor.get(f"/api/v1/doctor/patients/{case_of(patient.ref)}/form").json()

    assert form["patient"]["age_band"] is None
    assert form["medications"] is None
    assert form["allergies"] is None
    assert form["notes"] is None
    assert form["labs"][0]["test"] == "HbA1c"
    assert {item["scope"] for item in form["not_shared"]} == {
        "DEMOGRAPHICS", "MEDICATIONS", "ALLERGIES", "INSTRUCTIONS", "SOURCE_DOCUMENTS",
    }

    # Out-of-scope data is not reachable through other routes either.
    assert doctor.get(f"/api/v1/doctor/patients/{case_of(patient.ref)}/loops").status_code == 403


def test_doctor_confirms_uncertain_fact(doctor, patient_ref):
    ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))
    form = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form").json()
    fact = next(item for item in form["medications"] if item["state"] == "UNCERTAIN")

    response = doctor.patch(f"/api/v1/doctor/facts/{fact['ref']}/confirm")
    assert response.json()["review_status"] == "CONFIRMED"

    rejected = doctor.patch(f"/api/v1/doctor/facts/{form['medications'][0]['ref']}/reject")
    assert rejected.json()["review_status"] == "REJECTED"

    form = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form").json()
    assert len(form["medications"]) == 2
