from sqlalchemy.orm import Session

from .loops import effective_state
from .models import Commitment, Document, Observation, SourceEvidence
from .provenance import serialize_evidence


TYPE_ORDER = {
    "DOCUMENT": 0,
    "OBSERVATION": 1,
    "COMMITMENT": 2,
}


def get_patient_timeline(
    db: Session,
    patient_id: int,
):
    """
    Build a chronological health journey from documents,
    observations and doctor commitments.

    Items are dated by the report date stated in the source
    document when one was found, otherwise by the date received.
    Every observation/instruction carries its source evidence.
    """

    documents = {
        document.id: document
        for document in (
            db.query(Document)
            .filter(Document.patient_id == patient_id)
            .all()
        )
    }

    evidence_ids = set()

    observations = (
        db.query(Observation)
        .filter(
            Observation.patient_id == patient_id
        )
        .order_by(
            Observation.created_at.asc()
        )
        .all()
    )

    commitments = (
        db.query(Commitment)
        .filter(
            Commitment.patient_id == patient_id
        )
        .order_by(
            Commitment.created_at.asc()
        )
        .all()
    )

    for item in [*observations, *commitments]:
        if item.evidence_id is not None:
            evidence_ids.add(item.evidence_id)

    evidence = {
        row.id: row
        for row in (
            db.query(SourceEvidence)
            .filter(SourceEvidence.id.in_(evidence_ids))
            .all()
        )
    } if evidence_ids else {}

    def document_date(document_id, fallback):
        document = documents.get(document_id)

        if document is None:
            return fallback.date()

        return document.document_date or document.created_at.date()

    def evidence_for(evidence_id, document_id):
        return serialize_evidence(
            evidence.get(evidence_id),
            documents.get(document_id),
        )

    timeline = []

    # --------------------------------------------------------
    # Documents received
    # --------------------------------------------------------

    for document in documents.values():
        timeline.append(
            {
                "id": document.id,
                "type": "DOCUMENT",
                "title": "Document received",
                "value": document.label,
                "unit": None,
                "date": (
                    document.document_date
                    or document.created_at.date()
                ),
                "review_status": None,
                "evidence_id": None,
                "document_id": document.id,
                "source": document.source,
                "processing_status": document.processing_status,
                "quality_status": document.quality_status,
                "state": None,
                "due_date": None,
                "evidence": None,
                "created_at": document.created_at,
            }
        )

    # --------------------------------------------------------
    # Observations
    # --------------------------------------------------------

    for observation in observations:
        timeline.append(
            {
                "id": observation.id,
                "type": "OBSERVATION",
                "title": observation.observation_type,
                "value": observation.value,
                "unit": observation.unit,
                "date": (
                    observation.event_date
                    or document_date(
                        observation.document_id,
                        observation.created_at,
                    )
                ),
                "review_status": observation.review_status,
                "evidence_id": observation.evidence_id,
                "document_id": observation.document_id,
                "source": None,
                "processing_status": None,
                "quality_status": None,
                "state": None,
                "due_date": None,
                "evidence": evidence_for(
                    observation.evidence_id,
                    observation.document_id,
                ),
                "created_at": observation.created_at,
            }
        )

    # --------------------------------------------------------
    # Doctor instructions
    # --------------------------------------------------------

    for commitment in commitments:
        timeline.append(
            {
                "id": commitment.id,
                "type": "COMMITMENT",
                "title": "Doctor Instruction",
                "value": commitment.instruction,
                "unit": None,
                "date": document_date(
                    commitment.document_id,
                    commitment.created_at,
                ),
                "review_status": None,
                "evidence_id": commitment.evidence_id,
                "document_id": commitment.document_id,
                "source": None,
                "processing_status": None,
                "quality_status": None,
                "state": effective_state(commitment),
                "due_date": commitment.due_date,
                "evidence": evidence_for(
                    commitment.evidence_id,
                    commitment.document_id,
                ),
                "created_at": commitment.created_at,
            }
        )

    # --------------------------------------------------------
    # Sort everything chronologically
    # --------------------------------------------------------

    timeline.sort(
        key=lambda item: (
            item["date"],
            item["document_id"] or 0,
            TYPE_ORDER[item["type"]],
            item["id"],
        )
    )

    return timeline
