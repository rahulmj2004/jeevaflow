"""
Secure ingestion gateway and the single processing pipeline.

Web uploads (patient portal) and WhatsApp media both go through
receive_document() and process_document(). There is no second path.

receive_document()   - caller has authenticated the transaction or
                       patient session
    validate size / declared type / magic bytes
    -> isolated worker: structure, page count, dimensions, active
       PDF content, decompression bombs; sanitise (PDF rebuild,
       image re-encode without EXIF)
    -> malware scan (original and sanitised bytes)
    -> SHA-256 integrity hash, per-patient deduplication
    -> AES-256-GCM with a fresh DEK -> private vault
    -> Document QUARANTINED (nothing is read from it yet)

process_document()   - only after OTP verification AND active consent
    decrypt in memory -> isolated worker (OCR / extraction)
    -> quote-or-reject provenance (page, bbox, confidence, document
       hash, pipeline version)
    -> lab observations, instructions, medication/allergy facts
       with SOURCE_FACT / AI_INFERRED / UNCERTAIN / MISSING states
    -> potential Open Loop matches (never closes anything)
    -> plaintext discarded
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from . import vault
from .config import settings
from .facts import OCR_CONFIDENCE_THRESHOLD
from .loops import serialize_match
from .matching import detect_potential_matches
from .models import (
    PIPELINE_VERSION,
    ClinicalFact,
    Commitment,
    Document,
    DocumentSource,
    FactState,
    LoopState,
    Observation,
    Patient,
    ReviewStatus,
    SourceEvidence,
    Transaction,
)
from .provenance import locate_quote
from .security import audit, scanner
from .security.crypto import new_ref
from .storage import FileValidationError, sha256_of, validate_file
from .worker_client import WorkerError, decode, run_worker


REJECTION_MESSAGES = {
    "EMPTY_FILE": "The file is empty.",
    "FILE_TOO_LARGE": "The file is too large.",
    "UNSUPPORTED_TYPE": "Only PDF, JPG and PNG files are supported.",
    "CONTENT_MISMATCH": "The file content does not match its declared type.",
    "CORRUPTED_FILE": "The file is damaged or could not be read.",
    "SUSPICIOUS_PDF": "The PDF contains active content and was rejected.",
    "ENCRYPTED_PDF": "Password-protected PDFs are not accepted.",
    "TOO_MANY_PAGES": "The document has too many pages.",
    "INVALID_DIMENSIONS": "The image dimensions are not accepted.",
    "DECOMPRESSION_BOMB": "The image is too large to process safely.",
    "ANIMATED_IMAGE": "Animated images are not accepted.",
    "MALWARE_DETECTED": "The file failed the security scan.",
    "WORKER_ERROR": "The file could not be validated.",
    "WORKER_TIMEOUT": "The file could not be validated in time.",
}

GENERIC_FAILURE = "The document could not be processed reliably."

# Fixed labels for detected PDF features (app/security/pdf_active_content.py).
PDF_FEATURE_LABELS = {
    "JAVASCRIPT": "JavaScript",
    "OPEN_ACTION": "an automatic open action",
    "ADDITIONAL_ACTIONS": "event-triggered actions",
    "LAUNCH_ACTION": "a launch action",
    "REMOTE_ACTION": "a remote/external action",
    "RICH_MEDIA": "rich media",
    "XFA_FORM": "a dynamic XFA form",
    "EMBEDDED_EXECUTABLE": "an embedded executable",
    "EMBEDDED_FILE": "an embedded file",
}


class ExtractionFailed(Exception):
    pass


def run_inspection(content: bytes, content_type: str) -> dict:
    return run_worker("inspect", content, content_type, max_pages=settings.max_pdf_pages)


def run_extraction(content: bytes, content_type: str, received_on: date) -> dict:
    return run_worker("extract", content, content_type, received_on=received_on.isoformat())


@dataclass
class IngestionResult:
    # RECEIVED | PROCESSED | DUPLICATE | RETAKE | FAILED
    status: str
    document: Document
    duplicate: bool = False
    observations_created: int = 0
    open_loops_created: int = 0
    facts_created: int = 0
    evidence_created: int = 0
    evidence_rejected: int = 0
    potential_matches: list = field(default_factory=list)
    stages: list = field(default_factory=list)
    security_scan: Optional[dict] = None
    error: Optional[str] = None

    def stage(self, name: str, status: str, detail: Optional[str] = None):
        self.stages.append({"stage": name, "status": status, "detail": detail})

    def to_dict(self, db: Session) -> dict:
        document = self.document

        return {
            "ref": document.ref,
            "label": document.label,
            "content_type": document.content_type,
            "sha256": document.sha256,
            "source": document.source,
            "processing_status": document.processing_status,
            "quality_status": document.quality_status,
            "quality_reason": document.quality_reason,
            "processing_error": document.processing_error,
            "extraction_method": document.extraction_method,
            "document_date": document.document_date,
            "scan_status": document.scan_status,
            "scan_engine": document.scan_engine,
            "created_at": document.created_at,
            "ingestion_status": self.status,
            "duplicate": self.duplicate,
            "observations_created": self.observations_created,
            "open_loops_created": self.open_loops_created,
            "facts_created": self.facts_created,
            "evidence_created": self.evidence_created,
            "evidence_rejected": self.evidence_rejected,
            "potential_matches": [serialize_match(db, match) for match in self.potential_matches],
            "stages": self.stages,
            "security_scan": self.security_scan,
            "error": self.error,
        }


def _reject(patient: Patient, code: str, actor_ref: Optional[str], security_scan: Optional[dict] = None):
    feature = (security_scan or {}).get("detected_feature")
    message = REJECTION_MESSAGES.get(code, GENERIC_FAILURE)

    if code == "SUSPICIOUS_PDF" and feature in PDF_FEATURE_LABELS:
        message = f"The PDF contains active content ({PDF_FEATURE_LABELS[feature]}) and was rejected."

    # Only fixed codes reach the audit log, never document content.
    audit.record_now(
        "DOCUMENT_REJECTED", actor_type="SERVICE", actor_ref=actor_ref,
        object_type="PATIENT", object_ref=patient.ref, result="DENIED",
        reason=f"{code}:{feature}"[:60] if feature else code,
    )
    raise FileValidationError(code, message, security_scan)


# ============================================================
# RECEIVE (validate, scan, encrypt, quarantine)
# ============================================================

def receive_document(
    db: Session,
    patient: Patient,
    content: bytes,
    content_type: Optional[str],
    source: str,
    transaction: Optional[Transaction] = None,
    actor_ref: Optional[str] = None,
) -> IngestionResult:
    """
    Raises FileValidationError for anything that cannot be accepted.

    Commits any pending work of the caller first, so that rejection
    audit events can be written independently.
    """

    db.commit()

    try:
        verified_type = validate_file(content, content_type)
    except FileValidationError as exc:
        _reject(patient, exc.code, actor_ref)

    sha256 = sha256_of(content)

    existing = (
        db.query(Document)
        .filter(Document.patient_id == patient.id, Document.sha256 == sha256)
        .order_by(Document.id.asc())
        .first()
    )

    if existing is not None and existing.processing_status != "FAILED":
        if existing.processing_status == "QUARANTINED" and transaction is not None:
            existing.transaction_id = transaction.id
            db.commit()

        result = IngestionResult("DUPLICATE", existing, duplicate=True)
        result.stage("DEDUPLICATION", "DUPLICATE", "Identical file already received.")
        return result

    if existing is not None:
        # A previous attempt failed; retry cleanly.
        _delete_document_rows(db, existing)
        db.commit()

    try:
        inspected = run_inspection(content, verified_type)
    except WorkerError as exc:
        _reject(patient, exc.code, actor_ref, exc.security_scan)

    sanitized = decode(inspected, "sanitized")

    for payload in (content, sanitized):
        verdict = scanner.scan(payload, verified_type)

        if not verdict.clean:
            audit.record_now(
                "DOCUMENT_SCANNED", actor_type="SERVICE", actor_ref=verdict.engine,
                object_type="PATIENT", object_ref=patient.ref, result="DENIED", reason=verdict.code,
            )
            _reject(patient, "MALWARE_DETECTED", actor_ref)

    now = datetime.utcnow()

    document = Document(
        ref=new_ref("doc"),
        patient_id=patient.id,
        transaction_id=transaction.id if transaction else None,
        label={
            DocumentSource.WHATSAPP.value: "Document received via WhatsApp",
            DocumentSource.CAMERA.value: "Photo captured in portal",
            DocumentSource.DEMO.value: "Synthetic demo document",
        }.get(source, "Document uploaded in portal"),
        content_type=verified_type,
        sha256=sha256,
        stored_sha256=sha256_of(sanitized),
        byte_size=len(sanitized),
        page_count=inspected.get("page_count"),
        source=source,
        scan_status="CLEAN",
        scan_engine=scanner.engine_name(),
        sanitized=True,
        processing_status="QUARANTINED",
        retain_until=now + timedelta(days=settings.document_retention_days),
        created_at=now,
    )

    db.add(document)
    db.flush()

    vault.store(db, document, sanitized)

    for action in ("DOCUMENT_RECEIVED", "DOCUMENT_VALIDATED", "DOCUMENT_SCANNED", "DOCUMENT_ENCRYPTED"):
        audit.record(
            db, action, actor_type="SERVICE", actor_ref=actor_ref,
            object_type="DOCUMENT", object_ref=document.ref,
            reason=document.scan_engine if action == "DOCUMENT_SCANNED" else None,
        )

    db.commit()
    db.refresh(document)

    result = IngestionResult("RECEIVED", document, security_scan=inspected.get("security_scan"))
    result.stage("VALIDATION", "DONE", verified_type)

    if result.security_scan is not None:
        allowed = [item["feature"] for item in result.security_scan["allowed"]]
        result.stage(
            "ACTIVE_CONTENT_SCAN", "SAFE",
            "No active content" + (f"; removed {', '.join(allowed)}" if allowed else ""),
        )
    result.stage("SANITISATION", "DONE", "Rebuilt without metadata or active content")
    result.stage("MALWARE_SCAN", "CLEAN", document.scan_engine)
    result.stage("ENCRYPTION", "DONE", "AES-256-GCM, unique key")

    return result


# ============================================================
# PROCESS (decrypt in worker, extract, provenance)
# ============================================================

def _page_text(pages, page_number) -> str:
    return next((page["text"] for page in pages if page["page_number"] == page_number), "")


def _create_evidence(db: Session, document: Document, pages, item: dict) -> Optional[SourceEvidence]:
    """
    Quote-or-reject: only quotes found in the page text become
    evidence. This also guards against a misbehaving worker.
    """

    position = locate_quote(
        _page_text(pages, item["page_number"]),
        item["quote"],
        item.get("start_position"),
        item.get("end_position"),
    )

    if position is None:
        return None

    bbox = item.get("bbox") or [None] * 4

    evidence = SourceEvidence(
        document_id=document.id,
        quote=item["quote"],
        page_number=item["page_number"],
        start_position=position["start_position"],
        end_position=position["end_position"],
        bbox_x=bbox[0],
        bbox_y=bbox[1],
        bbox_w=bbox[2],
        bbox_h=bbox[3],
        confidence=item.get("confidence"),
        document_sha256=document.sha256,
        pipeline_version=PIPELINE_VERSION,
    )

    db.add(evidence)
    db.flush()

    return evidence


def _store_extraction(db: Session, document: Document, extracted: dict, result: IngestionResult):
    pages = extracted["pages"]
    future_date = (
        document.document_date is not None
        and document.document_date > date.today() + timedelta(days=1)
    )

    for item in extracted["observations"]:
        evidence = _create_evidence(db, document, pages, item)

        if evidence is None:
            result.evidence_rejected += 1
            continue

        confidence = item.get("confidence")
        state, note = FactState.SOURCE_FACT.value, None

        if confidence is not None and confidence < OCR_CONFIDENCE_THRESHOLD:
            state, note = FactState.UNCERTAIN.value, "Low OCR confidence; confirm against the source."
        elif future_date:
            state, note = FactState.UNCERTAIN.value, "Report date is in the future."

        db.add(
            Observation(
                patient_id=document.patient_id,
                document_id=document.id,
                observation_type=item["observation_type"],
                value=item["value"],
                unit=item.get("unit"),
                event_date=document.document_date,
                evidence_id=evidence.id,
                review_status=ReviewStatus.REVIEW.value,
                fact_state=state,
                fact_note=note,
                confidence=confidence,
            )
        )

        result.evidence_created += 1
        result.observations_created += 1

    for item in extracted["commitments"]:
        evidence = _create_evidence(db, document, pages, item)

        if evidence is None:
            result.evidence_rejected += 1
            continue

        db.add(
            Commitment(
                patient_id=document.patient_id,
                document_id=document.id,
                instruction=item["instruction"],
                due_date=date.fromisoformat(item["due_date"]) if item.get("due_date") else None,
                evidence_id=evidence.id,
                state=LoopState.OPEN.value,
            )
        )

        result.evidence_created += 1
        result.open_loops_created += 1

    for item in extracted["facts"]:
        evidence = _create_evidence(db, document, pages, item)

        if evidence is None:
            result.evidence_rejected += 1
            continue

        state = item["state"]

        if future_date and state != FactState.UNCERTAIN.value:
            state = FactState.UNCERTAIN.value

        db.add(
            ClinicalFact(
                ref=new_ref("fct"),
                patient_id=document.patient_id,
                document_id=document.id,
                evidence_id=evidence.id,
                category=item["category"],
                label=item["label"],
                fields=json.dumps(item["fields"]),
                state=state,
                confidence=item.get("confidence"),
                pipeline_version=PIPELINE_VERSION,
            )
        )

        result.evidence_created += 1
        result.facts_created += 1

    db.flush()


def process_document(db: Session, document: Document, actor_ref: Optional[str] = None) -> IngestionResult:
    """
    Read a quarantined document. The caller must already have
    verified the transaction and an active consent.
    """

    result = IngestionResult("PROCESSED", document)

    document.processing_status = "PROCESSING"
    audit.record(
        db, "OCR_STARTED", actor_type="SERVICE", actor_ref="processing-worker",
        object_type="DOCUMENT", object_ref=document.ref,
    )
    db.commit()

    try:
        plaintext = vault.load(db, document)

        try:
            extracted = run_extraction(plaintext, document.content_type, document.created_at.date())
        finally:
            del plaintext

        if extracted.get("quality"):
            document.quality_status = extracted["quality"]["quality_status"]
            document.quality_reason = extracted["quality"]["quality_reason"]
            result.stage("QUALITY_CHECK", document.quality_status, document.quality_reason)
        else:
            result.stage("QUALITY_CHECK", "SKIPPED", "PDF document")

        if extracted.get("pages") is None:
            document.processing_status = "REJECTED"
            document.processed_at = datetime.utcnow()
            result.status = "RETAKE"
            result.stage("COMPLETE", "RETAKE", document.quality_reason)
            audit.record(
                db, "EXTRACTION_COMPLETED", actor_type="SERVICE", actor_ref="processing-worker",
                object_type="DOCUMENT", object_ref=document.ref, result="DENIED", reason="RETAKE",
            )
            db.commit()
            return result

        if not any(page["text"].strip() for page in extracted["pages"]):
            raise ExtractionFailed("No readable text was detected in the document.")

        document.extraction_method = extracted.get("extraction_method")
        document.document_date = (
            date.fromisoformat(extracted["document_date"]) if extracted.get("document_date") else None
        )

        result.stage("EXTRACTION", "DONE", document.extraction_method)

        _store_extraction(db, document, extracted, result)

        result.stage("EVIDENCE", "DONE", f"{result.evidence_created} source quotes linked")

        result.potential_matches = detect_potential_matches(db, document)

        result.stage("LOOP_MATCHING", "DONE", f"{len(result.potential_matches)} potential matches")

        document.processing_status = "PROCESSED"
        document.processed_at = datetime.utcnow()
        result.stage("COMPLETE", "DONE")

        audit.record(
            db, "EXTRACTION_COMPLETED", actor_type="SERVICE", actor_ref="processing-worker",
            object_type="DOCUMENT", object_ref=document.ref,
        )

        db.commit()

        return result

    except Exception as exc:
        db.rollback()

        message = str(exc) if isinstance(exc, ExtractionFailed) else GENERIC_FAILURE
        reason = exc.code if isinstance(exc, WorkerError) else type(exc).__name__

        document = db.get(Document, result.document.id)
        document.processing_status = "FAILED"
        document.processing_error = message
        audit.record(
            db, "EXTRACTION_FAILED", actor_type="SERVICE", actor_ref="processing-worker",
            object_type="DOCUMENT", object_ref=document.ref, result="FAILED", reason=reason[:60],
        )
        db.commit()

        result.document = document
        result.status = "FAILED"
        result.error = message
        result.observations_created = 0
        result.open_loops_created = 0
        result.facts_created = 0
        result.evidence_created = 0
        result.potential_matches = []
        result.stage("COMPLETE", "FAILED", message)

        return result


def ingest_document(
    db: Session,
    patient: Patient,
    content: bytes,
    content_type: Optional[str],
    source: str = DocumentSource.WEB.value,
    transaction: Optional[Transaction] = None,
    actor_ref: Optional[str] = None,
) -> IngestionResult:
    """
    Receive and immediately process. Only for callers that have
    already confirmed an active consent for this patient.
    """

    received = receive_document(db, patient, content, content_type, source, transaction, actor_ref)

    if received.duplicate and received.document.processing_status != "QUARANTINED":
        return received

    processed = process_document(db, received.document, actor_ref)
    processed.stages = received.stages + processed.stages
    processed.duplicate = received.duplicate
    processed.security_scan = received.security_scan

    return processed


# ============================================================
# DELETION
# ============================================================

def _delete_document_rows(db: Session, document: Document):
    from .models import DocumentKey, LoopEvent, LoopMatch

    commitment_ids = [
        row.id for row in db.query(Commitment.id).filter(Commitment.document_id == document.id)
    ]

    match_query = db.query(LoopMatch).filter(
        (LoopMatch.document_id == document.id) | LoopMatch.commitment_id.in_(commitment_ids or [-1])
    )
    match_ids = [match.id for match in match_query.all()]

    if match_ids:
        db.query(LoopEvent).filter(LoopEvent.match_id.in_(match_ids)).update(
            {LoopEvent.match_id: None}, synchronize_session=False
        )

    if commitment_ids:
        db.query(LoopEvent).filter(LoopEvent.commitment_id.in_(commitment_ids)).delete(
            synchronize_session=False
        )

    match_query.delete(synchronize_session=False)

    db.query(Commitment).filter(Commitment.document_id == document.id).delete(synchronize_session=False)
    db.query(Observation).filter(Observation.document_id == document.id).delete(synchronize_session=False)
    db.query(ClinicalFact).filter(ClinicalFact.document_id == document.id).delete(synchronize_session=False)
    db.query(SourceEvidence).filter(SourceEvidence.document_id == document.id).delete(
        synchronize_session=False
    )

    vault.destroy(db, document)

    db.query(DocumentKey).filter(DocumentKey.document_id == document.id).delete(synchronize_session=False)
    db.delete(document)
    db.flush()


def delete_document(db: Session, document: Document, actor_type: str, actor_ref: Optional[str], reason: str):
    """
    DESTROY DEK -> DELETE OBJECT -> DELETE DATABASE REFERENCES -> AUDIT
    """

    ref = document.ref
    _delete_document_rows(db, document)

    audit.record(
        db,
        "DOCUMENT_RETENTION_EXPIRED" if reason == "RETENTION" else "DOCUMENT_DELETED",
        actor_type=actor_type, actor_ref=actor_ref,
        object_type="DOCUMENT", object_ref=ref, reason=reason,
    )
