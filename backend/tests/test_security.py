"""
Security controls. Every scenario from the security requirements:
OTP, consent, doctor authorization, files, encryption, evidence
access, CSRF, injection, logging, audit chain and retention.
"""

import io
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from app.config import BACKEND_DIR, settings
from app.database import SessionLocal
from app.identity import AuthSession, IdentitySessionLocal, PatientIdentity
from app.models import (
    AuditEvent,
    Consent,
    Document,
    DocumentKey,
    EvidenceToken,
    Observation,
    SourceEvidence,
    Transaction,
)
from app.retention import run_retention
from app.security import audit
from app.security.otp import MAX_ATTEMPTS
from app.worker_client import _environment

from tests.conftest import (
    case_of,
    INITIAL_REPORT_TEXT,
    PRESCRIPTION_TEXT,
    extract_otp,
    grant_consent,
    ingest,
    login,
    make_pdf,
    new_patient,
    staff_ref,
)


DEMO_PHONE_DIGITS = "9876543210"


# ============================================================
# HELPERS
# ============================================================

def send_demo(client, document="prescription", **extra):
    response = client.post("/api/v1/demo/whatsapp", json={"document": document, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def verify_portal(client, sent):
    otp = extract_otp(sent["messages"][1] if len(sent["messages"]) > 1 else sent["messages"][0])
    response = client.post("/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": otp})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def consent_via_portal(client, doctor_username="dr.example", scopes=None, days=7):
    session = client.get("/api/v1/portal/session").json()
    doctor_ref = staff_ref(doctor_username)
    response = client.post(
        "/api/v1/portal/consents",
        json={
            "doctor_ref": doctor_ref,
            "scopes": scopes or ["DEMOGRAPHICS", "MEDICATIONS", "LABS", "ALLERGIES", "INSTRUCTIONS", "SOURCE_DOCUMENTS"],
            "duration_days": days,
        },
    )
    assert response.status_code == 200, response.text
    assert doctor_ref in {doctor["ref"] for doctor in session["options"]["doctors"]}
    return response.json()


def demo_patient_ref(client):
    return client.get("/api/v1/demo").json()["patient"]["ref"]


def first_evidence_id(doctor, patient_ref):
    form = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form").json()
    return form["medications"][0]["evidence"]["evidence_id"]


# ============================================================
# END-TO-END DEMO FLOW
# ============================================================

def test_end_to_end_secure_flow(client, doctor):
    sent = send_demo(client)
    assert sent["signature"] == "VERIFIED"

    verify_portal(client, sent)
    granted = consent_via_portal(client)
    assert granted["processing"]["processed"] == 1

    phone = client.get("/api/v1/demo/phone").json()
    assert phone[-1]["kind"] == "PROCESSED"

    patients = doctor.get("/api/v1/doctor/patients").json()
    assert [item["case_alias"] for item in patients] == [case_of(demo_patient_ref(client))]

    form = doctor.get(f"/api/v1/doctor/patients/{patients[0]['case_alias']}/form").json()
    assert {item["label"] for item in form["medications"]} >= {"Metformin 500 mg"}

    revoked = client.post(f"/api/v1/portal/consents/{granted['consent']['ref']}/revoke").json()
    assert revoked["status"] == "REVOKED"

    assert doctor.get(f"/api/v1/doctor/patients/{patients[0]['case_alias']}/form").status_code == 403
    assert doctor.get("/api/v1/doctor/patients").json() == []


def test_patient_sees_case_code_but_it_grants_no_access(client, doctor, other_client):
    verify_portal(client, send_demo(client))
    consent_via_portal(client, "dr.example")

    case_alias = client.get("/api/v1/portal/session").json()["patient"]["case_alias"]
    assert [item["case_alias"] for item in doctor.get("/api/v1/doctor/patients").json()] == [case_alias]

    # Knowing the code is not enough: an unconsented doctor is still denied.
    login(other_client, "dr.other")
    assert other_client.get(f"/api/v1/doctor/patients/{case_alias}/form").status_code == 403


def test_tampered_demo_signature_is_rejected(client):
    sent = client.post("/api/v1/demo/whatsapp", json={"document": "prescription", "tamper_signature": True}).json()

    assert sent["http_status"] == 403
    assert sent["signature"] == "REJECTED"
    assert sent["transaction_ref"] is None


# ============================================================
# OTP
# ============================================================

def test_incorrect_otp_and_generic_error(client):
    sent = send_demo(client)
    response = client.post("/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": "000000"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Verification failed or expired."

    unknown = client.post("/api/v1/portal/verify", json={"transaction_ref": "txn_doesnotexist12345", "code": "123456"})
    assert unknown.status_code == 401
    assert unknown.json() == response.json()


def test_otp_brute_force_locks_transaction(client):
    sent = send_demo(client)
    real = extract_otp(sent["messages"][1])

    db = SessionLocal()

    try:
        for attempt in range(MAX_ATTEMPTS):
            # Skip the exponential back-off window for the test.
            transaction = db.query(Transaction).filter(Transaction.ref == sent["transaction_ref"]).one()
            transaction.otp_next_attempt_at = None
            db.commit()

            wrong = str((int(real) + attempt + 1) % 1_000_000).zfill(6)
            assert client.post(
                "/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": wrong}
            ).status_code == 401

        db.expire_all()
        transaction = db.query(Transaction).filter(Transaction.ref == sent["transaction_ref"]).one()
        assert transaction.status == "LOCKED"
        assert transaction.otp_hash is None
    finally:
        db.close()

    # Even the correct code no longer works.
    assert client.post(
        "/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": real}
    ).status_code == 401


