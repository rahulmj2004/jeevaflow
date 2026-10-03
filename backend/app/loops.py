"""
Open Loops: documented instructions tracked until a human
confirms they have been followed through.

The system may move a loop to POTENTIAL_MATCH (see matching.py).
Only a human action can move a loop to CLOSED.
"""

import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from .models import (
    Commitment,
    Document,
    LoopEvent,
    LoopMatch,
    LoopState,
    MatchStatus,
    Observation,
)
from .provenance import load_evidence


ACTIVE_STATES = {
    LoopState.OPEN.value,
    LoopState.NEEDS_REVIEW.value,
    LoopState.POTENTIAL_MATCH.value,
}


class LoopActionError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ============================================================
# AUDIT
# ============================================================

def record_event(
    db: Session,
    commitment: Commitment,
    action: str,
    to_state: Optional[str],
    actor_type: str,
    actor_name: Optional[str] = None,
    match_id: Optional[int] = None,
    note: Optional[str] = None,
):
    event = LoopEvent(
        commitment_id=commitment.id,
        action=action,
        from_state=commitment.state,
        to_state=to_state,
        actor_type=actor_type,
        actor_name=actor_name,
        match_id=match_id,
        note=note,
    )

    db.add(event)

    if to_state is not None:
        commitment.state = to_state
        commitment.updated_at = datetime.utcnow()

    return event


# ============================================================
# READ
# ============================================================

def effective_state(
    commitment: Commitment,
    today: Optional[date] = None,
) -> str:
    """
    OPEN commitments are reported as OVERDUE once their
    due date has passed.
    """

    today = today or date.today()

    if (
        commitment.state == LoopState.OPEN.value
        and commitment.due_date is not None
        and commitment.due_date < today
    ):
        return LoopState.OVERDUE.value

    return commitment.state


def serialize_match(db: Session, match: LoopMatch) -> dict:
    observation = db.get(Observation, match.observation_id)
    document = db.get(Document, match.document_id)

    return {
        "id": match.id,
        "status": match.status,
        "rule": match.rule,
        "method": match.method,
        "score": match.score,
        "explanation": json.loads(match.explanation) if match.explanation else None,
        "observation": (
            {
                "id": observation.id,
                "observation_type": observation.observation_type,
                "value": observation.value,
                "unit": observation.unit,
                "event_date": observation.event_date,
                "review_status": observation.review_status,
            }
            if observation
            else None
        ),
        "document_id": match.document_id,
        "document_filename": (
            document.label if document else None
        ),
        "document_source": document.source if document else None,
        "evidence": load_evidence(db, match.evidence_id),
        "reviewed_by": match.reviewed_by,
        "reviewed_at": match.reviewed_at,
        "created_at": match.created_at,
    }


def serialize_loop(
    db: Session,
    commitment: Commitment,
    include_history: bool = False,
) -> dict:
    document = db.get(Document, commitment.document_id)

    matches = (
        db.query(LoopMatch)
        .filter(LoopMatch.commitment_id == commitment.id)
        .order_by(LoopMatch.created_at.desc())
        .all()
    )

    result = {
        "id": commitment.id,
        "instruction": commitment.instruction,
        "due_date": commitment.due_date,
        "state": effective_state(commitment),
        "stored_state": commitment.state,
        "evidence_id": commitment.evidence_id,
        "document_id": commitment.document_id,
        "document_filename": (
            document.label if document else None
        ),
        "document_date": document.document_date if document else None,
        "evidence": load_evidence(db, commitment.evidence_id),
        "potential_matches": [
            serialize_match(db, match)
            for match in matches
            if match.status == MatchStatus.PENDING.value
        ],
        "confirmed_match": next(
            (
                serialize_match(db, match)
                for match in matches
                if match.status == MatchStatus.CONFIRMED.value
            ),
            None,
        ),
        "created_at": commitment.created_at,
        "updated_at": commitment.updated_at,
    }

    if include_history:
        events = (
            db.query(LoopEvent)
            .filter(LoopEvent.commitment_id == commitment.id)
            .order_by(LoopEvent.created_at.asc(), LoopEvent.id.asc())
            .all()
        )

        result["history"] = [
            {
                "id": event.id,
                "action": event.action,
                "from_state": event.from_state,
                "to_state": event.to_state,
                "actor_type": event.actor_type,
                "actor_name": event.actor_name,
                "match_id": event.match_id,
                "note": event.note,
                "created_at": event.created_at,
            }
            for event in events
        ]

    return result


def get_open_loops(
    db: Session,
    patient_id: int,
):
    """
    Return all commitments for a patient, newest first.
    """

    commitments = (
        db.query(Commitment)
        .filter(
            Commitment.patient_id == patient_id
        )
        .order_by(
            Commitment.created_at.desc(),
            Commitment.id.desc(),
        )
        .all()
    )

    return [
        serialize_loop(db, commitment)
        for commitment in commitments
    ]


