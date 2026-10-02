"""
Transactions and one-time passcodes.

    txn_<random>  binds  patient + OTP verification + consent + documents

OTP rules:
    6 digits from a CSPRNG, valid 5 minutes, single use
    stored only as HMAC-SHA256(server secret, otp + transaction ref)
    at most 5 attempts, exponential back-off between attempts,
    transaction LOCKED after the 5th failure
    constant-time comparison, never logged
    one generic error for every failure
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Document, Patient, Transaction
from . import audit
from .crypto import new_ref


OTP_DIGITS = 6
OTP_TTL = timedelta(minutes=5)
TRANSACTION_TTL = timedelta(minutes=30)
MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 30

GENERIC_OTP_ERROR = "Verification failed or expired."


class VerificationFailed(Exception):
    pass


def _otp_hash(otp: str, transaction_ref: str) -> str:
    return hmac.new(
        settings.keys()["JEEVAFLOW_OTP_SECRET"],
        f"{otp}:{transaction_ref}".encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_otp() -> str:
    return str(secrets.randbelow(10**OTP_DIGITS)).zfill(OTP_DIGITS)


def create_transaction(db: Session, patient: Patient, purpose: str) -> tuple[Transaction, str]:
    """
    Returns (transaction, otp). The OTP is returned once so it can
    be delivered to the patient's registered WhatsApp number; it is
    never stored or logged in plaintext.
    """

    now = datetime.utcnow()
    otp = generate_otp()
    ref = new_ref("txn")

    transaction = Transaction(
        ref=ref,
        patient_id=patient.id,
        purpose=purpose,
        status="PENDING_VERIFICATION",
        otp_hash=_otp_hash(otp, ref),
        otp_expires_at=now + OTP_TTL,
        otp_attempts=0,
        created_at=now,
        expires_at=now + TRANSACTION_TTL,
    )

    db.add(transaction)
    db.flush()

    audit.record(
        db, "TRANSACTION_CREATED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
        object_type="TRANSACTION", object_ref=ref, reason=purpose,
    )
    audit.record(
        db, "OTP_ISSUED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
        object_type="TRANSACTION", object_ref=ref,
    )

    return transaction, otp


def transaction_state(transaction: Transaction, now: Optional[datetime] = None) -> str:
    now = now or datetime.utcnow()

    if transaction.status in {"LOCKED", "COMPLETED", "EXPIRED"}:
        return transaction.status

    if transaction.expires_at <= now:
        return "EXPIRED"

    return transaction.status


def verify_otp(db: Session, transaction_ref: str, code: str) -> Transaction:
    """
    Raises VerificationFailed (always with the same generic meaning)
    or returns the verified transaction. The caller commits.
    """

    now = datetime.utcnow()

    transaction = (
        db.query(Transaction).filter(Transaction.ref == transaction_ref).first()
        if isinstance(transaction_ref, str) and transaction_ref.startswith("txn_")
        else None
    )

    # Compute an HMAC even when the transaction does not exist, so
    # response timing does not reveal which references are real.
    candidate = _otp_hash(code or "", transaction_ref or "")

    if transaction is None:
        raise VerificationFailed()

    def fail(reason: str):
        audit.record(
            db, "OTP_FAILED", actor_type="PATIENT", object_type="TRANSACTION",
            object_ref=transaction.ref, result="DENIED", reason=reason,
        )
        db.commit()
        raise VerificationFailed()

    if transaction_state(transaction, now) != "PENDING_VERIFICATION":
        fail("NOT_PENDING")

    if transaction.otp_hash is None or transaction.otp_consumed_at is not None:
        fail("CONSUMED")

    if transaction.otp_expires_at is None or transaction.otp_expires_at <= now:
        fail("OTP_EXPIRED")

    if transaction.otp_next_attempt_at and now < transaction.otp_next_attempt_at:
        fail("BACKOFF")

    valid = (
        isinstance(code, str)
        and code.isdigit()
        and len(code) == OTP_DIGITS
        and hmac.compare_digest(candidate, transaction.otp_hash)
    )

    if not valid:
        transaction.otp_attempts += 1
        transaction.otp_next_attempt_at = now + timedelta(
            seconds=min(2 ** (transaction.otp_attempts - 1), MAX_BACKOFF_SECONDS)
        )

        if transaction.otp_attempts >= MAX_ATTEMPTS:
            transaction.status = "LOCKED"
            transaction.locked_at = now
            transaction.otp_hash = None
            audit.record(
                db, "OTP_LOCKED", actor_type="PATIENT", object_type="TRANSACTION",
                object_ref=transaction.ref, result="DENIED", reason="MAX_ATTEMPTS",
            )

        fail("WRONG_CODE")

    # Single use: the OTP is destroyed on success.
    transaction.otp_hash = None
    transaction.otp_consumed_at = now
    transaction.verified_at = now
    transaction.status = "VERIFIED"

    audit.record(
        db, "OTP_VERIFIED", actor_type="PATIENT",
        actor_ref=db.get(Patient, transaction.patient_id).ref,
        object_type="TRANSACTION", object_ref=transaction.ref,
    )

    return transaction


def expire_stale_transactions(db: Session) -> int:
    """
    Expire old transactions. Quarantined documents that never got
    verification and consent are crypto-shredded by the caller.
    """

    now = datetime.utcnow()
    stale = (
        db.query(Transaction)
        .filter(
            Transaction.expires_at <= now,
            Transaction.status.in_(["PENDING_VERIFICATION", "VERIFIED"]),
        )
        .all()
    )

    for transaction in stale:
        transaction.status = "EXPIRED"
        transaction.otp_hash = None
        audit.record(
            db, "TRANSACTION_EXPIRED", object_type="TRANSACTION", object_ref=transaction.ref,
        )

    return len(stale)


def quarantined_documents(db: Session, transaction: Transaction) -> list[Document]:
    return (
        db.query(Document)
        .filter(
            Document.transaction_id == transaction.id,
            Document.processing_status == "QUARANTINED",
        )
        .order_by(Document.id.asc())
        .all()
    )