def test_otp_backoff_blocks_rapid_retries(client):
    sent = send_demo(client)
    real = extract_otp(sent["messages"][1])

    client.post("/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": "000001"})

    # Correct code, but inside the back-off window.
    assert client.post(
        "/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": real}
    ).status_code == 401


def test_otp_is_single_use(client, other_client):
    sent = send_demo(client)
    otp = extract_otp(sent["messages"][1])

    assert client.post("/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": otp}).status_code == 200
    assert other_client.post(
        "/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": otp}
    ).status_code == 401


def test_expired_otp(client):
    sent = send_demo(client)

    db = SessionLocal()
    try:
        transaction = db.query(Transaction).filter(Transaction.ref == sent["transaction_ref"]).one()
        transaction.otp_expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    assert client.post(
        "/api/v1/portal/verify",
        json={"transaction_ref": sent["transaction_ref"], "code": extract_otp(sent["messages"][1])},
    ).status_code == 401


def test_otp_never_stored_in_plaintext(client):
    sent = send_demo(client)
    otp = extract_otp(sent["messages"][1])

    db = SessionLocal()
    try:
        transaction = db.query(Transaction).one()
        assert otp not in (transaction.otp_hash or "")
        assert len(transaction.otp_hash) == 64
    finally:
        db.close()

    raw = Path(settings.database_url.replace("sqlite:///", "")).read_bytes()
    assert otp.encode() not in raw


def test_transaction_refs_are_random_and_not_sequential(client):
    refs = [send_demo(client, None)["transaction_ref"] for _ in range(3)]

    assert len(set(refs)) == 3
    for ref in refs:
        assert re.fullmatch(r"txn_[A-Za-z0-9_-]{22}", ref)


# ============================================================
# CONSENT AND DOCTOR AUTHORIZATION
# ============================================================

def test_doctor_without_consent_is_denied(client, other_client):
    patient = new_patient()
    ingest(patient.ref, make_pdf(INITIAL_REPORT_TEXT))

    login(other_client, "dr.example")

    assert other_client.get(f"/api/v1/doctor/patients/{case_of(patient.ref)}/form").status_code == 403
    assert other_client.get("/api/v1/doctor/patients").json() == []


def test_wrong_doctor_is_denied(client, other_client, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))

    login(other_client, "dr.other")

    response = other_client.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form")
    assert response.status_code == 403
    assert response.json()["detail"] == "Access not permitted."