# ============================================================
# HUMAN ACTIONS
# ============================================================

def _get_commitment(db: Session, commitment_id: int) -> Commitment:
    commitment = db.get(Commitment, commitment_id)

    if commitment is None:
        raise LoopActionError(404, "Open loop not found")

    return commitment


def _require_reviewer(name: Optional[str]) -> str:
    if name is None or not name.strip():
        raise LoopActionError(
            422,
            "A reviewer name is required for this action.",
        )

    return name.strip()[:200]


def _pending_matches(db: Session, commitment_id: int):
    return (
        db.query(LoopMatch)
        .filter(
            LoopMatch.commitment_id == commitment_id,
            LoopMatch.status == MatchStatus.PENDING.value,
        )
        .order_by(LoopMatch.created_at.desc(), LoopMatch.id.desc())
        .all()
    )


def confirm_completion(
    db: Session,
    commitment_id: int,
    confirmed_by: Optional[str],
    match_id: Optional[int] = None,
    note: Optional[str] = None,
) -> dict:
    """
    Human confirmation that a potential match fulfils the
    instruction. This is the only path from POTENTIAL_MATCH
    to CLOSED.
    """

    commitment = _get_commitment(db, commitment_id)
    reviewer = _require_reviewer(confirmed_by)

    if commitment.state == LoopState.CLOSED.value:
        raise LoopActionError(409, "Open loop is already closed.")

    pending = _pending_matches(db, commitment.id)

    if not pending:
        raise LoopActionError(
            409,
            "There is no potential match awaiting confirmation "
            "for this open loop.",
        )

    if match_id is None:
        match = pending[0]
    else:
        match = next(
            (item for item in pending if item.id == match_id),
            None,
        )

        if match is None:
            raise LoopActionError(
                404,
                "Potential match not found for this open loop.",
            )

    now = datetime.utcnow()

    match.status = MatchStatus.CONFIRMED.value
    match.reviewed_by = reviewer
    match.reviewed_at = now

    for other in pending:
        if other.id != match.id:
            other.status = MatchStatus.DISMISSED.value
            other.reviewed_by = reviewer
            other.reviewed_at = now

    record_event(
        db,
        commitment,
        action="COMPLETION_CONFIRMED",
        to_state=LoopState.CLOSED.value,
        actor_type="HUMAN",
        actor_name=reviewer,
        match_id=match.id,
        note=note,
    )

    db.commit()

    return serialize_loop(db, commitment, include_history=True)


def keep_open(
    db: Session,
    commitment_id: int,
    reviewed_by: Optional[str],
    note: Optional[str] = None,
) -> dict:
    """
    Human decision that the potential match does not fulfil the
    instruction. Pending matches are dismissed; the loop reopens.
    """

    commitment = _get_commitment(db, commitment_id)
    reviewer = _require_reviewer(reviewed_by)

    if commitment.state == LoopState.CLOSED.value:
        raise LoopActionError(409, "Open loop is already closed.")

    now = datetime.utcnow()

    for match in _pending_matches(db, commitment.id):
        match.status = MatchStatus.DISMISSED.value
        match.reviewed_by = reviewer
        match.reviewed_at = now

    record_event(
        db,
        commitment,
        action="KEPT_OPEN",
        to_state=LoopState.OPEN.value,
        actor_type="HUMAN",
        actor_name=reviewer,
        note=note,
    )

    db.commit()

    return serialize_loop(db, commitment, include_history=True)


def mark_needs_review(
    db: Session,
    commitment_id: int,
    reviewed_by: Optional[str],
    note: Optional[str] = None,
) -> dict:
    """
    Flag a loop for clinical review. Pending matches stay pending.
    """

    commitment = _get_commitment(db, commitment_id)
    reviewer = _require_reviewer(reviewed_by)

    if commitment.state == LoopState.CLOSED.value:
        raise LoopActionError(409, "Open loop is already closed.")

    record_event(
        db,
        commitment,
        action="FLAGGED_FOR_REVIEW",
        to_state=LoopState.NEEDS_REVIEW.value,
        actor_type="HUMAN",
        actor_name=reviewer,
        note=note,
    )

    db.commit()

    return serialize_loop(db, commitment, include_history=True)


def close_loop(
    db: Session,
    commitment_id: int,
    closed_by: Optional[str] = None,
    note: Optional[str] = None,
):
    """
    Close an Open Loop by commitment ID.

    This endpoint is a direct human action (kept for backward
    compatibility); it is recorded in the audit trail.
    """

    commitment = db.get(Commitment, commitment_id)

    if commitment is None:
        return None

    if commitment.state != LoopState.CLOSED.value:
        record_event(
            db,
            commitment,
            action="CLOSED_MANUALLY",
            to_state=LoopState.CLOSED.value,
            actor_type="HUMAN",
            actor_name=closed_by,
            note=note,
        )

        db.commit()

    db.refresh(commitment)

    return serialize_loop(db, commitment)
