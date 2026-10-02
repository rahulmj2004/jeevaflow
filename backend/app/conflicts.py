from sqlalchemy.orm import Session

from .models import Document, Observation, ReviewStatus, SourceEvidence
from .provenance import serialize_evidence


def _value_key(value: str):
    try:
        return float(value)
    except (TypeError, ValueError):
        return (value or "").strip().lower()


def detect_observation_conflicts(
    db: Session,
    patient_id: int,
):
    """
    Detect conflicting values for the same observation type
    reported for the same date.

    Results on different dates are a longitudinal series, not a
    conflict. Rejected observations are excluded.

    IMPORTANT:
    This function does NOT decide which value
    is medically correct.

    It only identifies that multiple different
    values exist and requires human review.
    """

    observations = (
        db.query(Observation)
        .filter(
            Observation.patient_id == patient_id,
            Observation.review_status
            != ReviewStatus.REJECTED.value,
        )
        .order_by(
            Observation.created_at.asc(),
            Observation.id.asc(),
        )
        .all()
    )

    documents = {
        document.id: document
        for document in (
            db.query(Document)
            .filter(Document.patient_id == patient_id)
            .all()
        )
    }

    grouped = {}

    for observation in observations:
        document = documents.get(observation.document_id)

        event_date = observation.event_date or (
            document.document_date or document.created_at.date()
            if document
            else observation.created_at.date()
        )

        key = (
            observation.observation_type.strip().lower(),
            event_date,
        )

        grouped.setdefault(key, []).append(observation)

    conflicts = []

    for (observation_type, event_date), items in grouped.items():

        values = {
            (
                _value_key(item.value),
                (item.unit or "").lower(),
            )
            for item in items
        }

        if len(values) <= 1:
            continue

        conflicts.append(
            {
                "type": "CONFLICT",
                "observation_type": items[0].observation_type,
                "event_date": event_date,
                "status": "HUMAN_REVIEW_REQUIRED",
                "message": (
                    "Multiple different values "
                    "were found across source "
                    "documents. Human review required."
                ),
                "observations": [
                    {
                        "id": item.id,
                        "value": item.value,
                        "unit": item.unit,
                        "document_id": item.document_id,
                        "evidence_id": (
                            item.evidence_id
                        ),
                        "review_status": (
                            item.review_status
                        ),
                        "evidence": serialize_evidence(
                            db.get(SourceEvidence, item.evidence_id)
                            if item.evidence_id
                            else None,
                            documents.get(item.document_id),
                        ),
                    }
                    for item in items
                ],
            }
        )

    return conflicts
