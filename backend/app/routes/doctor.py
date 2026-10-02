"""
Doctor console API.

Every route requires: staff session + MFA + role DOCTOR, and every
patient-data route re-checks, server-side, an active consent from
that patient to this doctor covering the scope being read. Unknown
objects and objects without consent return the same 403.

There is no "list all patients" endpoint: /doctor/patients lists
only patients with an active consent to the signed-in doctor.

Original documents are never served as files. A doctor can view one
evidence region at a time through a short-lived, single-use token
bound to their session; the server decrypts the document, the
isolated worker renders the page with the region highlighted and a
watermark (doctor ID + time), and the PNG is streamed with
Cache-Control: no-store.
"""

import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import vault
from ..conflicts import detect_observation_conflicts
from ..database import get_db
from ..doctor_brief import build_doctor_form
from ..identity import get_identity, get_identity_db
from ..loops import (
    LoopActionError,
    confirm_completion,
    get_open_loops,
    keep_open,
    mark_needs_review,
    serialize_loop,
)
from ..models import (
    ClinicalFact,
    Commitment,
    Consent,
    Document,
    EvidenceToken,
    LoopMatch,
    LoopState,
    MatchStatus,
    Observation,
    Patient,
    SourceEvidence,
)
from ..loops import record_event
from ..provenance import serialize_evidence
from ..security import audit
from ..security.auth import GENERIC_FORBIDDEN, StaffPrincipal, require_doctor
from ..security.consent import authorize_doctor, consent_status, scopes_of
from ..security.crypto import keyed_hash
from ..timeline import get_patient_timeline
from ..worker_client import WorkerError, decode, run_worker
from .deps import limit


router = APIRouter(prefix="/api/v1/doctor", tags=["doctor"])

EVIDENCE_TOKEN_TTL = timedelta(seconds=60)

CATEGORY_SCOPE = {
    "MEDICATION": "MEDICATIONS",
    "PRESCRIBER": "MEDICATIONS",
    "ALLERGY": "ALLERGIES",
}


class LoopAction(BaseModel):
    match_id: Optional[int] = None
    note: Optional[str] = Field(default=None, max_length=2000)


def forbidden():
    raise HTTPException(status_code=403, detail=GENERIC_FORBIDDEN)


def _granted(principal: StaffPrincipal, patient: Patient, what: str):
    audit.record_now(
        "DOCTOR_ACCESS_GRANTED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="PATIENT", object_ref=patient.ref, reason=what,
    )


def _patient_of(db: Session, patient_id: int) -> Optional[Patient]:
    return db.get(Patient, patient_id)


def _scoped_loops(loops: list[dict], scopes: set[str]) -> list[dict]:
    """
    Loop matches carry the lab result that may fulfil an instruction.
    Without the LABS scope that value and its evidence are withheld.
    """

    if "LABS" in scopes:
        return loops

    for loop in loops:
        for match in loop["potential_matches"] + ([loop["confirmed_match"]] if loop["confirmed_match"] else []):
            match["observation"] = None
            match["evidence"] = None

    return loops


# ============================================================
# PATIENTS (consented only)
# ============================================================

