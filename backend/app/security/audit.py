"""
Tamper-evident audit trail.

Every event stores the hash of the previous event:

    event_hash = SHA-256(canonical_event_including_prev_hash)
    mac        = HMAC-SHA256(audit key, event_hash)

Changing, removing or reordering any event breaks the chain from
that point on. The HMAC means an attacker who can write to the
database still cannot recompute a valid chain without the audit
key, and the chain head is anchored in the separate identity
database so silently truncating the tail is also detected.

Events hold references and codes only (usr_..., pt_..., doc_...),
never PHI.
"""

import hashlib
import hmac
import json
import re
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Integer, String, event, text
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal, engine
from ..identity import IdentityBase, IdentitySessionLocal
from ..models import AuditEvent
from .phi_log import log_event


GENESIS = "0" * 64

_lock = threading.Lock()

ACTIONS = {
    # Channel / ingestion
    "WEBHOOK_SIGNATURE_VERIFIED", "WEBHOOK_SIGNATURE_REJECTED",
    "WEBHOOK_REPLAY_BLOCKED", "WEBHOOK_RATE_LIMITED", "MESSAGE_UNENROLLED",
    "DEMO_RATE_LIMIT_RESET",
    "TRANSACTION_CREATED", "TRANSACTION_EXPIRED",
    "MEDIA_DOWNLOADED", "MEDIA_DELETED_FROM_PROVIDER", "MEDIA_DELETE_FAILED",
    "DOCUMENT_RECEIVED", "DOCUMENT_VALIDATED", "DOCUMENT_REJECTED",
    "DOCUMENT_SCANNED", "DOCUMENT_ENCRYPTED", "OCR_STARTED",
    "EXTRACTION_COMPLETED", "EXTRACTION_FAILED", "DOCUMENT_DELETED",
    "DOCUMENT_RETENTION_EXPIRED", "NOTIFICATION_SENT",
    # Patient verification and consent
    "OTP_ISSUED", "OTP_VERIFIED", "OTP_FAILED", "OTP_LOCKED",
    "CONSENT_GRANTED", "CONSENT_REVOKED", "CONSENT_EXPIRED",
    # Staff authentication
    "LOGIN_PASSWORD_OK", "LOGIN_FAILED", "LOGIN_LOCKED",
    "MFA_VERIFIED", "MFA_FAILED", "LOGOUT", "SESSION_EXPIRED",
    # Clinical access
    "DOCTOR_ACCESS_GRANTED", "DOCTOR_ACCESS_DENIED",
    "EVIDENCE_TOKEN_ISSUED", "DOCUMENT_VIEWED", "EVIDENCE_TOKEN_REJECTED",
    "FACT_CONFIRMED", "FACT_REJECTED", "OBSERVATION_VERIFIED",
    "OBSERVATION_REJECTED", "LOOP_DECISION", "ANOMALY_DETECTED",
    # Administration
    "PATIENT_ENROLLED", "AUDIT_CHAIN_VERIFIED", "AUDIT_CHAIN_BROKEN",
    "DEMO_RESET",
}

_SAFE = re.compile(r"^[A-Za-z0-9_.:\-]{0,60}$")


class AuditAnchor(IdentityBase):
    """
    Latest chain head, kept in the identity database.
    """

    __tablename__ = "audit_anchor"

    id = Column(Integer, primary_key=True)
    head_seq = Column(Integer, nullable=False)
    head_hash = Column(String(64), nullable=False)


def _audit_key() -> bytes:
    return settings.keys()["JEEVAFLOW_AUDIT_KEY"]


def canonical(fields: dict) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _fields(row) -> dict:
    return {
        "seq": row.seq,
        "created_at": row.created_at,
        "actor_type": row.actor_type,
        "actor_ref": row.actor_ref,
        "action": row.action,
        "object_type": row.object_type,
        "object_ref": row.object_ref,
        "result": row.result,
        "reason": row.reason,
        "prev_hash": row.prev_hash,
    }


def _digest(fields: dict) -> tuple[str, str]:
    event_hash = hashlib.sha256(canonical(fields).encode()).hexdigest()
    mac = hmac.new(_audit_key(), event_hash.encode(), hashlib.sha256).hexdigest()
    return event_hash, mac


def _safe(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    value = str(value)

    return value if _SAFE.match(value) else "REDACTED"


def _append(entries: list[dict]):
    with _lock:
        db = SessionLocal()
        idb = IdentitySessionLocal()

        try:
            last = db.query(AuditEvent).order_by(AuditEvent.seq.desc()).first()
            prev_hash = last.event_hash if last else GENESIS
            seq = last.seq if last else 0

            for entry in entries:
                seq += 1
                fields = {
                    "seq": seq,
                    "created_at": datetime.utcnow().isoformat(timespec="microseconds"),
                    "prev_hash": prev_hash,
                    **entry,
                }
                event_hash, mac = _digest(fields)

                db.add(AuditEvent(**fields, event_hash=event_hash, mac=mac))
                prev_hash = event_hash

            db.commit()

            anchor = idb.get(AuditAnchor, 1)

            if anchor is None:
                idb.add(AuditAnchor(id=1, head_seq=seq, head_hash=prev_hash))
            else:
                anchor.head_seq = seq
                anchor.head_hash = prev_hash

            idb.commit()

        finally:
            db.close()
            idb.close()

    for entry in entries:
        log_event(
            entry["action"],
            result=entry["result"],
            actor=entry["actor_ref"],
            object=entry["object_ref"],
            reason=entry["reason"],
        )


def _entry(action, actor_type, actor_ref, object_type, object_ref, result, reason) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"Unknown audit action {action}")

    return {
        "actor_type": _safe(actor_type),
        "actor_ref": _safe(actor_ref),
        "action": action,
        "object_type": _safe(object_type),
        "object_ref": _safe(object_ref),
        "result": _safe(result) or "SUCCESS",
        "reason": _safe(reason),
    }


