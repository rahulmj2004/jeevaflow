"""
Retention and clean-up (runs at startup and every few minutes).

    expired transactions        -> unverified quarantined documents
                                   are crypto-shredded
    documents past retain_until -> crypto-shredded
    consents past expiry        -> CONSENT_EXPIRED audited once
    evidence tokens, sessions   -> expired rows removed
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import SessionLocal
from .identity import AuthSession, IdentitySessionLocal
from .models import Consent, Document, EvidenceToken, Transaction
from .pipeline import delete_document
from .security import audit
from .security.otp import expire_stale_transactions


def run_retention(db: Session) -> dict:
    now = datetime.utcnow()
    counts = {"transactions_expired": expire_stale_transactions(db)}
    db.commit()

    unverified = (
        db.query(Document)
        .join(Transaction, Document.transaction_id == Transaction.id)
        .filter(
            Document.processing_status == "QUARANTINED",
            Transaction.status.in_(["EXPIRED", "LOCKED"]),
        )
        .all()
    )

    for document in unverified:
        delete_document(db, document, "SYSTEM", "retention", "UNVERIFIED_EXPIRED")

    counts["unverified_documents_deleted"] = len(unverified)

    expired_documents = db.query(Document).filter(Document.retain_until <= now).all()

    for document in expired_documents:
        delete_document(db, document, "SYSTEM", "retention", "RETENTION")

    counts["retention_deleted"] = len(expired_documents)

    consents = (
        db.query(Consent)
        .filter(Consent.expires_at <= now, Consent.revoked_at.is_(None), Consent.expiry_recorded.is_(False))
        .all()
    )

    for consent in consents:
        consent.expiry_recorded = True
        audit.record(db, "CONSENT_EXPIRED", object_type="CONSENT", object_ref=consent.ref, reason="TIME")

    counts["consents_expired"] = len(consents)

    db.query(EvidenceToken).filter(EvidenceToken.expires_at <= now - timedelta(minutes=5)).delete(
        synchronize_session=False
    )
    db.commit()

    idb = IdentitySessionLocal()

    try:
        idb.query(AuthSession).filter(AuthSession.expires_at <= now - timedelta(hours=1)).delete(
            synchronize_session=False
        )
        idb.commit()
    finally:
        idb.close()

    return counts


def run_retention_job() -> dict:
    db = SessionLocal()

    try:
        return run_retention(db)
    finally:
        db.close()