def test_expired_consent_denied(doctor, client):
    patient = new_patient()
    consent_ref = grant_consent(patient.ref)

    assert doctor.get(f"/api/v1/doctor/patients/{case_of(patient.ref)}/form").status_code == 200

    db = SessionLocal()
    try:
        consent = db.query(Consent).filter(Consent.ref == consent_ref).one()
        consent.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    assert doctor.get(f"/api/v1/doctor/patients/{case_of(patient.ref)}/form").status_code == 403


def test_patient_can_expire_consent_now(client, doctor):
    send_and_consent(client)
    consent_ref = client.get("/api/v1/portal/session").json()["consents"][0]["ref"]

    assert client.post(f"/api/v1/portal/consents/{consent_ref}/expire").json()["status"] == "EXPIRED"
    assert doctor.get(f"/api/v1/doctor/patients/{case_of(demo_patient_ref(client))}/form").status_code == 403


def send_and_consent(client, document="prescription", **kwargs):
    sent = send_demo(client, document)
    verify_portal(client, sent)
    return consent_via_portal(client, **kwargs)


def test_unknown_and_foreign_objects_look_the_same(doctor, patient_ref, client):
    other = new_patient("Other Synthetic", "+91 90000 22222")
    ingest(other.ref, make_pdf(INITIAL_REPORT_TEXT))

    db = SessionLocal()
    try:
        foreign = db.query(Observation).filter(Observation.patient_id == other.id).first().id
    finally:
        db.close()

    unknown = doctor.patch("/api/v1/doctor/observations/999999/verify")
    not_mine = doctor.patch(f"/api/v1/doctor/observations/{foreign}/verify")

    assert unknown.status_code == not_mine.status_code == 403
    assert unknown.json() == not_mine.json()

    assert doctor.get("/api/v1/doctor/patients/CASE-ZZZZ-ZZZZ/form").json() == doctor.get(
        f"/api/v1/doctor/patients/{case_of(other.ref)}/form"
    ).json()


def test_no_list_all_patients_or_phone_lookup(client, doctor):
    assert client.get("/api/v1/patients").status_code == 404
    assert doctor.get("/api/v1/patients").status_code == 404
    assert client.get("/api/v1/patients/lookup?phone=9876543210").status_code == 404


def test_missing_authentication(client):
    for method, path in [
        ("get", "/api/v1/doctor/patients"),
        ("get", "/api/v1/doctor/patients/pt_x/form"),
        ("get", "/api/v1/portal/session"),
        ("get", "/api/v1/audit/events"),
        ("get", "/api/v1/security/status"),
        ("post", "/api/v1/admin/patients"),
    ]:
        assert getattr(client, method)(path).status_code in (401, 422), path


def test_password_alone_is_not_enough(client):
    client.post("/api/v1/auth/login", json={"username": "dr.example", "password": "Synthetic-Test-Password-1"})

    assert client.get("/api/v1/doctor/patients").status_code == 401


def test_role_separation(client, other_client, patient_ref):
    login(other_client, "auditor")
    assert other_client.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form").status_code == 403
    assert other_client.get("/api/v1/audit/events").status_code == 200

    login(client, "dr.example")
    assert client.get("/api/v1/audit/events").status_code == 403
    assert client.post("/api/v1/admin/patients", json={"name": "x", "phone": "9000000000"}).status_code == 403


def test_login_errors_are_generic_and_lock_out(client):
    unknown = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
    wrong = client.post("/api/v1/auth/login", json={"username": "dr.example", "password": "x"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": "Invalid credentials."}

    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "dr.other", "password": "wrong"})

    locked = client.post("/api/v1/auth/login", json={"username": "dr.other", "password": "Synthetic-Test-Password-1"})
    assert locked.status_code == 401


def test_totp_code_cannot_be_replayed(client, other_client):
    from app.security.auth import totp_now
    from app.seed import demo_totp_secret

    login(client, "dr.example")
    code = totp_now(demo_totp_secret("dr.example"))

    other_client.post("/api/v1/auth/login", json={"username": "dr.example", "password": "Synthetic-Test-Password-1"})
    assert other_client.post("/api/v1/auth/mfa", json={"code": code}).status_code == 401


