"""
Handwritten prescription intake: detect, quality-gate, label, route.
No recognition model; synthetic images only.
"""

import io
import random
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from app.database import SessionLocal
from app.models import AuditEvent, ClinicalFact, Commitment, Document, Observation, SourceEvidence

from tests.conftest import INITIAL_REPORT_TEXT, PRESCRIPTION_TEXT, case_of, ingest, make_image


DEMO_PRESCRIPTION = Path(__file__).resolve().parents[2] / "data" / "demo" / "jeevaflow_demo_prescription_2026-09-20.pdf"


def handwritten_png(seed: int = 1, size=(1400, 1000)) -> bytes:
    """
    Pen-like strokes: wavering baseline, cursive loops and pressure
    (stroke width) that varies along each stroke.
    """

    rng = random.Random(seed)
    page = np.full((size[1], size[0]), 242, np.uint8)

    for line in range(6):
        base = 130 + line * 130 + rng.randint(-10, 10)
        x = 80 + rng.randint(0, 40)

        while x < size[0] - 200:
            length = rng.randint(90, 260)
            amp, freq, phase = rng.uniform(12, 28), rng.uniform(0.12, 0.22), rng.uniform(0, 6.28)
            previous = None

            for step in range(length):
                point = (
                    int(x + step + 6 * np.sin(step * 0.3)),
                    int(base + amp * np.sin(freq * step + phase) + 0.08 * step * rng.uniform(-1, 1)),
                )
                width = max(1, int(round(2.5 + 2.2 * np.sin(step * 0.07 + phase))))

                if previous is not None:
                    cv2.line(page, previous, point, 25, width, cv2.LINE_AA)

                previous = point

            x += length + rng.randint(30, 70)

    buffer = io.BytesIO()
    Image.fromarray(page).save(buffer, format="PNG")
    return buffer.getvalue()


def _rows(model, document_ref):
    db = SessionLocal()
    try:
        document = db.query(Document).filter(Document.ref == document_ref).one()
        return db.query(model).filter(model.document_id == document.id).all()
    finally:
        db.close()


def test_handwritten_image_is_accepted_and_flagged(patient_ref):
    result = ingest(patient_ref, handwritten_png(), "image/png")

    assert result["processing_status"] == "PROCESSED"
    assert result["content_kind"] == "HANDWRITTEN"
    assert result["needs_manual_review"] is True

    db = SessionLocal()
    try:
        reasons = [
            event.reason for event in db.query(AuditEvent).filter(AuditEvent.action == "CONTENT_KIND_DETERMINED")
        ]
    finally:
        db.close()

    assert reasons == ["HANDWRITTEN:REVIEW"]


def test_printed_demo_pdf_is_unaffected(patient_ref):
    result = ingest(patient_ref, DEMO_PRESCRIPTION.read_bytes())

    assert result["processing_status"] == "PROCESSED"
    assert result["content_kind"] == "PRINT"
    assert result["needs_manual_review"] is False
    assert result["facts_created"] > 0


def test_printed_photo_is_print(patient_ref):
    result = ingest(patient_ref, make_image(INITIAL_REPORT_TEXT), "image/png")

    assert result["content_kind"] == "PRINT"
    assert result["needs_manual_review"] is False
    assert result["open_loops_created"] >= 1


@pytest.mark.parametrize(
    "content",
    [
        make_image(PRESCRIPTION_TEXT, blur=8),
        make_image(PRESCRIPTION_TEXT, dpi=40),
    ],
    ids=["blurry", "tiny"],
)
def test_poor_photo_returns_retake_and_reads_nothing(patient_ref, content):
    result = ingest(patient_ref, content, "image/png")

    assert result["ingestion_status"] == "RETAKE"
    assert result["content_kind"] is None

    for model in (SourceEvidence, ClinicalFact, Observation, Commitment):
        assert _rows(model, result["ref"]) == []


