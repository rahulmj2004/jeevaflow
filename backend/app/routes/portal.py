"""
Patient portal: OTP verification, consent, documents.

A patient session exists only after a WhatsApp transaction's OTP is
verified. It is bound to that patient and transaction, lasts 15
minutes, and can only act on that patient's own consents and
documents. Quarantined documents are read (OCR/extraction) only
after an active consent covers them.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..identity import Role, StaffUser, get_identity, get_identity_db
from ..models import Consent, Document, DocumentSource, Patient, Transaction
from ..pipeline import GENERIC_REJECTION, delete_document, ingest_document, process_document
from ..security import audit, ratelimit
from ..security.auth import (
    PATIENT_COOKIE,
    PATIENT_SESSION_TTL,
    PatientPrincipal,
    clear_cookie,
    create_session,
    csrf_for,
    current_patient,
    revoke_sessions,
    set_session_cookie,
)
from ..security.consent import (
    ALLOWED_DURATIONS_DAYS,
    DEFAULT_RETENTION_DAYS,
    PURPOSE_CLINICAL_REVIEW,
    PURPOSES,
    SCOPES,
    active_consent,
    consent_status,
    grant,
    serialize_consent,
)
from ..security.otp import GENERIC_OTP_ERROR, VerificationFailed, quarantined_documents, transaction_state, verify_otp
from ..storage import FileValidationError
from ..whatsapp import send_notification
from .deps import client_ip, limit


router = APIRouter(prefix="/api/v1/portal", tags=["patient portal"])


class VerifyRequest(BaseModel):
    transaction_ref: str = Field(..., min_length=8, max_length=40)
    code: str = Field(..., min_length=1, max_length=8)


class ConsentRequest(BaseModel):
    doctor_ref: str = Field(..., min_length=4, max_length=40)
    scopes: list[str] = Field(..., min_length=1, max_length=len(SCOPES))
    duration_days: int
    purpose: str = PURPOSE_CLINICAL_REVIEW


def _patient(db: Session, principal: PatientPrincipal) -> Patient:
    patient = db.query(Patient).filter(Patient.ref == principal.patient_ref).first()

    if patient is None:
        raise HTTPException(status_code=401, detail="Authentication required.")

    return patient


def _transaction(db: Session, principal: PatientPrincipal, patient: Patient) -> Optional[Transaction]:
    if not principal.transaction_ref:
        return None

    return (
        db.query(Transaction)
        .filter(Transaction.ref == principal.transaction_ref, Transaction.patient_id == patient.id)
        .first()
    )


def _own_consent(db: Session, patient: Patient, ref: str) -> Consent:
    consent = db.query(Consent).filter(Consent.ref == ref, Consent.patient_id == patient.id).first()

    if consent is None:
        raise HTTPException(status_code=404, detail="Consent not found.")

    return consent


def _doctors(idb: Session) -> list[dict]:
    return [
        {"ref": user.ref, "display_name": user.display_name}
        for user in idb.query(StaffUser)
        .filter(StaffUser.role == Role.DOCTOR, StaffUser.active.is_(True))
        .order_by(StaffUser.display_name.asc())
        .all()
    ]


def _documents(db: Session, patient: Patient) -> list[dict]:
    return [
        {
            "ref": document.ref,
            "label": document.label,
            "source": document.source,
            "status": document.processing_status,
            "received_at": document.created_at,
            "retain_until": document.retain_until,
            "scan_status": document.scan_status,
            "content_kind": document.content_kind,
            "needs_manual_review": document.needs_manual_review,
        }
        for document in db.query(Document)
        .filter(Document.patient_id == patient.id)
        .order_by(Document.created_at.desc())
        .all()
    ]


def _process_transaction(db: Session, idb: Session, patient: Patient, transaction: Transaction, consent: Consent) -> dict:
    documents = quarantined_documents(db, transaction)
    summary = {
        "documents": 0, "processed": 0, "facts": 0, "observations": 0, "instructions": 0, "failed": 0,
        "needs_manual_review": 0,
    }

    transaction.consent_id = consent.id
    transaction.status = "CONSENTED"
    db.commit()

    for document in documents:
        result = process_document(db, document, actor_ref=patient.ref)
        summary["documents"] += 1

        if result.status == "PROCESSED":
            summary["processed"] += 1
            summary["facts"] += result.facts_created
            summary["observations"] += result.observations_created
            summary["instructions"] += result.open_loops_created
            summary["needs_manual_review"] += int(result.document.needs_manual_review)
        else:
            summary["failed"] += 1

    transaction.status = "COMPLETED"
    transaction.completed_at = datetime.utcnow()

    if summary["processed"]:
        send_notification(db, idb, patient, "PROCESSED", transaction.ref)

    if summary["needs_manual_review"]:
        send_notification(db, idb, patient, "HANDWRITTEN_RECEIVED", transaction.ref)

    db.commit()

    return summary


# ============================================================
# VERIFY
# ============================================================

@router.post("/verify")
def verify(
    body: VerifyRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    limit("otp_verify_ip", client_ip(request))

    try:
        transaction = verify_otp(db, body.transaction_ref, body.code)
    except VerificationFailed:
        if not body.transaction_ref.startswith("txn_") or not db.query(Transaction.id).filter(
            Transaction.ref == body.transaction_ref
        ).first():
            audit.record_now("OTP_FAILED", actor_type="PATIENT", result="DENIED", reason="UNKNOWN_TRANSACTION")
        raise HTTPException(status_code=401, detail=GENERIC_OTP_ERROR)

    patient = db.get(Patient, transaction.patient_id)
    db.commit()

    # One live portal session per patient.
    revoke_sessions(idb, patient.ref, "PATIENT")

    token = create_session(
        idb, "PATIENT", patient.ref, Role.PATIENT, PATIENT_SESSION_TTL, transaction_ref=transaction.ref
    )
    set_session_cookie(response, PATIENT_COOKIE, token, PATIENT_SESSION_TTL)

    return {"verified": True, "csrf_token": csrf_for(token)}


# ============================================================
# SESSION OVERVIEW
# ============================================================

@router.get("/session")
def session_overview(
    request: Request,
    principal: PatientPrincipal = Depends(current_patient),
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    limit("portal_ip", client_ip(request))

    patient = _patient(db, principal)
    transaction = _transaction(db, principal, patient)
    identity = get_identity(idb, patient.ref)

    consents = db.query(Consent).filter(Consent.patient_id == patient.id).order_by(Consent.granted_at.desc()).all()

    return {
        "csrf_token": csrf_for(request.cookies.get(PATIENT_COOKIE)),
        "patient": {
            "ref": patient.ref,
            # The pseudonym the doctor sees, so the patient can show it
            # at the visit. Grants nothing on its own: doctor access
            # still requires this patient's active consent.
            "case_alias": patient.case_alias,
            "name": identity.name if identity else None,
            "phone_masked": identity.phone_masked if identity else None,
        },
        "transaction": (
            {
                "ref": transaction.ref,
                "purpose": transaction.purpose,
                "status": transaction_state(transaction),
                "expires_at": transaction.expires_at,
                "pending_documents": len(quarantined_documents(db, transaction)),
            }
            if transaction
            else None
        ),
        "consents": [serialize_consent(consent) for consent in consents],
        "documents": _documents(db, patient),
        "options": {
            "doctors": _doctors(idb),
            "scopes": [{"key": key, "label": label} for key, label in SCOPES.items()],
            "durations_days": list(ALLOWED_DURATIONS_DAYS),
            "purposes": [{"key": key, "label": label} for key, label in PURPOSES.items()],
            "retention_days": DEFAULT_RETENTION_DAYS,
        },
    }


# ============================================================
# CONSENT
# ============================================================

@router.post("/consents")
def grant_consent(
    body: ConsentRequest,
    principal: PatientPrincipal = Depends(current_patient),
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    patient = _patient(db, principal)

    doctor = (
        idb.query(StaffUser)
        .filter(StaffUser.ref == body.doctor_ref, StaffUser.role == Role.DOCTOR, StaffUser.active.is_(True))
        .first()
    )

    if doctor is None:
        raise HTTPException(status_code=422, detail="Select a doctor from the list.")

    transaction = _transaction(db, principal, patient)

    consent = grant(
        db, patient, doctor.ref, doctor.display_name, body.scopes, body.duration_days, body.purpose,
        transaction_id=transaction.id if transaction else None,
    )
    db.commit()

    summary = None

    if transaction is not None and transaction_state(transaction) == "VERIFIED":
        summary = _process_transaction(db, idb, patient, transaction, consent)

    return {"consent": serialize_consent(consent), "processing": summary}


@router.post("/consents/{consent_ref}/apply")
def apply_existing_consent(
    consent_ref: str,
    principal: PatientPrincipal = Depends(current_patient),
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    """
    Share this transaction's documents under an existing active consent.
    """

    patient = _patient(db, principal)
    consent = _own_consent(db, patient, consent_ref)

    if consent_status(consent) != "ACTIVE":
        raise HTTPException(status_code=409, detail="This consent is no longer active.")

    transaction = _transaction(db, principal, patient)

    if transaction is None or transaction_state(transaction) != "VERIFIED":
        raise HTTPException(status_code=409, detail="There are no documents waiting for consent.")

    return {"consent": serialize_consent(consent), "processing": _process_transaction(db, idb, patient, transaction, consent)}


@router.post("/consents/{consent_ref}/revoke")
def revoke_consent(
    consent_ref: str,
    principal: PatientPrincipal = Depends(current_patient),
    db: Session = Depends(get_db),
):
    patient = _patient(db, principal)
    consent = _own_consent(db, patient, consent_ref)

    if consent.revoked_at is None:
        consent.revoked_at = datetime.utcnow()
        audit.record(
            db, "CONSENT_REVOKED", actor_type="PATIENT", actor_ref=patient.ref,
            object_type="CONSENT", object_ref=consent.ref,
        )
        db.commit()

    return serialize_consent(consent)


@router.post("/consents/{consent_ref}/expire")
def expire_consent(
    consent_ref: str,
    principal: PatientPrincipal = Depends(current_patient),
    db: Session = Depends(get_db),
):
    """
    End the consent now by setting its expiry to the current time.
    """

    patient = _patient(db, principal)
    consent = _own_consent(db, patient, consent_ref)

    if consent_status(consent) == "ACTIVE":
        consent.expires_at = datetime.utcnow()
        audit.record(
            db, "CONSENT_EXPIRED", actor_type="PATIENT", actor_ref=patient.ref,
            object_type="CONSENT", object_ref=consent.ref, reason="PATIENT_ENDED",
        )
        db.commit()

    return serialize_consent(consent)


# ============================================================
# DOCUMENTS
# ============================================================

@router.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    camera: bool = False,
    principal: PatientPrincipal = Depends(current_patient),
    db: Session = Depends(get_db),
):
    """
    Portal upload. Requires an active consent, because the document
    is read immediately after it is accepted.
    """

    patient = _patient(db, principal)
    limit("upload_patient", patient.ref)

    if active_consent(db, patient.id) is None:
        raise HTTPException(status_code=403, detail="Grant consent before sharing documents.")

    ip = client_ip(request)

    def rejected():
        # One response for every rejection reason (and for lockout).
        try:
            ratelimit.hit("upload_reject_ip", ip)
        except ratelimit.RateLimited:
            pass
        return HTTPException(status_code=400, detail=GENERIC_REJECTION)

    if ratelimit.exhausted("upload_reject_ip", ip):
        audit.record_now(
            "DOCUMENT_REJECTED", actor_type="PATIENT", actor_ref=patient.ref,
            object_type="CASE", object_ref=patient.case_alias, result="DENIED", reason="UPLOAD_LOCKED_IP",
        )
        raise HTTPException(status_code=400, detail=GENERIC_REJECTION)

    declared_length = request.headers.get("content-length")

    if declared_length and declared_length.isdigit() and int(declared_length) > settings.max_upload_bytes + 64 * 1024:
        raise rejected()

    content = await file.read(settings.max_upload_bytes + 1)

    try:
        result = await run_in_threadpool(
            ingest_document,
            db,
            patient,
            content,
            file.content_type,
            DocumentSource.CAMERA.value if camera else DocumentSource.WEB.value,
            None,
            patient.ref,
        )
    except FileValidationError:
        raise rejected()
    finally:
        del content

    if result.status == "FAILED":
        raise HTTPException(status_code=422, detail=result.error)

    return result.to_dict(db)


@router.get("/documents")
def list_documents(principal: PatientPrincipal = Depends(current_patient), db: Session = Depends(get_db)):
    return _documents(db, _patient(db, principal))


@router.delete("/documents/{document_ref}")
def remove_document(
    document_ref: str,
    principal: PatientPrincipal = Depends(current_patient),
    db: Session = Depends(get_db),
):
    patient = _patient(db, principal)

    document = (
        db.query(Document)
        .filter(Document.ref == document_ref, Document.patient_id == patient.id)
        .first()
    )

    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    delete_document(db, document, "PATIENT", patient.ref, "PATIENT_REQUEST")
    db.commit()

    return {"deleted": True}


@router.post("/logout")
def logout(
    response: Response,
    principal: PatientPrincipal = Depends(current_patient),
    idb: Session = Depends(get_identity_db),
):
    revoke_sessions(idb, principal.patient_ref, "PATIENT")
    clear_cookie(response, PATIENT_COOKIE)

    return {"logged_out": True}