def test_idle_session_expires(doctor):
    idb = IdentitySessionLocal()
    try:
        for session in idb.query(AuthSession).filter(AuthSession.kind == "STAFF").all():
            session.last_seen_at = datetime.utcnow() - timedelta(hours=1)
        idb.commit()
    finally:
        idb.close()

    assert doctor.get("/api/v1/doctor/patients").status_code == 401


def test_bulk_access_anomaly(doctor, client, monkeypatch):
    from app.security import consent as consent_module

    monkeypatch.setattr(consent_module, "ANOMALY_DISTINCT_PATIENTS", 2)

    refs = []
    for index in range(3):
        patient = new_patient(f"Synthetic {index}", f"+91 9100000{index:03d}")
        grant_consent(patient.ref)
        refs.append(patient.ref)

    assert doctor.get(f"/api/v1/doctor/patients/{case_of(refs[0])}/form").status_code == 200
    assert doctor.get(f"/api/v1/doctor/patients/{case_of(refs[1])}/form").status_code == 200
    assert doctor.get(f"/api/v1/doctor/patients/{case_of(refs[2])}/form").status_code == 403


# ============================================================
# CSRF, ORIGIN, INJECTION, XSS, HEADERS
# ============================================================

def test_csrf_token_required_for_state_changes(doctor, patient_ref):
    ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))
    evidence_id = first_evidence_id(doctor, patient_ref)

    token = doctor.headers.pop("X-CSRF-Token")

    assert doctor.post(f"/api/v1/doctor/evidence/{evidence_id}/view-token").status_code == 403
    assert doctor.post(
        f"/api/v1/doctor/evidence/{evidence_id}/view-token", headers={"X-CSRF-Token": "forged"}
    ).status_code == 403
    assert doctor.post(
        f"/api/v1/doctor/evidence/{evidence_id}/view-token", headers={"X-CSRF-Token": token}
    ).status_code == 200


def test_foreign_origin_rejected(doctor, patient_ref):
    response = doctor.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403


def test_sql_injection_attempts_are_inert(client, doctor):
    for payload in ["' OR 1=1 --", "pt_x' UNION SELECT * FROM patients --", "1; DROP TABLE patients"]:
        assert doctor.get(f"/api/v1/doctor/patients/{payload}/form").status_code == 403
        assert client.post("/api/v1/auth/login", json={"username": payload, "password": payload}).status_code == 401
        assert client.post("/api/v1/portal/verify", json={"transaction_ref": payload, "code": "123456"}).status_code in (401, 422)

    db = SessionLocal()
    try:
        assert db.execute(__import__("sqlalchemy").text("SELECT count(*) FROM patients")).scalar() >= 1
    finally:
        db.close()


def test_xss_payload_is_data_not_markup(doctor, patient_ref):
    payload = "Repeat HbA1c after 3 months <script>alert(1)</script>"
    ingest(patient_ref, make_pdf(f"Report Date: 15/06/2026\nHbA1c: 9.4 %\n{payload}\n"))

    response = doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/loops")

    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.json()[0]["instruction"].endswith("<script>alert(1)</script>")


def test_security_headers_and_no_store(client):
    response = client.get("/api/v1/health")

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_validation_errors_do_not_echo_input(client):
    response = client.post("/api/v1/auth/mfa", json={"code": "SECRET-VALUE-XYZ"})

    assert response.status_code == 422
    assert "SECRET-VALUE-XYZ" not in response.text


def test_unhandled_errors_hide_stack_traces(client, monkeypatch):
    from app.routes import demo as demo_routes

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic internal detail")

    monkeypatch.setattr(demo_routes, "demo_identity", boom)

    with pytest.raises(RuntimeError):
        # TestClient re-raises server errors by default.
        client.get("/api/v1/demo")

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as safe:
        response = safe.get("/api/v1/demo")

    assert response.status_code == 500
    assert "synthetic internal detail" not in response.text
    assert "Traceback" not in response.text


# ============================================================
# FILES
# ============================================================