def record(
    db: Session,
    action: str,
    *,
    actor_type: str = "SYSTEM",
    actor_ref: Optional[str] = None,
    object_type: Optional[str] = None,
    object_ref: Optional[str] = None,
    result: str = "SUCCESS",
    reason: Optional[str] = None,
):
    """
    Record an event that belongs to the caller's database
    transaction. It is written after that transaction commits and
    discarded if it rolls back.
    """

    db.info.setdefault("pending_audit", []).append(
        _entry(action, actor_type, actor_ref, object_type, object_ref, result, reason)
    )


def record_now(
    action: str,
    *,
    actor_type: str = "SYSTEM",
    actor_ref: Optional[str] = None,
    object_type: Optional[str] = None,
    object_ref: Optional[str] = None,
    result: str = "SUCCESS",
    reason: Optional[str] = None,
):
    """
    Record a standalone event immediately (e.g. a denied request).
    The caller must not hold uncommitted writes.
    """

    _append([_entry(action, actor_type, actor_ref, object_type, object_ref, result, reason)])


@event.listens_for(Session, "after_commit")
def _flush_pending(session):
    pending = session.info.pop("pending_audit", None)

    if pending:
        _append(pending)


@event.listens_for(Session, "after_rollback")
def _drop_pending(session):
    session.info.pop("pending_audit", None)


# ============================================================
# VERIFICATION
# ============================================================

def verify_chain(db: Session) -> dict:
    rows = db.query(AuditEvent).order_by(AuditEvent.seq.asc()).all()

    prev_hash = GENESIS
    expected_seq = 1

    for row in rows:
        if row.seq != expected_seq:
            return _broken(row.seq, "SEQUENCE_GAP", len(rows))

        if row.prev_hash != prev_hash:
            return _broken(row.seq, "PREVIOUS_HASH_MISMATCH", len(rows))

        event_hash, mac = _digest(_fields(row))

        if not hmac.compare_digest(event_hash, row.event_hash):
            return _broken(row.seq, "EVENT_HASH_MISMATCH", len(rows))

        if not hmac.compare_digest(mac, row.mac):
            return _broken(row.seq, "MAC_MISMATCH", len(rows))

        prev_hash = row.event_hash
        expected_seq += 1

    idb = IdentitySessionLocal()

    try:
        anchor = idb.get(AuditAnchor, 1)
    finally:
        idb.close()

    if rows and anchor is not None:
        if anchor.head_seq != rows[-1].seq or anchor.head_hash != rows[-1].event_hash:
            return _broken(rows[-1].seq, "ANCHOR_MISMATCH", len(rows))

    if anchor is not None and not rows and anchor.head_seq:
        return _broken(0, "ANCHOR_MISMATCH", 0)

    return {
        "valid": True,
        "events": len(rows),
        "head_hash": prev_hash,
        "broken_at_seq": None,
        "reason": None,
    }


def _broken(seq: int, reason: str, total: int) -> dict:
    return {
        "valid": False,
        "events": total,
        "head_hash": None,
        "broken_at_seq": seq,
        "reason": reason,
    }


def serialize(row: AuditEvent) -> dict:
    return {
        "seq": row.seq,
        "created_at": row.created_at,
        "actor_type": row.actor_type,
        "actor_ref": row.actor_ref,
        "action": row.action,
        "object_type": row.object_type,
        "object_ref": row.object_ref,
        "result": row.result,
        "reason": row.reason,
        "prev_hash": row.prev_hash,
        "event_hash": row.event_hash,
    }


# ============================================================
# DEMO: SIMULATED DATABASE-LEVEL TAMPERING
# ============================================================

_tampered: dict[int, str] = {}


def demo_tamper(seq: int) -> bool:
    """
    DEMO IMPLEMENTATION. Simulates an attacker with direct database
    write access: the append-only trigger is bypassed and one
    event's result is rewritten. verify_chain() must detect it.
    """

    with _lock, engine.begin() as connection:
        row = connection.execute(
            text("SELECT result FROM audit_events WHERE seq = :seq"), {"seq": seq}
        ).first()

        if row is None:
            return False

        _tampered.setdefault(seq, row[0])

        connection.execute(text("DROP TRIGGER IF EXISTS audit_events_no_update"))
        connection.execute(
            text("UPDATE audit_events SET result = :value WHERE seq = :seq"),
            {"value": "SUCCESS" if row[0] != "SUCCESS" else "DENIED", "seq": seq},
        )

    from ..database import AUDIT_TRIGGERS

    with engine.begin() as connection:
        connection.execute(text(AUDIT_TRIGGERS[0]))

    return True


def demo_restore() -> int:
    restored = 0

    with _lock, engine.begin() as connection:
        connection.execute(text("DROP TRIGGER IF EXISTS audit_events_no_update"))

        for seq, original in list(_tampered.items()):
            connection.execute(
                text("UPDATE audit_events SET result = :value WHERE seq = :seq"),
                {"value": original, "seq": seq},
            )
            restored += 1

        _tampered.clear()

    from ..database import AUDIT_TRIGGERS

    with engine.begin() as connection:
        connection.execute(text(AUDIT_TRIGGERS[0]))

    return restored
