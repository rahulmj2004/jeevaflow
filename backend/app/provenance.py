"""
Quote-or-reject provenance.

An extracted item is only stored if its quote can be located in the
source page text. Items that cannot be anchored are dropped.
"""

from typing import Optional

from sqlalchemy.orm import Session

from .models import Document, SourceEvidence


def find_quote(text: str, quote: str):
    normalized_text = text.lower()
    normalized_quote = quote.lower().strip()

    if not normalized_quote:
        return None

    position = normalized_text.find(
        normalized_quote
    )

    if position == -1:
        return None

    return {
        "start_position": position,
        "end_position": position + len(normalized_quote),
    }


def locate_quote(
    page_text: str,
    quote: str,
    start_position: Optional[int] = None,
    end_position: Optional[int] = None,
):
    """
    Confirm the quote exists in the page text.

    Prefers the exact span reported by the extractor; falls back to
    the first occurrence. Returns None when the quote is not in the
    source (the caller must then reject the item).
    """

    if (
        start_position is not None
        and end_position is not None
        and page_text[start_position:end_position].lower()
        == quote.lower()
    ):
        return {
            "start_position": start_position,
            "end_position": end_position,
        }

    return find_quote(page_text, quote)


def serialize_evidence(
    evidence: Optional[SourceEvidence],
    document: Optional[Document] = None,
) -> Optional[dict]:
    if evidence is None:
        return None

    document = document or evidence.document

    return {
        "id": evidence.id,
        "evidence_id": evidence.id,
        "document_id": evidence.document_id,
        "document_filename": (
            document.label if document else None
        ),
        "document_source": (
            document.source if document else None
        ),
        "document_date": (
            document.document_date if document else None
        ),
        "document_received_at": (
            document.created_at if document else None
        ),
        "document_ref": document.ref if document else None,
        "quote": evidence.quote,
        "page_number": evidence.page_number,
        "start_position": evidence.start_position,
        "end_position": evidence.end_position,
        "bbox": (
            [evidence.bbox_x, evidence.bbox_y, evidence.bbox_w, evidence.bbox_h]
            if evidence.bbox_x is not None
            else None
        ),
        "confidence": evidence.confidence,
        "document_sha256": evidence.document_sha256,
        "pipeline_version": evidence.pipeline_version,
        "extracted_at": evidence.created_at,
    }


def load_evidence(
    db: Session,
    evidence_id: Optional[int],
) -> Optional[dict]:
    if evidence_id is None:
        return None

    evidence = db.get(SourceEvidence, evidence_id)

    return serialize_evidence(evidence)
