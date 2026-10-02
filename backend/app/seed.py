"""
Synthetic demo data: one fictional patient and demo staff accounts.

Only runs in demo mode. Passwords and TOTP secrets come from the
environment (JEEVAFLOW_DEMO_STAFF_PASSWORD, JEEVAFLOW_DEMO_TOTP_SEED),
never from source code. Get the current demo TOTP codes with:

    venv/bin/python -m app.security.demo_totp
"""

import base64
import hashlib
import hmac
from typing import Optional

from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .identity import IdentitySessionLocal, PatientIdentity, Role, StaffUser
from .models import Patient
from .patient_lookup import enroll_patient, mask_phone, normalize_phone
from .security.auth import hash_password, verify_password
from .security.crypto import identity_index, new_ref
from .security.phi_log import log_event


DEMO_PATIENT_NAME = "JeevaFlow Demo Patient (synthetic)"
DEMO_PATIENT_DOB = "2004-01-01"

DEMO_STAFF = [
    ("dr.example", "Dr. Example (synthetic)", Role.DOCTOR),
    ("dr.other", "Dr. Other (synthetic)", Role.DOCTOR),
    ("auditor", "Audit Officer (synthetic)", Role.AUDITOR),
    ("admin", "Platform Admin (synthetic)", Role.ADMIN),
]


def demo_totp_secret(username: str) -> str:
    seed = base64.b64decode(settings.demo_totp_seed)
    digest = hmac.new(seed, f"totp:{username}".encode(), hashlib.sha256).digest()[:20]
    return base64.b32encode(digest).decode().rstrip("=")


def demo_identity(idb: Session) -> Optional[PatientIdentity]:
    return idb.query(PatientIdentity).filter(PatientIdentity.synthetic.is_(True)).first()


def demo_patient(db: Session) -> Optional[Patient]:
    idb = IdentitySessionLocal()

    try:
        identity = demo_identity(idb)
    finally:
        idb.close()

    if identity is None:
        return None

    return db.query(Patient).filter(Patient.ref == identity.patient_ref).first()


def ensure_demo_patient() -> Optional[int]:
    if not settings.demo_mode:
        return None

    db = SessionLocal()
    idb = IdentitySessionLocal()

    try:
        normalized = normalize_phone(settings.demo_patient_phone)
        identity = demo_identity(idb)

        if identity is None:
            return enroll_patient(
                db, idb, DEMO_PATIENT_NAME, normalized, DEMO_PATIENT_DOB, synthetic=True
            ).id

        if normalized and identity.phone != normalized:
            identity.phone = normalized
            identity.phone_index = identity_index(normalized)
            identity.phone_masked = mask_phone(normalized)
            idb.commit()

        patient = db.query(Patient).filter(Patient.ref == identity.patient_ref).first()

        if patient is None:
            patient = Patient(ref=identity.patient_ref)
            db.add(patient)
            db.commit()

        return patient.id

    finally:
        db.close()
        idb.close()


def ensure_demo_staff() -> bool:
    if not settings.demo_mode:
        return False

    if not settings.demo_staff_password or not settings.demo_totp_seed:
        log_event("STARTUP", status="DEMO_STAFF_SKIPPED", reason="SECRETS_MISSING")
        return False

    idb = IdentitySessionLocal()

    try:
        for username, display_name, role in DEMO_STAFF:
            user = idb.query(StaffUser).filter(StaffUser.username == username).first()
            secret = demo_totp_secret(username)

            if user is None:
                idb.add(
                    StaffUser(
                        ref=new_ref("usr"),
                        username=username,
                        display_name=display_name,
                        role=role,
                        password_hash=hash_password(settings.demo_staff_password),
                        totp_secret=secret,
                        synthetic=True,
                    )
                )
                continue

            if not verify_password(settings.demo_staff_password, user.password_hash):
                user.password_hash = hash_password(settings.demo_staff_password)

            if user.totp_secret != secret:
                user.totp_secret = secret
                user.totp_last_step = None

        idb.commit()
        return True

    finally:
        idb.close()