@pytest.mark.parametrize(
    "content, content_type, code",
    [
        (b"", "application/pdf", "EMPTY_FILE"),
        (b"MZ\x90\x00\x03", "application/pdf", "CONTENT_MISMATCH"),
        (b"%PDF-1.4 broken", "application/pdf", "CORRUPTED_FILE"),
        (b"\x89PNG\r\n\x1a\n" + b"0" * 20, "image/png", "CORRUPTED_FILE"),
        (b"GIF89a", "image/gif", "UNSUPPORTED_TYPE"),
        (b"#!/bin/sh\nrm -rf /", "application/x-sh", "UNSUPPORTED_TYPE"),
    ],
)
def test_invalid_files_rejected(patient_ref, content, content_type, code):
    from app.storage import FileValidationError

    with pytest.raises(FileValidationError) as error:
        ingest(patient_ref, content, content_type)

    assert error.value.code == code


def test_oversized_file_rejected(patient_ref, monkeypatch):
    from app.storage import FileValidationError

    monkeypatch.setattr(settings, "max_upload_bytes", 1000)

    with pytest.raises(FileValidationError) as error:
        ingest(patient_ref, b"%PDF-1.4\n" + b"0" * 5000)

    assert error.value.code == "FILE_TOO_LARGE"


def test_malicious_files_rejected(patient_ref):
    from app.security.scanner import EICAR
    from app.storage import FileValidationError

    import pymupdf

    javascript = pymupdf.open()
    javascript.new_page().insert_text((72, 72), "hello")
    javascript.set_xml_metadata("")
    pdf_js = javascript.tobytes()
    javascript.close()
    pdf_js = pdf_js.replace(b"/Type/Catalog", b"/Type/Catalog/OpenAction<</S/JavaScript/JS(app.alert(1))>>", 1)
    assert b"/JavaScript" in pdf_js

    embedded = pymupdf.open()
    embedded.new_page().insert_text((72, 72), "hello")
    embedded.embfile_add("payload.exe", b"MZ synthetic")
    pdf_embedded = embedded.tobytes()
    embedded.close()

    cases = [
        make_pdf("hello") + b"\n%" + EICAR,
        pdf_js,
        pdf_embedded,
        make_pdf("hello") + b"\nThis program cannot be run in DOS mode",
    ]

    for content in cases:
        with pytest.raises(FileValidationError) as error:
            ingest(patient_ref, content)
        assert error.value.code in {"MALWARE_DETECTED", "SUSPICIOUS_PDF", "CORRUPTED_FILE"}

    image = io.BytesIO()
    Image.new("RGB", (900, 900), "white").save(image, format="PNG")
    with pytest.raises(FileValidationError) as error:
        ingest(patient_ref, image.getvalue() + b"<script>alert(1)</script>", "image/png")
    assert error.value.code == "MALWARE_DETECTED"


def test_decompression_bomb_rejected(patient_ref):
    from app.storage import FileValidationError

    bomb = io.BytesIO()
    Image.new("L", (11000, 11000), 255).save(bomb, format="PNG", optimize=True)

    with pytest.raises(FileValidationError) as error:
        ingest(patient_ref, bomb.getvalue(), "image/png")

    assert error.value.code in {"DECOMPRESSION_BOMB", "INVALID_DIMENSIONS"}


def test_image_metadata_is_stripped(patient_ref):
    from app import vault

    image = Image.new("RGB", (1000, 1000), "white")
    exif = Image.Exif()
    exif[0x010E] = "SYNTHETIC-GPS-AND-NAME-METADATA"
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())

    result = ingest(patient_ref, buffer.getvalue(), "image/jpeg")

    db = SessionLocal()
    try:
        document = db.query(Document).filter(Document.ref == result["ref"]).one()
        stored = vault.load(db, document)
    finally:
        db.close()

    assert b"SYNTHETIC-GPS-AND-NAME-METADATA" not in stored
    assert result["sha256"] != document.stored_sha256


def test_portal_upload_requires_active_consent(client):
    sent = send_demo(client, None)
    verify_portal(client, sent)

    upload = client.post(
        "/api/v1/portal/documents",
        files={"file": ("synthetic.pdf", make_pdf(INITIAL_REPORT_TEXT), "application/pdf")},
    )
    assert upload.status_code == 403

    consent_via_portal(client)

    upload = client.post(
        "/api/v1/portal/documents",
        files={"file": ("john_doe_hiv_report.pdf", make_pdf(INITIAL_REPORT_TEXT), "application/pdf")},
    )
    assert upload.status_code == 200, upload.text
    assert "john_doe" not in upload.text


