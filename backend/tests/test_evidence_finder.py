"""
AI evidence finder: a doctor's query concept is embedded in the
isolated worker and ranked against THIS case's consented, quoted lab
results. Cited results above the calibrated threshold, or AI_ABSTAINED.
"""

import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.demo import FOLLOWTHROUGH_LABS, FOLLOWTHROUGH_PLAN, demo_document_bytes
from app.followthrough import encoder as enc
from app.followthrough import lexical, semantic
from app.models import AIDecision, AuditEvent

from tests.conftest import case_of, grant_consent, ingest, login, make_pdf, new_patient


needs_model = pytest.mark.skipif(enc.verify() is not None, reason="encoder not fetched")

SECRET_QUERY = "When was thyroid last checked for zebra-synthetic-7731?"


def _ask(doctor, patient_ref, query):
    return doctor.post(f"/api/v1/doctor/patients/{case_of(patient_ref)}/evidence-finder", json={"query": query})


@pytest.fixture
def labs_patient(patient_ref):
    ingest(patient_ref, demo_document_bytes(FOLLOWTHROUGH_PLAN))
    ingest(patient_ref, demo_document_bytes(FOLLOWTHROUGH_LABS))
    return patient_ref


# ============================================================
# SCENARIO D: evidence-grounded answer
# ============================================================

@needs_model
def test_query_returns_cited_evidence(doctor, labs_patient):
    response = _ask(doctor, labs_patient, "When was thyroid last checked?")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["decision"] == "ANSWERED"
    assert body["backend"] == "pubmedbert" and body["model"] == enc.MODEL_ID
    assert body["threshold"] == 0.40
    assert body["candidates"] == 5

    top = body["results"][0]
    assert top["observation_type"] == "TSH"
    assert top["score"] >= body["threshold"]
    assert top["evidence"]["quote"] == "TSH: 3.2 mIU/L"
    assert top["evidence"]["page_number"] == 1
    assert top["evidence"]["document_ref"]
    assert all(item["score"] >= body["threshold"] for item in body["results"])


# ============================================================
# SCENARIO E: abstention
# ============================================================

@needs_model
def test_unanswerable_query_abstains(doctor, labs_patient):
    body = _ask(doctor, labs_patient, "MRI brain").json()

    assert body["decision"] == "AI_ABSTAINED"
    assert body["reason"] == "BELOW_THRESHOLD"
    assert body["results"] == []
    assert body["best_score"] < body["threshold"]


def test_query_naming_no_test_abstains_without_running_the_model(doctor, labs_patient, monkeypatch):
    from app.routes import doctor as doctor_routes

    monkeypatch.setattr(doctor_routes, "run_worker", lambda *a, **k: pytest.fail("model must not run"))
    body = _ask(doctor, labs_patient, "when was the?").json()

    assert body == {**body, "decision": "AI_ABSTAINED", "reason": "QUERY_NAMES_NO_TEST", "results": []}


def test_no_results_on_record_abstains(doctor, patient_ref):
    body = _ask(doctor, patient_ref, "thyroid").json()

    assert body["decision"] == "AI_ABSTAINED" and body["reason"] == "NO_CANDIDATES"


def test_model_failure_abstains_instead_of_erroring(doctor, labs_patient, monkeypatch):
    from app.routes import doctor as doctor_routes
    from app.worker_client import WorkerError

    def broken(*args, **kwargs):
        raise WorkerError("WORKER_TIMEOUT")

    monkeypatch.setattr(doctor_routes, "run_worker", broken)
    response = _ask(doctor, labs_patient, "thyroid")

    assert response.status_code == 200
    assert response.json()["decision"] == "AI_ABSTAINED"
    assert response.json()["reason"] == "MODEL_UNAVAILABLE"


@pytest.mark.parametrize("payload", [
    {"vectors": ["garbage"], "model": enc.MODEL_ID},
    {"vectors": [], "model": enc.MODEL_ID},
    {"model": enc.MODEL_ID},
])
def test_malformed_model_output_abstains(doctor, labs_patient, monkeypatch, payload):
    from app.routes import doctor as doctor_routes

    monkeypatch.setattr(doctor_routes, "run_worker", lambda *a, **k: {"ok": True, **payload})
    body = _ask(doctor, labs_patient, "thyroid").json()

    assert body["decision"] == "AI_ABSTAINED"
    assert body["results"] == []