def test_handwritten_content_stays_uncertain_and_creates_no_open_loops(patient_ref, monkeypatch):
    from app import pipeline

    real = pipeline.run_extraction

    def as_handwritten(*args, **kwargs):
        extracted = real(*args, **kwargs)
        assert extracted["commitments"] and extracted["facts"]  # would otherwise be stored
        extracted["content_kind"] = "HANDWRITTEN"
        for item in extracted["facts"]:
            item["state"] = "SOURCE_FACT"
        return extracted

    monkeypatch.setattr(pipeline, "run_extraction", as_handwritten)
    monkeypatch.setattr(pipeline, "detect_potential_matches", lambda *a: pytest.fail("loop matching ran"))

    result = ingest(patient_ref, make_image(PRESCRIPTION_TEXT + "\n" + INITIAL_REPORT_TEXT), "image/png")

    assert result["needs_manual_review"] is True
    assert result["open_loops_created"] == 0
    assert result["potential_matches"] == []
    assert _rows(Commitment, result["ref"]) == []

    facts = _rows(ClinicalFact, result["ref"])
    observations = _rows(Observation, result["ref"])
    assert facts and observations
    assert {fact.state for fact in facts} == {"UNCERTAIN"}
    assert {observation.fact_state for observation in observations} == {"UNCERTAIN"}


def test_spoofed_executable_renamed_jpg_is_rejected(patient_ref):
    from app.storage import FileValidationError

    with pytest.raises(FileValidationError):
        ingest(patient_ref, b"MZ\x90\x00\x03\x00\x00\x00This program cannot be run in DOS mode", "image/jpeg")

    db = SessionLocal()
    try:
        assert db.query(Document).count() == 0
    finally:
        db.close()


# ============================================================
# REGRESSION: handwriting is classified BEFORE the readability check
# ============================================================

def _patch_extraction(monkeypatch, content_kind=None, blank_text=False, forbid_matching=True):
    """
    Wrap the real worker extraction: optionally override its content
    kind or blank its OCR text (handwriting Tesseract cannot read),
    and fail the test if loop matching runs.
    """

    from app import pipeline

    real = pipeline.run_extraction

    def wrapped(*args, **kwargs):
        extracted = real(*args, **kwargs)
        if content_kind:
            extracted["content_kind"] = content_kind
        if blank_text and extracted.get("pages"):
            for page in extracted["pages"]:
                page["text"] = ""
            extracted["observations"], extracted["commitments"], extracted["facts"] = [], [], []
        return extracted

    monkeypatch.setattr(pipeline, "run_extraction", wrapped)

    if forbid_matching:
        def no_matching(*args, **kwargs):
            raise AssertionError("loop matching must not run for review documents")

        monkeypatch.setattr(pipeline, "detect_potential_matches", no_matching)


def test_detector_runs_when_ocr_finds_no_words():
    from app.worker import _page_kind

    image = Image.open(io.BytesIO(handwritten_png(seed=5)))
    assert _page_kind(image, []) == "HANDWRITTEN"
    assert _page_kind(Image.new("RGB", (900, 900), "white"), []) == "BLANK"


def test_document_kind_rules():
    from app.worker import _document_kind

    assert _document_kind(["PRINT", "PRINT"]) == "PRINT"
    assert _document_kind(["HANDWRITTEN"]) == "HANDWRITTEN"
    assert _document_kind(["PRINT", "HANDWRITTEN"]) == "MIXED"
    assert _document_kind(["PRINT", "UNKNOWN"]) == "PRINT"  # blank trailing page
    assert _document_kind(["UNKNOWN"]) == "UNKNOWN"
    assert _document_kind(["BLANK"]) == "UNKNOWN"
    assert _document_kind(["HANDWRITTEN", "BLANK"]) == "HANDWRITTEN"