# ============================================================
# ENCRYPTION AND STORAGE
# ============================================================

def test_documents_and_fields_are_encrypted_at_rest(client, patient_ref):
    ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))

    for path in settings.vault_dir.iterdir():
        blob = path.read_bytes()
        assert blob.startswith(b"JFV1")
        assert b"Metformin" not in blob and b"%PDF" not in blob

    medical = Path(settings.database_url.replace("sqlite:///", "")).read_bytes()
    for secret in (b"Metformin", b"Penicillin", b"Synthetic Test Patient", b"91234"):
        assert secret not in medical

    identity = Path(settings.identity_database_url.replace("sqlite:///", "")).read_bytes()
    for secret in (b"Synthetic Test Patient", b"9123456789", b"1990-01-01"):
        assert secret not in identity


def test_identity_is_separate_from_medical_data(client, patient_ref):
    from sqlalchemy import inspect

    from app.database import engine

    columns = {column["name"] for column in inspect(engine).get_columns("patients")}
    assert columns == {"id", "ref", "case_alias", "created_at"}

    idb = IdentitySessionLocal()
    try:
        identity = idb.query(PatientIdentity).filter(PatientIdentity.patient_ref == patient_ref).one()
        assert identity.phone_index and len(identity.phone_index) == 64
    finally:
        idb.close()


def test_ciphertext_cannot_be_swapped_between_documents(patient_ref):
    from app import vault
    from app.security.crypto import DecryptionError

    first = ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))
    second = ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))

    db = SessionLocal()
    try:
        a = db.query(Document).filter(Document.ref == first["ref"]).one()
        b = db.query(Document).filter(Document.ref == second["ref"]).one()
        a.storage_key, b.storage_key = b.storage_key, a.storage_key

        with pytest.raises(DecryptionError):
            vault.load(db, a)
    finally:
        db.rollback()
        db.close()


def test_delete_document_destroys_key_object_and_rows(client):
    send_and_consent(client)

    document_ref = client.get("/api/v1/portal/documents").json()[0]["ref"]

    db = SessionLocal()
    try:
        document = db.query(Document).filter(Document.ref == document_ref).one()
        object_path = settings.vault_dir / document.storage_key
        document_id = document.id
    finally:
        db.close()

    assert object_path.exists()
    assert client.delete(f"/api/v1/portal/documents/{document_ref}").json() == {"deleted": True}
    assert not object_path.exists()

    db = SessionLocal()
    try:
        assert db.get(Document, document_id) is None
        assert db.query(DocumentKey).filter(DocumentKey.document_id == document_id).count() == 0
        assert db.query(SourceEvidence).filter(SourceEvidence.document_id == document_id).count() == 0
        assert db.query(AuditEvent).filter(AuditEvent.action == "DOCUMENT_DELETED", AuditEvent.object_ref == document_ref).count() == 1
    finally:
        db.close()


def test_expired_unverified_transaction_documents_are_shredded(client):
    sent = send_demo(client)

    db = SessionLocal()
    try:
        transaction = db.query(Transaction).filter(Transaction.ref == sent["transaction_ref"]).one()
        transaction.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()

        counts = run_retention(db)
        assert counts["transactions_expired"] == 1
        assert counts["unverified_documents_deleted"] == 1
        assert db.query(Document).count() == 0
    finally:
        db.close()


# ============================================================
# EVIDENCE ACCESS
# ============================================================

def test_no_direct_document_download(client, doctor, patient_ref):
    ingest(patient_ref, make_pdf(INITIAL_REPORT_TEXT))

    for path in ("/api/v1/documents/1/file", "/api/v1/documents/1", "/uploads/x.pdf", "/vault/x.jfv"):
        assert doctor.get(path).status_code == 404