def test_vectors_from_another_backend_are_never_compared(doctor, labs_patient, monkeypatch):
    """
    Stored vectors are pubmedbert (or absent); a lexical-fallback query
    vector must not be scored against them.
    """

    from app.routes import doctor as doctor_routes

    query = semantic.pack(lexical.MODEL_ID, lexical.LexicalEncoder().encode(["thyroid"])[0])
    monkeypatch.setattr(doctor_routes, "run_worker", lambda *a, **k: {"model": lexical.MODEL_ID, "vectors": [query]})

    body = _ask(doctor, labs_patient, "thyroid").json()

    if enc.verify() is None:
        assert body["reason"] == "NO_COMPARABLE_CANDIDATES"
    assert body["results"] == []


# ============================================================
# AUTHORIZATION, ISOLATION, PRIVACY
# ============================================================

@needs_model
def test_results_never_cross_cases(doctor, patient_ref):
    other = new_patient("Other Synthetic Patient", "+91 98111 22334")
    grant_consent(other.ref)
    ingest(other.ref, make_pdf("SYNTHETIC LAB\nReport Date: 01/12/2026\nTSH: 9.9 mIU/L\n"))
    ingest(patient_ref, make_pdf("SYNTHETIC LAB\nReport Date: 01/12/2026\nSerum Uric Acid: 6.1 mg/dL\n"))

    body = _ask(doctor, patient_ref, "thyroid").json()
    quotes = [item["evidence"]["quote"] for item in body["results"]]

    assert "TSH: 9.9 mIU/L" not in quotes
    assert body["candidates"] == 1


def test_unconsented_doctor_is_denied(client, labs_patient):
    login(client, "dr.other")

    assert _ask(client, labs_patient, "thyroid").status_code == 403


def test_consent_without_lab_scope_is_denied(doctor):
    patient = new_patient("Scoped Synthetic Patient", "+91 98222 33445")
    grant_consent(patient.ref, scopes=["MEDICATIONS"])

    assert _ask(doctor, patient.ref, "thyroid").status_code == 403


def test_query_validation(doctor, patient_ref):
    assert _ask(doctor, patient_ref, "").status_code == 422
    assert _ask(doctor, patient_ref, "x" * 121).status_code == 422


@needs_model
def test_query_text_is_never_stored_or_logged(doctor, labs_patient, caplog):
    caplog.set_level(logging.DEBUG)
    assert _ask(doctor, labs_patient, SECRET_QUERY).status_code == 200

    db = SessionLocal()
    try:
        event = db.query(AuditEvent).filter(AuditEvent.action == "AI_EVIDENCE_QUERY").one()
        decision = db.query(AIDecision).filter(AIDecision.feature == "EVIDENCE_FINDER").one()
        dump = " ".join(
            str(value)
            for table in ("audit_events", "ai_decisions")
            for row in db.execute(text(f"SELECT * FROM {table}"))
            for value in row
        )
    finally:
        db.close()

    assert event.object_ref == case_of(labs_patient)
    assert re.fullmatch(r"(ANSWERED|AI_ABSTAINED):\w+", event.reason)
    assert decision.actor_ref and decision.backend == "pubmedbert"
    assert "zebra" not in dump and "thyroid" not in dump.lower()
    assert "zebra" not in caplog.text


# ============================================================
# NETWORK ISOLATION (static)
# ============================================================

AI_RUNTIME = ["app/followthrough", "app/drift", "app/handwriting"]
FORBIDDEN = re.compile(r"\b(?:import|from)\s+(?:requests|httpx|urllib|socket|openai|anthropic|google\.generativeai)\b|generativelanguage|api\.openai|api\.anthropic")


def test_ai_runtime_has_no_network_code():
    backend = Path(__file__).resolve().parents[1]

    for folder in AI_RUNTIME:
        for path in (backend / folder).rglob("*.py"):
            assert not FORBIDDEN.search(path.read_text()), path


def test_models_load_from_disk_only():
    source = (Path(__file__).resolve().parents[1] / "app/followthrough/encoder.py").read_text()

    assert "from_pretrained" not in source and "hf_hub" not in source and "http" not in source.split("MODEL_SOURCE")[0]


@needs_model
def test_rejected_and_unprocessed_results_are_never_returned(doctor, labs_patient):
    from app.models import Document, Observation

    db = SessionLocal()
    try:
        tsh = db.query(Observation).filter(Observation.observation_type == "TSH").one()
        tsh.review_status = "REJECTED"
        db.commit()
    finally:
        db.close()

    assert all(r["observation_type"] != "TSH" for r in _ask(doctor, labs_patient, "thyroid").json()["results"])

    db = SessionLocal()
    try:
        for document in db.query(Document).all():
            document.processing_status = "QUARANTINED"
        db.commit()
    finally:
        db.close()

    assert _ask(doctor, labs_patient, "vitamin D").json()["reason"] == "NO_CANDIDATES"
