"""
Identity store (separate database from medical data).

    phone / name / date of birth  <->  pseudonymous patient ref
    staff accounts, password hashes, MFA secrets, sessions

Identity fields are encrypted with a key derived from
JEEVAFLOW_IDENTITY_KEY, which is independent of the medical KEK.
Phone numbers are found through an HMAC blind index, never by
plaintext comparison.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .config import settings
from .security.crypto import EncryptedText, identity_index


identity_engine = create_engine(
    settings.identity_database_url,
    connect_args={"check_same_thread": False},
)

IdentitySessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=identity_engine,
)

IdentityBase = declarative_base()


def get_identity_db():
    db = IdentitySessionLocal()

    try:
        yield db
    finally:
        db.close()


class Role:
    PATIENT = "PATIENT"
    DOCTOR = "DOCTOR"
    ADMIN = "ADMIN"
    AUDITOR = "AUDITOR"
    SERVICE = "SERVICE"

    STAFF = {DOCTOR, ADMIN, AUDITOR}


class PatientIdentity(IdentityBase):
    __tablename__ = "patient_identities"

    id = Column(Integer, primary_key=True)
    patient_ref = Column(String(40), unique=True, nullable=False, index=True)

    phone_index = Column(String(64), unique=True, nullable=True, index=True)
    phone = Column(EncryptedText("identity"), nullable=True)
    phone_masked = Column(String(30), nullable=True)
    name = Column(EncryptedText("identity"), nullable=False)
    date_of_birth = Column(EncryptedText("identity"), nullable=True)

    synthetic = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class StaffUser(IdentityBase):
    __tablename__ = "staff_users"

    id = Column(Integer, primary_key=True)
    ref = Column(String(40), unique=True, nullable=False, index=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    display_name = Column(String(120), nullable=False)
    role = Column(String(20), nullable=False)

    password_hash = Column(String(200), nullable=False)
    totp_secret = Column(EncryptedText("identity"), nullable=False)
    # Last accepted TOTP time step (blocks code reuse).
    totp_last_step = Column(Integer, nullable=True)

    failed_logins = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    synthetic = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AuthSession(IdentityBase):
    """
    Server-side session. Only an HMAC of the bearer token is stored.

    kind: STAFF_MFA_PENDING | STAFF | PATIENT
    """

    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    csrf_hash = Column(String(64), nullable=True)

    kind = Column(String(30), nullable=False)
    subject_ref = Column(String(40), nullable=False)
    role = Column(String(20), nullable=False)
    transaction_ref = Column(String(40), nullable=True)
    mfa_verified = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)


def init_identity_db():
    IdentityBase.metadata.create_all(bind=identity_engine)


# ============================================================
# PATIENT IDENTITY
# ============================================================

def find_patient_ref_by_phone(db: Session, normalized_phone: str) -> Optional[str]:
    identity = (
        db.query(PatientIdentity)
        .filter(PatientIdentity.phone_index == identity_index(normalized_phone))
        .first()
    )

    return identity.patient_ref if identity else None


def get_identity(db: Session, patient_ref: str) -> Optional[PatientIdentity]:
    return (
        db.query(PatientIdentity)
        .filter(PatientIdentity.patient_ref == patient_ref)
        .first()
    )