def test_evidence_token_single_use_short_lived_session_bound(client, doctor, other_client, patient_ref):
    ingest(patient_ref, make_pdf(PRESCRIPTION_TEXT))
    evidence_id = first_evidence_id(doctor, patient_ref)

    def issue():
        response = doctor.post(f"/api/v1/doctor/evidence/{evidence_id}/view-token")
        assert response.status_code == 200
        return response.json()["token"]

    token = issue()
    view = doctor.get(f"/api/v1/doctor/evidence/view/{token}")
    assert view.status_code == 200
    assert view.headers["content-type"] == "image/png"
    assert "no-store" in view.headers["cache-control"]
    assert doctor.get(f"/api/v1/doctor/evidence/view/{token}").status_code == 403

    expired = issue()
    db = SessionLocal()
    try:
        for row in db.query(EvidenceToken).filter(EvidenceToken.used_at.is_(None)):
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    assert doctor.get(f"/api/v1/doctor/evidence/view/{expired}").status_code == 403

    # A token forwarded to another (even legitimately logged-in) session fails.
    forwarded = issue()
    login(client, "dr.example")
    assert client.get(f"/api/v1/doctor/evidence/view/{forwarded}").status_code == 403


def test_evidence_view_requires_source_scope_and_live_consent(client, doctor):
    patient = new_patient()
    consent_ref = grant_consent(patient.ref, scopes=["MEDICATIONS"])
    ingest(patient.ref, make_pdf(PRESCRIPTION_TEXT))
    evidence_id = first_evidence_id(doctor, patient.ref)

    assert doctor.post(f"/api/v1/doctor/evidence/{evidence_id}/view-token").status_code == 403

    full = new_patient("Full Scope", "+91 90000 33333")
    full_consent = grant_consent(full.ref)
    ingest(full.ref, make_pdf(PRESCRIPTION_TEXT))
    evidence_id = first_evidence_id(doctor, full.ref)
    token = doctor.post(f"/api/v1/doctor/evidence/{evidence_id}/view-token").json()["token"]

    db = SessionLocal()
    try:
        db.query(Consent).filter(Consent.ref == full_consent).one().revoked_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()

    # Consent revoked between issuing and viewing: denied at view time.
    assert doctor.get(f"/api/v1/doctor/evidence/view/{token}").status_code == 403


# ============================================================
# LOGGING
# ============================================================

def test_no_phi_in_logs(client, doctor, caplog):
    caplog.set_level(logging.DEBUG)

    sent = send_demo(client)
    otp = extract_otp(sent["messages"][1])
    verify_portal(client, sent)
    consent_via_portal(client)

    patient_ref = demo_patient_ref(client)
    doctor.get(f"/api/v1/doctor/patients/{case_of(patient_ref)}/form")

    logging.getLogger("jeevaflow.pipeline").info("Metformin 500 mg for patient 9876543210")

    text = caplog.text
    assert "STARTUP" in text or "DOCUMENT_RECEIVED" in text

    from app.security.phi_log import PHISafeFilter

    allowed = [record for record in caplog.records if PHISafeFilter().filter(record)]
    emitted = "\n".join(record.getMessage() for record in allowed)

    for phi in ("Metformin", "Penicillin", "JeevaFlow Demo Patient", DEMO_PHONE_DIGITS, otp, "500 mg", "rash"):
        assert phi not in emitted, phi


def test_log_event_blocks_unsafe_fields(caplog):
    from app.security.phi_log import log_event

    caplog.set_level(logging.INFO, logger="jeevaflow.security")
    log_event("DOCUMENT_RECEIVED", object="Metformin 500 mg", diagnosis="x", actor="+919876543210")

    assert "Metformin" not in caplog.text
    assert "9876543210" not in caplog.text
    assert "diagnosis=[BLOCKED]" in caplog.text


# ============================================================
# AUDIT CHAIN
# ============================================================

