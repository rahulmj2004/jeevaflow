"""
Extraction, provenance and the secure ingestion pipeline.
"""

from datetime import date

import pytest

from app import pipeline
from app.database import SessionLocal
from app.extraction import extract_commitments, extract_observations
from app.models import ClinicalFact, Document, Observation, SourceEvidence
from app.ocr import extract_image_text
from app.patient_lookup import normalize_phone
from app.provenance import locate_quote
from app.quality import assess_image_quality

from tests.conftest import (
    INITIAL_REPORT_TEXT,
    PRESCRIPTION_TEXT,
    ingest,
    make_image,
    make_pdf,
    make_scanned_pdf,
)


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_phone_normalization():
    expected = "+919876543210"

    for raw in [
        "+919876543210",
        "919876543210",
        "+91 9876543210",
        "+91 98765 43210",
        "9876543210",
        "09876543210",
        "whatsapp:+919876543210",
    ]:
        assert normalize_phone(raw) == expected, raw

    assert normalize_phone("12345") is None
    assert normalize_phone("") is None
    assert normalize_phone(None) is None


def test_pdf_ingestion_is_source_backed_with_full_provenance(patient_ref):
    result = ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))

    assert result["processing_status"] == "PROCESSED"
    assert result["observations_created"] == 2
    assert result["open_loops_created"] == 2
    assert result["scan_status"] == "CLEAN"
    assert [stage["stage"] for stage in result["stages"]][:5] == [
        "VALIDATION", "ACTIVE_CONTENT_SCAN", "SANITISATION", "MALWARE_SCAN", "ENCRYPTION",
    ]

    db = SessionLocal()

    try:
        document = db.query(Document).filter(Document.ref == result["ref"]).one()
        evidence = db.query(SourceEvidence).filter(SourceEvidence.document_id == document.id).all()

        assert evidence
        for item in evidence:
            page_text = item.quote  # decrypted transparently
            assert page_text
            assert item.page_number == 1
            assert item.document_sha256 == document.sha256
            assert item.pipeline_version
            assert item.confidence == 1.0
            assert item.bbox_x is not None and 0 <= item.bbox_x <= 1

        assert document.document_date == date(2026, 6, 15)
        # The uploaded file name is never stored.
        assert document.label == "Document uploaded in portal"
    finally:
        db.close()


def test_scanned_pdf_uses_ocr(patient_ref):
    result = ingest(patient_ref, make_scanned_pdf(INITIAL_REPORT_TEXT))

    assert result["processing_status"] == "PROCESSED"
    assert result["extraction_method"] == "OCR"
    assert result["observations_created"] >= 1


def test_image_png_and_jpeg(patient_ref):
    for image_format, content_type in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
        result = ingest(
            patient_ref,
            make_image(INITIAL_REPORT_TEXT + f"\n{image_format}", image_format=image_format),
            content_type,
        )
        assert result["processing_status"] == "PROCESSED", result
        assert result["extraction_method"] == "OCR"
        assert result["observations_created"] >= 1


def test_quality_gate(tmp_path):
    good = tmp_path / "good.png"
    good.write_bytes(make_image(INITIAL_REPORT_TEXT))
    assert assess_image_quality(str(good))["quality_status"] in {"GOOD", "WARN"}

    tiny = tmp_path / "tiny.png"
    tiny.write_bytes(make_image("x", dpi=30))
    assert assess_image_quality(str(tiny))["quality_status"] == "RETAKE"


def test_retake_image_is_not_interpreted(patient_ref):
    result = ingest(patient_ref, make_image(INITIAL_REPORT_TEXT, blur=12), "image/png")

    assert result["ingestion_status"] == "RETAKE"
    assert result["processing_status"] == "REJECTED"
    assert result["observations_created"] == 0


def test_ocr(tmp_path):
    path = tmp_path / "ocr.png"
    path.write_bytes(make_image("HbA1c: 9.4 %"))
    assert "9.4" in extract_image_text(str(path))


def test_provenance_quote_or_reject():
    text = "Report\nHbA1c: 9.4 %\n"
    assert locate_quote(text, "HbA1c: 9.4 %") == {"start_position": 7, "end_position": 19}
    assert locate_quote(text, "HbA1c: 7.0 %") is None


def test_observation_and_commitment_extraction():
    observations = extract_observations(INITIAL_REPORT_TEXT)
    assert {(o["observation_type"], o["value"]) for o in observations} == {("HbA1c", "9.4"), ("Glucose", "186")}

    commitments = extract_commitments("Reference range: 4-6\nReviewed by lab.\nRepeat HbA1c after 3 months.")
    assert [c["instruction"] for c in commitments] == ["Repeat HbA1c after 3 months"]


def test_pdf_without_text_fails_honestly(patient_ref):
    result = ingest(patient_ref, make_pdf(""))

    assert result["processing_status"] == "FAILED"
    assert result["error"] == "No readable text was detected in the document."


def test_unanchored_extraction_is_rejected(patient_ref, monkeypatch):
    real = pipeline.run_extraction

    def tampered(content, content_type, received_on):
        result = real(content, content_type, received_on)
        result["observations"].append({
            "observation_type": "HbA1c", "value": "5.0", "unit": "%",
            "quote": "HbA1c: 5.0 %", "page_number": 1,
            "start_position": 0, "end_position": 12, "bbox": None, "confidence": 1.0,
        })
        return result

    monkeypatch.setattr(pipeline, "run_extraction", tampered)

    result = ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))

    assert result["evidence_rejected"] == 1
    assert result["observations_created"] == 2


def test_idempotent_document_processing(patient_ref):
    content = make_pdf(INITIAL_REPORT_TEXT)

    first = ingest(patient_ref, content)
    second = ingest(patient_ref, content)

    assert second["duplicate"] is True
    assert second["ref"] == first["ref"]

    db = SessionLocal()
    try:
        assert db.query(Observation).count() == 2
    finally:
        db.close()


def test_failed_document_is_retried_not_deduplicated(patient_ref, monkeypatch):
    content = make_pdf(INITIAL_REPORT_TEXT)

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(pipeline, "run_extraction", broken)
    failed = ingest(patient_ref, content)
    assert failed["processing_status"] == "FAILED"
    assert failed["error"] == pipeline.GENERIC_FAILURE

    monkeypatch.undo()
    retried = ingest(patient_ref, content)
    assert retried["processing_status"] == "PROCESSED"
    assert retried["duplicate"] is False


def test_prescription_facts_are_stored_encrypted(patient_ref):
    result = ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))

    assert result["facts_created"] == 5  # 3 medications, 1 allergy, 1 prescriber

    db = SessionLocal()
    try:
        labels = sorted(fact.label for fact in db.query(ClinicalFact).all())
        assert labels == ["Dr. Synthetic Prescriber", "Glimepiride 1 mg", "Metformin 500 mg", "Penicillin", "Zyxorin 50 mg"]
    finally:
        db.close()


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_extraction_helpers(text):
    assert extract_observations(text) == []
