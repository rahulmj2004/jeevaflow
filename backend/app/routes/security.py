"""
Security dashboard and audit trail.

The status report states what is actually running. Controls that
are demo-grade are labelled DEMO IMPLEMENTATION and the production
replacement is named; nothing here claims the system is "secure"
in absolute terms or compliant with any regulation.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..identity import get_identity_db
from ..models import AuditEvent, Consent, Document, DocumentKey, Patient
from ..security import audit, scanner
from ..security.auth import StaffPrincipal, require_any_staff, require_auditor
from ..security.consent import consent_status
from ..whatsapp import integration_status


router = APIRouter(tags=["security"])


def _control(name: str, status: str, detail: str, grade: str = "IMPLEMENTED", production: str = None) -> dict:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        # IMPLEMENTED | DEMO IMPLEMENTATION | PRODUCTION REQUIRED
        "grade": grade,
        "production": production,
    }


@router.get("/api/v1/security/status")
def security_status(
    principal: StaffPrincipal = Depends(require_any_staff),
    db: Session = Depends(get_db),
):
    chain = audit.verify_chain(db)

    whatsapp = integration_status()

    documents = db.query(Document).count()
    keys_live = db.query(DocumentKey).filter(DocumentKey.wrapped_dek.isnot(None)).count()

    engine = scanner.engine_name()

    last_doc = db.query(Document).order_by(Document.created_at.desc()).first()

    demo_consent = None

    if settings.demo_mode:
        from ..seed import demo_patient

        patient = demo_patient(db)

        if patient is not None:
            latest = (
                db.query(Consent)
                .filter(Consent.patient_id == patient.id)
                .order_by(Consent.granted_at.desc())
                .first()
            )
            demo_consent = consent_status(latest) if latest else "NONE"

    controls = [
        _control(
            "Encryption", "ACTIVE",
            f"AES-256-GCM per document with unique keys ({keys_live} live of {documents} documents); "
            "medical and identity columns encrypted at field level.",
        ),
        _control(
            "Key management", "ACTIVE",
            "Envelope encryption with a KeyProvider abstraction; KEK loaded from the environment.",
            "DEMO IMPLEMENTATION", "AWS KMS / Azure Key Vault / Cloud KMS / Vault / HSM provider.",
        ),
        _control("OTP Protection", "ACTIVE", "6 digits, 5 min, single use, HMAC stored, 5 attempts, back-off, lockout."),
        _control(
            "Consent", demo_consent or "ENFORCED",
            "Checked server-side on every doctor request (purpose, scope, expiry, revocation).",
        ),
        _control("Doctor MFA", "ACTIVE", "Password (scrypt) + TOTP; session cookies HttpOnly, SameSite=Strict, CSRF token."),
        _control(
            "File Scan",
            "PASSED" if last_doc is None or last_doc.scan_status == "CLEAN" else "ATTENTION",
            f"Engine: {engine}. Magic bytes, structure, active-content and bomb checks; sanitised re-encode.",
            "IMPLEMENTED" if engine.startswith("clamav") else "DEMO IMPLEMENTATION",
            None if engine.startswith("clamav") else "Install ClamAV (clamdscan) or a managed scanning service.",
        ),
        _control(
            "OCR", "PRIVATE",
            "Local Tesseract in an isolated worker process: no secrets, no DB, egress blocked, temp files deleted.",
            "DEMO IMPLEMENTATION", "Run the worker in a separate container/VM with no network and a read-only root.",
        ),
        _control("External AI", "DISABLED", "No document or extracted data is sent to any external AI service."),
        _control("PHI Logging", "BLOCKED", "Allow-listed security events only; other application logs dropped; tracebacks stripped."),
        _control(
            "Audit Chain", "VERIFIED" if chain["valid"] else "BROKEN",
            f"{chain['events']} events, SHA-256 hash chain + HMAC, head anchored in the identity database."
            + ("" if chain["valid"] else f" Broken at #{chain['broken_at_seq']} ({chain['reason']})."),
            "IMPLEMENTED", "Also anchor the chain head to WORM storage or an external timestamping service.",
        ),
        _control("Document Access", "AUTHORIZED", "Single-use 60 s evidence tokens bound to the session; watermarked render; no file download."),
        _control("Document Retention", "ACTIVE", f"Documents expire after {settings.document_retention_days} days; deletion destroys the key first."),
        _control(
            "WhatsApp Channel", whatsapp["signature_validation"],
            "Signature, MessageSid replay, account and freshness checks; generic replies; media deleted from Twilio.",
            "IMPLEMENTED" if whatsapp["scoped_api_key"] else "DEMO IMPLEMENTATION",
            None if whatsapp["scoped_api_key"] else "Use a restricted Twilio API key (TWILIO_API_KEY_SID/SECRET).",
        ),
        _control(
            "Rate limiting", "ACTIVE", "Per sender, per user and per patient (per IP for invalid webhook signatures), configurable, HTTP 429 with Retry-After; bulk-access anomaly detection.",
            "DEMO IMPLEMENTATION", "Shared limiter (e.g. Redis) across instances.",
        ),
        _control(
            "Transport", "HTTPS" if settings.public_base_url.startswith("https://") else "LOCAL HTTP",
            "HSTS and Secure cookies in production; webhook must be https.",
            "IMPLEMENTED" if settings.is_production else "DEMO IMPLEMENTATION",
            "Terminate TLS at the edge; JEEVAFLOW_ENV=production.",
        ),
        _control(
            "Database encryption at rest", "FIELD-LEVEL",
            "Sensitive columns are encrypted; the SQLite files themselves are not.",
            "PRODUCTION REQUIRED", "SQLCipher or a managed database with encryption at rest and least-privilege accounts.",
        ),
    ]

    return {
        "generated_at": datetime.utcnow(),
        "statement": "Defense-in-depth security architecture. Patient data protected using layered security controls.",
        "environment": settings.environment,
        "demo_mode": settings.demo_mode,
        "controls": controls,
        "audit_chain": chain,
    }


@router.get("/api/v1/audit/events")
def audit_events(
    limit: int = Query(100, ge=1, le=500),
    principal: StaffPrincipal = Depends(require_auditor),
    db: Session = Depends(get_db),
):
    rows = db.query(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit).all()

    return [audit.serialize(row) for row in rows]


@router.post("/api/v1/audit/verify")
def verify_audit(principal: StaffPrincipal = Depends(require_auditor), db: Session = Depends(get_db)):
    result = audit.verify_chain(db)

    audit.record_now(
        "AUDIT_CHAIN_VERIFIED" if result["valid"] else "AUDIT_CHAIN_BROKEN",
        actor_type="STAFF", actor_ref=principal.user_ref,
        result="SUCCESS" if result["valid"] else "FAILED",
        reason=result["reason"],
    )

    return result


class TamperRequest(BaseModel):
    seq: int


@router.post("/api/v1/demo/audit/tamper")
def tamper(body: TamperRequest, principal: StaffPrincipal = Depends(require_auditor)):
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Not found.")

    if not audit.demo_tamper(body.seq):
        raise HTTPException(status_code=404, detail="Audit event not found.")

    return {"tampered_seq": body.seq}


@router.post("/api/v1/demo/audit/restore")
def restore(principal: StaffPrincipal = Depends(require_auditor)):
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="Not found.")

    return {"restored": audit.demo_restore()}