def test_audit_chain_verifies_and_detects_tampering(client, other_client):
    send_and_consent(client)
    login(other_client, "auditor")

    assert other_client.post("/api/v1/audit/verify").json()["valid"] is True

    events = other_client.get("/api/v1/audit/events?limit=500").json()
    target = events[len(events) // 2]["seq"]

    assert other_client.post("/api/v1/demo/audit/tamper", json={"seq": target}).status_code == 200

    broken = other_client.post("/api/v1/audit/verify").json()
    assert broken["valid"] is False
    assert broken["broken_at_seq"] == target

    assert other_client.post("/api/v1/demo/audit/restore").json()["restored"] == 1
    assert other_client.post("/api/v1/audit/verify").json()["valid"] is True


def test_audit_table_is_append_only(client):
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    from app.database import engine

    send_demo(client)

    with pytest.raises(DatabaseError):
        with engine.begin() as connection:
            connection.execute(text("UPDATE audit_events SET result = 'X'"))

    with pytest.raises(DatabaseError):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_events"))


def test_audit_detects_deleted_tail(client):
    from sqlalchemy import text

    from app.database import engine

    send_demo(client)

    db = SessionLocal()
    assert audit.verify_chain(db)["valid"] is True

    with engine.begin() as connection:
        connection.execute(text("DROP TRIGGER audit_events_no_delete"))
        connection.execute(text("DELETE FROM audit_events WHERE seq = (SELECT max(seq) FROM audit_events)"))

    result = audit.verify_chain(db)
    db.close()

    assert result["valid"] is False
    assert result["reason"] == "ANCHOR_MISMATCH"


def test_audit_events_contain_no_phi(client, doctor):
    send_and_consent(client)
    doctor.get(f"/api/v1/doctor/patients/{case_of(demo_patient_ref(client))}/form")

    db = SessionLocal()
    try:
        dump = " ".join(
            " ".join(str(value) for value in audit._fields(row).values())
            for row in db.query(AuditEvent).all()
        )
    finally:
        db.close()

    for phi in ("Metformin", "Penicillin", "Demo Patient", DEMO_PHONE_DIGITS):
        assert phi not in dump


# ============================================================
# ISOLATED WORKER
# ============================================================

def test_worker_environment_has_no_secrets():
    env = _environment("/tmp/x")

    assert not any(name.startswith(("JEEVAFLOW_", "TWILIO_")) for name in env)


def test_worker_blocks_network():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import app.worker as w; w.harden(); "
        "import socket\n"
        "try:\n    socket.create_connection(('example.com', 80))\nexcept PermissionError:\n    print('BLOCKED')"
    )
    completed = subprocess.run(
        [sys.executable, "-E", "-s", "-c", code, str(BACKEND_DIR)],
        capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"},
    )

    assert completed.stdout.strip() == "BLOCKED"


def test_security_dashboard_is_honest(doctor):
    status = doctor.get("/api/v1/security/status").json()
    names = {control["name"]: control for control in status["controls"]}

    for name in ("Encryption", "OTP Protection", "Consent", "Doctor MFA", "File Scan", "OCR",
                 "External AI", "PHI Logging", "Audit Chain", "Document Access", "Document Retention"):
        assert name in names

    assert names["External AI"]["status"] == "DISABLED"
    assert "100%" not in str(status)
    assert any(control["grade"] == "PRODUCTION REQUIRED" for control in status["controls"])


def test_demo_controls_refuse_proxied_requests(client):
    assert client.get("/api/v1/demo", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 404
    assert client.get("/api/v1/demo", headers={"Host": "abc.ngrok-free.app"}).status_code == 404
    assert client.get("/api/v1/demo").status_code == 200


def test_instruction_scope_does_not_reveal_lab_values(doctor, client):
    from tests.conftest import FOLLOWUP_REPORT_TEXT

    patient = new_patient()
    grant_consent(patient.ref, scopes=["INSTRUCTIONS"])
    ingest(patient.ref, make_pdf(INITIAL_REPORT_TEXT))
    ingest(patient.ref, make_pdf(FOLLOWUP_REPORT_TEXT))

    loops = doctor.get(f"/api/v1/doctor/patients/{case_of(patient.ref)}/loops").json()
    match = next(loop for loop in loops if loop["potential_matches"])["potential_matches"][0]

    assert match["observation"] is None and match["evidence"] is None
    assert "8.1" not in doctor.get(f"/api/v1/doctor/patients/{case_of(patient.ref)}/journey").text


def test_uploads_stay_in_memory():
    from starlette.formparsers import MultiPartParser

    assert MultiPartParser.spool_max_size > settings.max_upload_bytes
