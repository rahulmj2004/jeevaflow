"""
Patient consent and server-side access decisions.

A doctor may read a patient's data only when ALL hold, checked on
every request:

    authenticated staff session with MFA
    role DOCTOR
    an active consent from this patient to this doctor
    for this purpose, not expired and not revoked
    covering the scope being read
    no bulk-access anomaly for this doctor

Unknown patients and missing consent produce the same generic 403,
so the API cannot be used to discover which patients exist.
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Consent, Patient
from . import audit, ratelimit
from .auth import GENERIC_FORBIDDEN, StaffPrincipal


PURPOSE_CLINICAL_REVIEW = "CLINICAL_REVIEW"

PURPOSES = {PURPOSE_CLINICAL_REVIEW: "Clinical review"}

SCOPES = {
    "DEMOGRAPHICS": "Name and date of birth",
    "MEDICATIONS": "Medications (name, dose, frequency, duration)",
    "LABS": "Laboratory results",
    "ALLERGIES": "Allergies",
    "INSTRUCTIONS": "Doctor instructions and follow-ups",
    "SOURCE_DOCUMENTS": "View the original document regions",
}

ALLOWED_DURATIONS_DAYS = (1, 7, 30)
DEFAULT_RETENTION_DAYS = 90

# Bulk-access anomaly: distinct patients per doctor per hour.
ANOMALY_DISTINCT_PATIENTS = 25
ANOMALY_WINDOW_SECONDS = 3600


def consent_status(consent: Consent, now: Optional[datetime] = None) -> str:
    now = now or datetime.utcnow()

    if consent.revoked_at is not None:
        return "REVOKED"

    if consent.expires_at <= now:
        return "EXPIRED"

    return "ACTIVE"


def scopes_of(consent: Consent) -> set[str]:
    return {item for item in consent.scopes.split(",") if item}


def grant(
    db: Session,
    patient: Patient,
    doctor_ref: str,
    doctor_name: str,
    scopes: list[str],
    duration_days: int,
    purpose: str = PURPOSE_CLINICAL_REVIEW,
    transaction_id: Optional[int] = None,
) -> Consent:
    from .crypto import new_ref

    clean = sorted({scope for scope in scopes if scope in SCOPES})

    if not clean:
        raise HTTPException(status_code=422, detail="Select at least one information scope.")

    if duration_days not in ALLOWED_DURATIONS_DAYS:
        raise HTTPException(status_code=422, detail="Unsupported consent duration.")

    if purpose not in PURPOSES:
        raise HTTPException(status_code=422, detail="Unsupported purpose.")

    now = datetime.utcnow()

    consent = Consent(
        ref=new_ref("cns"),
        patient_id=patient.id,
        doctor_ref=doctor_ref,
        doctor_name=doctor_name,
        purpose=purpose,
        scopes=",".join(clean),
        retention_days=DEFAULT_RETENTION_DAYS,
        granted_at=now,
        expires_at=now + timedelta(days=duration_days),
        transaction_id=transaction_id,
    )

    db.add(consent)
    db.flush()

    audit.record(
        db, "CONSENT_GRANTED", actor_type="PATIENT", actor_ref=patient.ref,
        object_type="CONSENT", object_ref=consent.ref,
    )

    return consent


def active_consent(
    db: Session,
    patient_id: int,
    doctor_ref: Optional[str] = None,
    purpose: str = PURPOSE_CLINICAL_REVIEW,
) -> Optional[Consent]:
    now = datetime.utcnow()

    query = db.query(Consent).filter(
        Consent.patient_id == patient_id,
        Consent.purpose == purpose,
        Consent.revoked_at.is_(None),
        Consent.expires_at > now,
    )

    if doctor_ref is not None:
        query = query.filter(Consent.doctor_ref == doctor_ref)

    return query.order_by(Consent.granted_at.desc()).first()


def authorize_doctor(
    db: Session,
    principal: StaffPrincipal,
    patient_ref: str,
    scope: Optional[str] = None,
    purpose: str = PURPOSE_CLINICAL_REVIEW,
) -> tuple[Patient, Consent]:
    def deny(reason: str):
        audit.record_now(
            "DOCTOR_ACCESS_DENIED", actor_type="STAFF", actor_ref=principal.user_ref,
            object_type="PATIENT", object_ref=patient_ref if patient_ref.startswith("pt_") else None,
            result="DENIED", reason=reason,
        )
        raise HTTPException(status_code=403, detail=GENERIC_FORBIDDEN)

    if principal.role != "DOCTOR":
        deny("ROLE")

    try:
        ratelimit.hit("doctor_api_user", principal.user_ref)
    except ratelimit.RateLimited:
        audit.record_now(
            "DOCTOR_ACCESS_DENIED", actor_type="STAFF", actor_ref=principal.user_ref,
            result="DENIED", reason="RATE_LIMIT",
        )
        raise

    patient = db.query(Patient).filter(Patient.ref == patient_ref).first()

    if patient is None:
        deny("NO_CONSENT")

    consent = active_consent(db, patient.id, principal.user_ref, purpose)

    if consent is None:
        deny("NO_CONSENT")

    if scope is not None and scope not in scopes_of(consent):
        deny("SCOPE")

    distinct = ratelimit.distinct_count(
        "doctor_patients", principal.user_ref, patient.ref, ANOMALY_WINDOW_SECONDS
    )

    if distinct > ANOMALY_DISTINCT_PATIENTS:
        audit.record_now(
            "ANOMALY_DETECTED", actor_type="STAFF", actor_ref=principal.user_ref,
            result="DENIED", reason="BULK_PATIENT_ACCESS",
        )
        deny("ANOMALY")

    return patient, consent


def serialize_consent(consent: Consent) -> dict:
    return {
        "ref": consent.ref,
        "doctor_ref": consent.doctor_ref,
        "doctor_name": consent.doctor_name,
        "purpose": consent.purpose,
        "purpose_label": PURPOSES.get(consent.purpose, consent.purpose),
        "scopes": sorted(scopes_of(consent)),
        "retention_days": consent.retention_days,
        "granted_at": consent.granted_at,
        "expires_at": consent.expires_at,
        "revoked_at": consent.revoked_at,
        "status": consent_status(consent),
    }