def test_handwritten_image_without_ocr_text_is_not_rejected(patient_ref, monkeypatch):
    _patch_extraction(monkeypatch, blank_text=True)

    result = ingest(patient_ref, handwritten_png(seed=2), "image/png")

    assert result["ingestion_status"] == "PROCESSED"
    assert result["processing_status"] == "PROCESSED"
    assert result["error"] is None
    assert result["processing_error"] is None
    assert result["content_kind"] == "HANDWRITTEN"
    assert result["needs_manual_review"] is True
    assert {"stage": "DOCTOR_REVIEW", "status": "REQUIRED", "detail": "HANDWRITTEN"} in result["stages"]
    assert result["open_loops_created"] == 0
    assert result["potential_matches"] == []
    assert _rows(Commitment, result["ref"]) == []

    db = SessionLocal()
    try:
        reasons = [e.reason for e in db.query(AuditEvent).filter(AuditEvent.action == "CONTENT_KIND_DETERMINED")]
    finally:
        db.close()

    assert reasons == ["HANDWRITTEN:REVIEW"]


def test_handwritten_image_runs_no_loop_matching(patient_ref, monkeypatch):
    _patch_extraction(monkeypatch)

    result = ingest(patient_ref, handwritten_png(seed=3), "image/png")

    assert result["content_kind"] == "HANDWRITTEN"
    assert result["open_loops_created"] == 0
    assert result["potential_matches"] == []


@pytest.mark.parametrize("kind", ["MIXED", "UNKNOWN"])
def test_mixed_and_unknown_are_routed_to_review(patient_ref, monkeypatch, kind):
    _patch_extraction(monkeypatch, content_kind=kind)

    result = ingest(patient_ref, make_image(PRESCRIPTION_TEXT + "\n" + INITIAL_REPORT_TEXT), "image/png")

    assert result["content_kind"] == kind
    assert result["needs_manual_review"] is True
    assert result["open_loops_created"] == 0
    assert _rows(Commitment, result["ref"]) == []
    assert {fact.state for fact in _rows(ClinicalFact, result["ref"])} <= {"UNCERTAIN"}
    assert {o.fact_state for o in _rows(Observation, result["ref"])} <= {"UNCERTAIN"}


def test_unknown_without_text_is_review_not_rejection(patient_ref, monkeypatch):
    _patch_extraction(monkeypatch, content_kind="UNKNOWN", blank_text=True)

    result = ingest(patient_ref, make_image(PRESCRIPTION_TEXT), "image/png")

    assert result["processing_status"] == "PROCESSED"
    assert result["needs_manual_review"] is True


def test_print_without_text_still_fails_as_before(patient_ref, monkeypatch):
    _patch_extraction(monkeypatch, content_kind="PRINT", blank_text=True, forbid_matching=False)

    result = ingest(patient_ref, make_image(PRESCRIPTION_TEXT), "image/png")

    assert result["ingestion_status"] == "FAILED"
    assert result["error"] == "No readable text was detected in the document."


def test_print_follows_normal_extraction_and_matching(patient_ref, monkeypatch):
    from app import pipeline

    calls = []
    real = pipeline.detect_potential_matches
    monkeypatch.setattr(pipeline, "detect_potential_matches", lambda *a: calls.append(1) or real(*a))

    result = ingest(patient_ref, make_image(INITIAL_REPORT_TEXT), "image/png")

    assert result["content_kind"] == "PRINT"
    assert result["needs_manual_review"] is False
    assert result["open_loops_created"] >= 1
    assert calls == [1]
    assert not any(stage["stage"] == "DOCTOR_REVIEW" for stage in result["stages"])


def test_doctor_form_flags_handwritten_document(patient_ref, doctor, monkeypatch):
    _patch_extraction(monkeypatch, blank_text=True)
    ingest(patient_ref, handwritten_png(seed=4), "image/png")

    form = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form").json()

    assert any(source["needs_manual_review"] and source["content_kind"] == "HANDWRITTEN" for source in form["sources"])
    assert any(
        item["section"] == "DOCUMENT" and item["note"] == "Handwritten document: needs doctor review."
        for item in form["uncertain"]
    )