@router.get("/patients")
def my_patients(
    principal: StaffPrincipal = Depends(require_doctor),
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    now = datetime.utcnow()

    consents = (
        db.query(Consent)
        .filter(
            Consent.doctor_ref == principal.user_ref,
            Consent.revoked_at.is_(None),
            Consent.expires_at > now,
        )
        .order_by(Consent.granted_at.desc())
        .all()
    )

    seen, items = set(), []

    for consent in consents:
        if consent.patient_id in seen:
            continue

        seen.add(consent.patient_id)
        patient = db.get(Patient, consent.patient_id)
        identity = get_identity(idb, patient.ref) if "DEMOGRAPHICS" in scopes_of(consent) else None

        items.append({
            "patient_ref": patient.ref,
            "name": identity.name if identity else None,
            "consent_ref": consent.ref,
            "scopes": sorted(scopes_of(consent)),
            "expires_at": consent.expires_at,
        })

    return items


@router.get("/patients/{patient_ref}/form")
def doctor_form(
    patient_ref: str,
    principal: StaffPrincipal = Depends(require_doctor),
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    patient, consent = authorize_doctor(db, principal, patient_ref)
    _granted(principal, patient, "DOCTOR_FORM")

    return build_doctor_form(db, idb, patient, consent)


@router.get("/patients/{patient_ref}/journey")
def journey(
    patient_ref: str,
    principal: StaffPrincipal = Depends(require_doctor),
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    patient, consent = authorize_doctor(db, principal, patient_ref)
    scopes = scopes_of(consent)
    _granted(principal, patient, "JOURNEY")

    shared = {
        document.id: document
        for document in db.query(Document).filter(
            Document.patient_id == patient.id, Document.processing_status != "QUARANTINED"
        )
    }

    timeline = [
        item
        for item in get_patient_timeline(db=db, patient_id=patient.id)
        if item["document_id"] in shared
        and (
            item["type"] == "DOCUMENT"
            or (item["type"] == "OBSERVATION" and "LABS" in scopes)
            or (item["type"] == "COMMITMENT" and "INSTRUCTIONS" in scopes)
        )
    ]

    loops = _scoped_loops(get_open_loops(db=db, patient_id=patient.id), scopes) if "INSTRUCTIONS" in scopes else []
    conflicts = detect_observation_conflicts(db=db, patient_id=patient.id) if "LABS" in scopes else []

    identity = get_identity(idb, patient.ref) if "DEMOGRAPHICS" in scopes else None

    return {
        "patient": {"ref": patient.ref, "name": identity.name if identity else None},
        "consent": {
            "ref": consent.ref,
            "scopes": sorted(scopes),
            "expires_at": consent.expires_at,
            "status": consent_status(consent),
        },
        "summary": {
            "document_count": len(shared),
            "observation_count": sum(1 for item in timeline if item["type"] == "OBSERVATION"),
            "open_loop_count": sum(1 for loop in loops if loop["state"] != LoopState.CLOSED.value),
            "potential_match_count": sum(1 for loop in loops if loop["state"] == LoopState.POTENTIAL_MATCH.value),
            "conflict_count": len(conflicts),
        },
        "documents": [
            {
                "ref": document.ref,
                "label": document.label,
                "status": document.processing_status,
                "quality": document.quality_status,
                "source": document.source,
                "document_date": document.document_date,
                "created_at": document.created_at,
            }
            for document in sorted(shared.values(), key=lambda d: d.created_at, reverse=True)
        ],
        "timeline": timeline,
        "open_loops": loops,
        "conflicts": conflicts,
    }


@router.get("/patients/{patient_ref}/loops")
def loops(patient_ref: str, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    patient, consent = authorize_doctor(db, principal, patient_ref, "INSTRUCTIONS")

    return _scoped_loops(get_open_loops(db=db, patient_id=patient.id), scopes_of(consent))


@router.get("/patients/{patient_ref}/conflicts")
def conflicts(patient_ref: str, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    patient, _ = authorize_doctor(db, principal, patient_ref, "LABS")

    return detect_observation_conflicts(db=db, patient_id=patient.id)


# ============================================================
# OBSERVATIONS AND FACTS (doctor is the final gate)
# ============================================================

def _observation(db: Session, principal: StaffPrincipal, observation_id: int) -> Observation:
    observation = db.get(Observation, observation_id)

    if observation is None:
        audit.record_now(
            "DOCTOR_ACCESS_DENIED", actor_type="STAFF", actor_ref=principal.user_ref,
            result="DENIED", reason="UNKNOWN_OBJECT",
        )
        forbidden()

    authorize_doctor(db, principal, _patient_of(db, observation.patient_id).ref, "LABS")

    return observation


def _serialize_observation(observation: Observation) -> dict:
    return {
        "id": observation.id,
        "observation_type": observation.observation_type,
        "value": observation.value,
        "unit": observation.unit,
        "event_date": observation.event_date,
        "review_status": observation.review_status,
        "state": observation.fact_state,
        "evidence_id": observation.evidence_id,
    }


@router.patch("/observations/{observation_id}/verify")
def verify_observation(
    observation_id: int,
    principal: StaffPrincipal = Depends(require_doctor),
    db: Session = Depends(get_db),
):
    observation = _observation(db, principal, observation_id)

    if observation.evidence_id is None:
        raise HTTPException(status_code=422, detail="Observation cannot be verified without source evidence.")

    observation.review_status = "VERIFIED"
    audit.record(
        db, "OBSERVATION_VERIFIED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="OBSERVATION", object_ref=str(observation.id),
    )
    db.commit()

    return _serialize_observation(observation)


@router.patch("/observations/{observation_id}/reject")
def reject_observation(
    observation_id: int,
    principal: StaffPrincipal = Depends(require_doctor),
    db: Session = Depends(get_db),
):
    """
    Any pending Open Loop match that relied on this observation is
    withdrawn, because its supporting evidence was rejected.
    """

    observation = _observation(db, principal, observation_id)
    observation.review_status = "REJECTED"

    for match in db.query(LoopMatch).filter(
        LoopMatch.observation_id == observation.id,
        LoopMatch.status == MatchStatus.PENDING.value,
    ):
        match.status = MatchStatus.DISMISSED.value
        commitment = db.get(Commitment, match.commitment_id)

        still_pending = (
            db.query(LoopMatch)
            .filter(
                LoopMatch.commitment_id == commitment.id,
                LoopMatch.status == MatchStatus.PENDING.value,
                LoopMatch.id != match.id,
            )
            .count()
        )

        record_event(
            db,
            commitment,
            action="MATCH_WITHDRAWN",
            to_state=(
                LoopState.OPEN.value
                if not still_pending and commitment.state == LoopState.POTENTIAL_MATCH.value
                else None
            ),
            actor_type="SYSTEM",
            actor_name="observation-rejected",
            match_id=match.id,
            note="Supporting observation was rejected in review.",
        )

    audit.record(
        db, "OBSERVATION_REJECTED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="OBSERVATION", object_ref=str(observation.id),
    )
    db.commit()

    return _serialize_observation(observation)


def _fact(db: Session, principal: StaffPrincipal, fact_ref: str) -> ClinicalFact:
    fact = db.query(ClinicalFact).filter(ClinicalFact.ref == fact_ref).first()

    if fact is None:
        audit.record_now(
            "DOCTOR_ACCESS_DENIED", actor_type="STAFF", actor_ref=principal.user_ref,
            result="DENIED", reason="UNKNOWN_OBJECT",
        )
        forbidden()

    authorize_doctor(db, principal, _patient_of(db, fact.patient_id).ref, CATEGORY_SCOPE[fact.category])

    return fact


@router.patch("/facts/{fact_ref}/confirm")
def confirm_fact(fact_ref: str, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    fact = _fact(db, principal, fact_ref)
    fact.review_status = "CONFIRMED"
    fact.reviewed_by_ref = principal.user_ref
    fact.reviewed_at = datetime.utcnow()
    audit.record(
        db, "FACT_CONFIRMED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="FACT", object_ref=fact.ref,
    )
    db.commit()

    return {"ref": fact.ref, "review_status": fact.review_status}


@router.patch("/facts/{fact_ref}/reject")
def reject_fact(fact_ref: str, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    fact = _fact(db, principal, fact_ref)
    fact.review_status = "REJECTED"
    fact.reviewed_by_ref = principal.user_ref
    fact.reviewed_at = datetime.utcnow()
    audit.record(
        db, "FACT_REJECTED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="FACT", object_ref=fact.ref,
    )
    db.commit()

    return {"ref": fact.ref, "review_status": fact.review_status}


# ============================================================
# OPEN LOOPS
# ============================================================

def _commitment(db: Session, principal: StaffPrincipal, commitment_id: int) -> Commitment:
    commitment = db.get(Commitment, commitment_id)

    if commitment is None:
        audit.record_now(
            "DOCTOR_ACCESS_DENIED", actor_type="STAFF", actor_ref=principal.user_ref,
            result="DENIED", reason="UNKNOWN_OBJECT",
        )
        forbidden()

    authorize_doctor(db, principal, _patient_of(db, commitment.patient_id).ref, "INSTRUCTIONS")

    return commitment


def _loop_decision(db: Session, principal: StaffPrincipal, commitment: Commitment, action, *args, **kwargs):
    try:
        result = action(db, commitment.id, *args, **kwargs)
    except LoopActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    audit.record_now(
        "LOOP_DECISION", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="LOOP", object_ref=str(commitment.id), reason=action.__name__.upper(),
    )

    return result


@router.get("/commitments/{commitment_id}")
def commitment_detail(commitment_id: int, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    commitment = _commitment(db, principal, commitment_id)
    _, consent = authorize_doctor(db, principal, _patient_of(db, commitment.patient_id).ref, "INSTRUCTIONS")

    return _scoped_loops([serialize_loop(db, commitment, include_history=True)], scopes_of(consent))[0]


@router.post("/commitments/{commitment_id}/confirm-completion")
def confirm(commitment_id: int, body: LoopAction, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    """
    Human confirmation that a POTENTIAL_MATCH fulfils the instruction.
    The reviewer is the authenticated doctor, not a typed name.
    """

    commitment = _commitment(db, principal, commitment_id)

    return _loop_decision(
        db, principal, commitment, confirm_completion,
        confirmed_by=principal.display_name, match_id=body.match_id, note=body.note,
    )


@router.post("/commitments/{commitment_id}/keep-open")
def keep(commitment_id: int, body: LoopAction, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    commitment = _commitment(db, principal, commitment_id)

    return _loop_decision(db, principal, commitment, keep_open, reviewed_by=principal.display_name, note=body.note)


@router.post("/commitments/{commitment_id}/review")
def review(commitment_id: int, body: LoopAction, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    commitment = _commitment(db, principal, commitment_id)

    return _loop_decision(
        db, principal, commitment, mark_needs_review, reviewed_by=principal.display_name, note=body.note
    )


# ============================================================
# EVIDENCE
# ============================================================

def _evidence_scopes(db: Session, evidence: SourceEvidence) -> set[str]:
    needed = set()

    if db.query(Observation.id).filter(Observation.evidence_id == evidence.id).first():
        needed.add("LABS")

    if db.query(Commitment.id).filter(Commitment.evidence_id == evidence.id).first():
        needed.add("INSTRUCTIONS")

    if db.query(LoopMatch.id).filter(LoopMatch.evidence_id == evidence.id).first():
        needed.add("LABS")

    for (category,) in db.query(ClinicalFact.category).filter(ClinicalFact.evidence_id == evidence.id):
        needed.add(CATEGORY_SCOPE[category])

    return needed


def _authorize_evidence(db: Session, principal: StaffPrincipal, evidence_id: int, require_source: bool):
    evidence = db.get(SourceEvidence, evidence_id)

    if evidence is None:
        audit.record_now(
            "DOCTOR_ACCESS_DENIED", actor_type="STAFF", actor_ref=principal.user_ref,
            result="DENIED", reason="UNKNOWN_OBJECT",
        )
        forbidden()

    document = db.get(Document, evidence.document_id)
    patient = _patient_of(db, document.patient_id)

    patient, consent = authorize_doctor(db, principal, patient.ref, "SOURCE_DOCUMENTS" if require_source else None)

    if not (_evidence_scopes(db, evidence) & scopes_of(consent)):
        audit.record_now(
            "DOCTOR_ACCESS_DENIED", actor_type="STAFF", actor_ref=principal.user_ref,
            object_type="PATIENT", object_ref=patient.ref, result="DENIED", reason="SCOPE",
        )
        forbidden()

    return evidence, document, patient


@router.get("/evidence/{evidence_id}")
def evidence_detail(evidence_id: int, principal: StaffPrincipal = Depends(require_doctor), db: Session = Depends(get_db)):
    evidence, document, _ = _authorize_evidence(db, principal, evidence_id, require_source=False)

    result = serialize_evidence(evidence, document)
    result.pop("document_id", None)

    linked = []

    for observation in db.query(Observation).filter(Observation.evidence_id == evidence.id):
        linked.append({
            "type": "OBSERVATION", "id": observation.id,
            "label": f"{observation.observation_type} {observation.value}{(' ' + observation.unit) if observation.unit else ''}",
            "review_status": observation.review_status,
        })

    for commitment in db.query(Commitment).filter(Commitment.evidence_id == evidence.id):
        linked.append({
            "type": "COMMITMENT", "id": commitment.id, "label": commitment.instruction,
            "review_status": None, "state": commitment.state,
        })

    for fact in db.query(ClinicalFact).filter(ClinicalFact.evidence_id == evidence.id):
        linked.append({"type": fact.category, "id": fact.ref, "label": fact.label, "review_status": fact.review_status})

    result["linked_items"] = linked

    return result


@router.post("/evidence/{evidence_id}/view-token")
def issue_view_token(
    evidence_id: int,
    principal: StaffPrincipal = Depends(require_doctor),
    db: Session = Depends(get_db),
):
    limit("evidence_view_user", principal.user_ref)

    evidence, document, patient = _authorize_evidence(db, principal, evidence_id, require_source=True)

    token = secrets.token_urlsafe(32)

    db.add(
        EvidenceToken(
            token_hash=keyed_hash("evidence-token", token),
            session_hash=principal.session_hash,
            user_ref=principal.user_ref,
            patient_id=patient.id,
            document_id=document.id,
            evidence_id=evidence.id,
            expires_at=datetime.utcnow() + EVIDENCE_TOKEN_TTL,
        )
    )
    audit.record(
        db, "EVIDENCE_TOKEN_ISSUED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="DOCUMENT", object_ref=document.ref,
    )
    db.commit()

    return {"token": token, "expires_in": int(EVIDENCE_TOKEN_TTL.total_seconds())}


@router.get("/evidence/view/{token}")
async def view_evidence(
    token: str,
    principal: StaffPrincipal = Depends(require_doctor),
    db: Session = Depends(get_db),
):
    def reject(reason: str):
        audit.record_now(
            "EVIDENCE_TOKEN_REJECTED", actor_type="STAFF", actor_ref=principal.user_ref,
            result="DENIED", reason=reason,
        )
        forbidden()

    if len(token) > 64:
        reject("MALFORMED")

    row = db.query(EvidenceToken).filter(EvidenceToken.token_hash == keyed_hash("evidence-token", token)).first()
    now = datetime.utcnow()

    if row is None:
        reject("UNKNOWN_TOKEN")

    if row.used_at is not None:
        reject("TOKEN_REUSED")

    if row.expires_at <= now:
        reject("TOKEN_EXPIRED")

    if row.session_hash != principal.session_hash or row.user_ref != principal.user_ref:
        reject("SESSION_MISMATCH")

    row.used_at = now
    db.commit()

    # Consent is re-checked at view time, not only at issue time.
    patient = db.get(Patient, row.patient_id)
    authorize_doctor(db, principal, patient.ref, "SOURCE_DOCUMENTS")

    document = db.get(Document, row.document_id)
    evidence = db.get(SourceEvidence, row.evidence_id)

    try:
        plaintext = vault.load(db, document)
    except vault.VaultError:
        forbidden()

    bbox = (
        [evidence.bbox_x, evidence.bbox_y, evidence.bbox_w, evidence.bbox_h]
        if evidence.bbox_x is not None
        else None
    )
    watermark = f"{principal.user_ref} | {now.strftime('%Y-%m-%d %H:%M:%S')} UTC | JeevaFlow confidential - do not copy"

    try:
        rendered = await run_in_threadpool(
            run_worker, "render", plaintext, document.content_type,
            page=evidence.page_number or 1, bbox=bbox, watermark=watermark,
        )
    except WorkerError:
        raise HTTPException(status_code=422, detail="The evidence could not be displayed.")
    finally:
        del plaintext

    audit.record_now(
        "DOCUMENT_VIEWED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="DOCUMENT", object_ref=document.ref,
    )

    return Response(
        decode(rendered, "png"),
        media_type="image/png",
        headers={
            "Cache-Control": "no-store, private, max-age=0",
            "Pragma": "no-cache",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )
