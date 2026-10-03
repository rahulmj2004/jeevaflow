"""
Medical data store.

Patients are referenced only by a pseudonymous patient row with an
opaque `ref`. Names, phone numbers and dates of birth live in the
separate identity database (identity.py), never here.

Free-text medical content (quotes, values, instructions, facts,
notes) is encrypted at the column level with AES-256-GCM.
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base
from .security.crypto import EncryptedText, new_case_alias


PIPELINE_VERSION = "jeevaflow-extract-2.0"


class LoopState(str, Enum):
    OPEN = "OPEN"
    OVERDUE = "OVERDUE"
    POTENTIAL_MATCH = "POTENTIAL_MATCH"
    CLOSED = "CLOSED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class DocumentSource(str, Enum):
    WEB = "WEB"
    CAMERA = "CAMERA"
    WHATSAPP = "WHATSAPP"
    DEMO = "DEMO"


class MatchStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"


class ReviewStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REVIEW = "REVIEW"
    REJECTED = "REJECTED"


class FactState(str, Enum):
    SOURCE_FACT = "SOURCE_FACT"
    AI_INFERRED = "AI_INFERRED"
    UNCERTAIN = "UNCERTAIN"
    MISSING = "MISSING"


class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    # Pseudonymous patient reference; the only patient identifier
    # used outside the identity store.
    ref = Column(String(40), unique=True, nullable=False, index=True)
    # Random case ID; the only patient identifier doctors ever see.
    case_alias = Column(String(20), unique=True, nullable=True, index=True, default=new_case_alias)
    created_at = Column(DateTime, default=datetime.utcnow)

    documents = relationship(
        "Document",
        back_populates="patient",
        cascade="all, delete-orphan",
    )

    observations = relationship(
        "Observation",
        back_populates="patient",
        cascade="all, delete-orphan",
    )

    commitments = relationship(
        "Commitment",
        back_populates="patient",
        cascade="all, delete-orphan",
    )


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    ref = Column(String(40), unique=True, nullable=False, index=True)

    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)

    # Generic label only. Uploaded file names are never stored,
    # because they often contain names or diagnoses.
    label = Column(String(80), nullable=False)
    content_type = Column(String(100), nullable=False)

    # SHA-256 of the bytes as received (integrity + deduplication)
    # and of the sanitised bytes that were encrypted and stored.
    sha256 = Column(String(64), nullable=False, index=True)
    stored_sha256 = Column(String(64), nullable=True)
    byte_size = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)

    source = Column(String(20), default=DocumentSource.WEB.value, nullable=False)

    # Opaque object name inside the private vault (ciphertext only).
    storage_key = Column(String(80), nullable=True)

    scan_status = Column(String(20), nullable=True)
    scan_engine = Column(String(60), nullable=True)
    sanitized = Column(Boolean, default=False, nullable=False)

    document_date = Column(Date, nullable=True)
    quality_status = Column(String(20), default="GOOD", nullable=False)
    quality_reason = Column(Text, nullable=True)
    # Generic, PHI-free error description.
    processing_error = Column(Text, nullable=True)
    extraction_method = Column(String(20), nullable=True)
    # PRINT | HANDWRITTEN | MIXED | UNKNOWN, decided in the worker at
    # processing time (NULL until then). Anything but PRINT needs a
    # doctor's review before its contents are trusted.
    content_kind = Column(String(16), nullable=True)
    needs_manual_review = Column(Boolean, default=False, nullable=False)

    # QUARANTINED -> PROCESSING -> PROCESSED | REJECTED | FAILED
    processing_status = Column(String(30), default="QUARANTINED", nullable=False)

    retain_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)

    patient = relationship("Patient", back_populates="documents")

    evidence = relationship(
        "SourceEvidence",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class DocumentKey(Base):
    """
    Wrapped (KEK-encrypted) data encryption key for one document.
    Destroying the wrapped key makes the ciphertext unrecoverable.
    """

    __tablename__ = "document_keys"

    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), unique=True, nullable=False)
    key_id = Column(String(60), nullable=False)
    wrapped_dek = Column(LargeBinary, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    destroyed_at = Column(DateTime, nullable=True)


class SourceEvidence(Base):
    __tablename__ = "source_evidence"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)

    quote = Column(EncryptedText(), nullable=False)
    page_number = Column(Integer, nullable=True)
    start_position = Column(Integer, nullable=True)
    end_position = Column(Integer, nullable=True)

    # Bounding box as fractions of the page (0..1), when located.
    bbox_x = Column(Float, nullable=True)
    bbox_y = Column(Float, nullable=True)
    bbox_w = Column(Float, nullable=True)
    bbox_h = Column(Float, nullable=True)

    confidence = Column(Float, nullable=True)
    document_sha256 = Column(String(64), nullable=True)
    pipeline_version = Column(String(40), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    document = relationship("Document", back_populates="evidence")


class Observation(Base):
    __tablename__ = "observations"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)

    observation_type = Column(String(100), nullable=False)
    value = Column(EncryptedText(), nullable=False)
    unit = Column(String(50), nullable=True)
    event_date = Column(Date, nullable=True)

    evidence_id = Column(Integer, ForeignKey("source_evidence.id"), nullable=True)

    review_status = Column(String(20), default=ReviewStatus.REVIEW.value, nullable=False)
    fact_state = Column(String(20), default=FactState.SOURCE_FACT.value, nullable=False)
    fact_note = Column(String(200), nullable=True)
    # Drift-detector result (JSON). Encrypted: findings quote the source.
    drift = Column(EncryptedText(), nullable=True)
    # Follow-through engine: embedding of the result LABEL only (never
    # the value), "<model id>|<base64 float16>".
    embedding = Column(EncryptedText(), nullable=True)
    confidence = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    patient = relationship("Patient", back_populates="observations")


class Commitment(Base):
    __tablename__ = "commitments"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)

    instruction = Column(EncryptedText(), nullable=False)
    due_date = Column(Date, nullable=True)
    # Follow-through engine: embedding of the instruction's test
    # concept ("<model id>|<base64 float16>"), computed in the worker.
    # NULL when the instruction names no test or the model was absent.
    embedding = Column(EncryptedText(), nullable=True)
    state = Column(String(30), default=LoopState.OPEN.value, nullable=False)

    evidence_id = Column(Integer, ForeignKey("source_evidence.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    patient = relationship("Patient", back_populates="commitments")


class ClinicalFact(Base):
    """
    A structured fact read from a document (medication, allergy,
    prescriber). Every field carries a state:
    SOURCE_FACT | AI_INFERRED | UNCERTAIN | MISSING.
    """

    __tablename__ = "clinical_facts"

    id = Column(Integer, primary_key=True)
    ref = Column(String(40), unique=True, nullable=False, index=True)

    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    evidence_id = Column(Integer, ForeignKey("source_evidence.id"), nullable=True)

    # MEDICATION | ALLERGY | PRESCRIBER
    category = Column(String(20), nullable=False)
    label = Column(EncryptedText(), nullable=False)
    # JSON: {field: {value, state, source_text, note}}
    fields = Column(EncryptedText(), nullable=False)

    state = Column(String(20), nullable=False)
    confidence = Column(Float, nullable=True)
    pipeline_version = Column(String(40), nullable=True)
    # Drift-detector result (JSON). Encrypted: findings quote the source.
    drift = Column(EncryptedText(), nullable=True)

    # REVIEW | CONFIRMED | REJECTED  (doctor is the final gate)
    review_status = Column(String(20), default="REVIEW", nullable=False)
    reviewed_by_ref = Column(String(40), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LoopMatch(Base):
    """
    A rule-based link between an Open Loop and a newer
    source-backed observation that may fulfil it. A match never
    closes a loop by itself.
    """

    __tablename__ = "loop_matches"

    id = Column(Integer, primary_key=True, index=True)
    commitment_id = Column(Integer, ForeignKey("commitments.id"), nullable=False, index=True)
    observation_id = Column(Integer, ForeignKey("observations.id"), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    evidence_id = Column(Integer, ForeignKey("source_evidence.id"), nullable=False)

    rule = Column(Text, nullable=False)
    # RULE (keyword table) | AI_SEMANTIC (follow-through engine)
    method = Column(String(20), default="RULE", nullable=False)
    score = Column(Float, nullable=True)
    # AI_SEMANTIC only: JSON with model, score, rank, threshold and the
    # other candidates considered. Encrypted: holds result labels.
    explanation = Column(EncryptedText(), nullable=True)
    status = Column(String(20), default=MatchStatus.PENDING.value, nullable=False)
    reviewed_by = Column(String(200), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LoopEvent(Base):
    """
    Open Loop state history (system and human decisions).
    """

    __tablename__ = "loop_events"

    id = Column(Integer, primary_key=True, index=True)
    commitment_id = Column(Integer, ForeignKey("commitments.id"), nullable=False, index=True)
    action = Column(String(50), nullable=False)
    from_state = Column(String(30), nullable=True)
    to_state = Column(String(30), nullable=True)
    actor_type = Column(String(20), nullable=False)
    actor_name = Column(String(200), nullable=True)
    match_id = Column(Integer, ForeignKey("loop_matches.id"), nullable=True)
    note = Column(EncryptedText(), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ============================================================
# TRANSACTIONS, CONSENT, MESSAGING
# ============================================================

class Transaction(Base):
    """
    Binds one patient interaction together:
    verification (OTP) + consent + documents. Expires automatically.
    """

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True)
    ref = Column(String(40), unique=True, nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)

    # UPLOAD | PORTAL_ACCESS
    purpose = Column(String(20), nullable=False)
    # PENDING_VERIFICATION | VERIFIED | CONSENTED | COMPLETED
    # | EXPIRED | LOCKED
    status = Column(String(30), nullable=False)

    # HMAC-SHA256(server secret, otp + transaction ref). Never the OTP.
    otp_hash = Column(String(64), nullable=True)
    otp_expires_at = Column(DateTime, nullable=True)
    otp_attempts = Column(Integer, default=0, nullable=False)
    otp_next_attempt_at = Column(DateTime, nullable=True)
    otp_consumed_at = Column(DateTime, nullable=True)

    consent_id = Column(Integer, ForeignKey("consents.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    verified_at = Column(DateTime, nullable=True)
    locked_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class Consent(Base):
    __tablename__ = "consents"

    id = Column(Integer, primary_key=True)
    ref = Column(String(40), unique=True, nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)

    # Staff user reference from the identity store.
    doctor_ref = Column(String(40), nullable=False, index=True)
    doctor_name = Column(String(120), nullable=False)

    purpose = Column(String(40), nullable=False)
    # Comma-separated scopes, e.g. MEDICATIONS,LABS,ALLERGIES
    scopes = Column(String(200), nullable=False)

    retention_days = Column(Integer, nullable=False)

    granted_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    # Set once the natural expiry has been written to the audit log.
    expiry_recorded = Column(Boolean, default=False, nullable=False)

    transaction_id = Column(Integer, nullable=True)


class WhatsAppMessage(Base):
    """
    Inbound WhatsApp log. Masked sender, status codes and a reply
    template key only. Never message bodies, OTPs or medical text.
    """

    __tablename__ = "whatsapp_messages"

    id = Column(Integer, primary_key=True, index=True)
    message_sid = Column(String(64), nullable=True, index=True)
    sender_masked = Column(String(30), nullable=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)
    transaction_ref = Column(String(40), nullable=True)
    num_media = Column(Integer, default=0, nullable=False)
    status = Column(String(30), nullable=False)
    detail = Column(Text, nullable=True)
    document_ids = Column(String(255), nullable=True)
    reply_kind = Column(String(40), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProcessedMessage(Base):
    """
    Replay protection: every Twilio MessageSid is accepted once.
    """

    __tablename__ = "processed_messages"

    message_sid = Column(String(64), primary_key=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OutboundMessage(Base):
    """
    Generic workflow notifications sent to the patient. The body is
    rendered from a fixed template; nothing medical is stored.
    """

    __tablename__ = "outbound_messages"

    id = Column(Integer, primary_key=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=True, index=True)
    transaction_ref = Column(String(40), nullable=True)
    kind = Column(String(40), nullable=False)
    # SENT | SIMULATED | FAILED
    delivery_status = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EvidenceToken(Base):
    """
    Short-lived, single-use token for viewing one evidence region.
    Bound to the issuing staff session and to one document.
    """

    __tablename__ = "evidence_tokens"

    id = Column(Integer, primary_key=True)
    token_hash = Column(String(64), unique=True, nullable=False)
    session_hash = Column(String(64), nullable=False)
    user_ref = Column(String(40), nullable=False)
    patient_id = Column(Integer, nullable=False)
    document_id = Column(Integer, nullable=False)
    evidence_id = Column(Integer, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AuditEvent(Base):
    """
    Tamper-evident, append-only audit chain. Contains references
    and codes only, never PHI.
    """

    __tablename__ = "audit_events"

    seq = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(String(32), nullable=False)
    actor_type = Column(String(20), nullable=False)
    actor_ref = Column(String(60), nullable=True)
    action = Column(String(60), nullable=False)
    object_type = Column(String(30), nullable=True)
    object_ref = Column(String(60), nullable=True)
    result = Column(String(20), nullable=False)
    reason = Column(String(60), nullable=True)
    prev_hash = Column(String(64), nullable=False)
    event_hash = Column(String(64), nullable=False)
    mac = Column(String(64), nullable=False)


Index("ix_documents_patient_sha256", Document.patient_id, Document.sha256)
