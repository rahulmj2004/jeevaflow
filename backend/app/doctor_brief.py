"""
Doctor-ready form.

Arranges source-linked, consented data for clinical review:

    Case ID (random alias, never identity), Medications (dose, frequency, duration),
    Laboratory results, Allergies, Source-supported notes
    (instructions), Uncertain information, Missing information,
    Source evidence

Only scopes covered by the patient's active consent are included;
the rest are listed as "not shared". Nothing is interpreted, values
are never marked better or worse, and every item links to its
evidence. Rejected items are left out.

Pseudonymized: no name, phone, date of birth, address or ID numbers.
The patient appears only as a random case alias (plus an age band
when DEMOGRAPHICS is shared), and every free-text string is passed
through mask_pii() before it leaves the server.
"""

import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from .conflicts import detect_observation_conflicts
from .identity import get_identity
from .loops import get_open_loops
from .models import (
    ClinicalFact,
    Consent,
    Document,
    FactState,
    Observation,
    Patient,
    ReviewStatus,
    SourceEvidence,
)
from .security.consent import SCOPES, scopes_of
from .security.masking import identity_terms, mask_tree


NOTICE = (
    "Compiled from documents the patient chose to share. Values are shown "
    "as written in the source and are not interpreted. Items marked "
    "Uncertain or AI-inferred need confirmation against the source. "
    "JeevaFlow does not diagnose, prescribe or change medication."
)

STATE_ORDER = {"OVERDUE": 0, "POTENTIAL_MATCH": 1, "NEEDS_REVIEW": 2, "OPEN": 3, "CLOSED": 4}


def _evidence(evidence: Optional[SourceEvidence], document: Optional[Document]) -> Optional[dict]:
    if evidence is None:
        return None

    return {
        "evidence_id": evidence.id,
        "document_ref": document.ref if document else None,
        "document_label": document.label if document else None,
        "document_date": document.document_date if document else None,
        "page_number": evidence.page_number,
        "quote": evidence.quote,
        "has_region": evidence.bbox_x is not None,
        "confidence": evidence.confidence,
    }


def _age_band(date_of_birth: Optional[str]) -> Optional[str]:
    try:
        born = date.fromisoformat(date_of_birth or "")
    except ValueError:
        return None

    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    if age < 0:
        return None

    return f"{(age // 10) * 10}-{(age // 10) * 10 + 9}" if age < 90 else "90+"


def _observation_date(observation: Observation, document: Optional[Document]):
    return (
        observation.event_date
        or (document.document_date if document else None)
        or observation.created_at.date()
    )


def build_doctor_form(db: Session, idb: Session, patient: Patient, consent: Consent) -> dict:
    scopes = scopes_of(consent)

    documents = {
        document.id: document
        for document in db.query(Document).filter(
            Document.patient_id == patient.id,
            Document.processing_status == "PROCESSED",
        )
    }

    evidence_rows = {
        row.id: row
        for row in db.query(SourceEvidence).filter(SourceEvidence.document_id.in_(list(documents) or [-1]))
    }

    def ev(evidence_id, document_id):
        return _evidence(evidence_rows.get(evidence_id), documents.get(document_id))

    uncertain: list[dict] = []
    missing: list[dict] = []

    # ---------------- case (pseudonymous) ----------------
    # Identity is read only to build masking terms and an age band;
    # it is never placed in the response.
    identity = get_identity(idb, patient.ref)
    terms = identity_terms(identity.name, identity.phone) if identity else []

    patient_section = {
        "case_alias": patient.case_alias,
        "age_band": _age_band(identity.date_of_birth) if identity and "DEMOGRAPHICS" in scopes else None,
        "demographics_shared": "DEMOGRAPHICS" in scopes,
    }

    facts = (
        db.query(ClinicalFact)
        .filter(
            ClinicalFact.patient_id == patient.id,
            ClinicalFact.document_id.in_(list(documents) or [-1]),
            ClinicalFact.review_status != "REJECTED",
        )
        .order_by(ClinicalFact.created_at.asc())
        .all()
    )

    def drift_of(raw: Optional[str], section: str, item: str, evidence, fact_ref: Optional[str] = None):
        """
        Parse a stored drift result and list each finding as an
        uncertain item pointing at the original evidence.
        """

        drift = json.loads(raw) if raw else None

        for finding in (drift or {}).get("findings", []):
            uncertain.append({
                "section": section, "item": item, "field": finding["field"],
                "note": f"Possible extraction drift: {finding['reason']} {finding['action']}.",
                "fact_ref": fact_ref, "evidence": evidence, "drift_code": finding["code"],
            })

        return drift

    def fact_item(fact: ClinicalFact) -> dict:
        fields = json.loads(fact.fields)

        for name, value in fields.items():
            if value["state"] == FactState.UNCERTAIN.value:
                uncertain.append({
                    "section": fact.category, "item": fact.label, "field": name,
                    "note": value.get("note"), "fact_ref": fact.ref,
                    "evidence": ev(fact.evidence_id, fact.document_id),
                })
            elif value["state"] == FactState.MISSING.value and fact.category != "PRESCRIBER":
                missing.append({
                    "section": fact.category, "item": fact.label, "field": name,
                    "note": value.get("note"),
                })

        return {
            "ref": fact.ref,
            "label": fact.label,
            "fields": fields,
            "state": fact.state,
            "review_status": fact.review_status,
            "confidence": fact.confidence,
            "evidence": ev(fact.evidence_id, fact.document_id),
            "drift": drift_of(
                fact.drift, fact.category, fact.label, ev(fact.evidence_id, fact.document_id), fact.ref
            ),
        }

    # ---------------- medications + prescriber ----------------
    medications, prescribers = None, None

    if "MEDICATIONS" in scopes:
        medications = [fact_item(f) for f in facts if f.category == "MEDICATION"]
        prescribers = [fact_item(f) for f in facts if f.category == "PRESCRIBER"]

        if not medications:
            missing.append({"section": "MEDICATION", "item": None, "field": None,
                            "note": "No medications found in shared documents."})

    # ---------------- allergies ----------------
    allergies = None

    if "ALLERGIES" in scopes:
        allergies = [fact_item(f) for f in facts if f.category == "ALLERGY"]

        if not allergies:
            missing.append({"section": "ALLERGY", "item": None, "field": None,
                            "note": "Allergy status is not documented in shared documents."})

    # ---------------- laboratory results ----------------
    labs, conflicts = None, []

    if "LABS" in scopes:
        conflicts = detect_observation_conflicts(db=db, patient_id=patient.id)
        conflicted = {c["observation_type"] for c in conflicts}

        groups: dict[str, list[dict]] = {}

        for observation in (
            db.query(Observation)
            .filter(
                Observation.patient_id == patient.id,
                Observation.document_id.in_(list(documents) or [-1]),
                Observation.review_status != ReviewStatus.REJECTED.value,
            )
            .all()
        ):
            document = documents.get(observation.document_id)
            item = {
                "id": observation.id,
                "value": observation.value,
                "unit": observation.unit,
                "date": _observation_date(observation, document),
                "review_status": observation.review_status,
                "state": observation.fact_state,
                "note": observation.fact_note,
                "evidence": ev(observation.evidence_id, observation.document_id),
            }
            item["drift"] = drift_of(observation.drift, "LAB", observation.observation_type, item["evidence"])
            groups.setdefault(observation.observation_type, []).append(item)

            if observation.fact_state == FactState.UNCERTAIN.value and not (item["drift"] or {}).get("findings"):
                uncertain.append({
                    "section": "LAB", "item": observation.observation_type, "field": "value",
                    "note": observation.fact_note, "evidence": item["evidence"],
                })

        labs = []

        for test, series in groups.items():
            series.sort(key=lambda entry: (entry["date"], entry["id"]))
            labs.append({"test": test, "latest": series[-1], "series": series, "has_conflict": test in conflicted})

            if test in conflicted:
                uncertain.append({
                    "section": "LAB", "item": test, "field": "value",
                    "note": "Conflicting values on the same date.", "evidence": None,
                })

        labs.sort(key=lambda group: group["latest"]["date"], reverse=True)

    # ---------------- source-supported notes (instructions) ----------------
    notes = None

    if "INSTRUCTIONS" in scopes:
        notes = [
            {
                "id": loop["id"],
                "instruction": loop["instruction"],
                "state": loop["state"],
                "due_date": loop["due_date"],
                "documented_on": loop["document_date"],
                "evidence": ev(loop["evidence_id"], loop["document_id"]),
            }
            for loop in get_open_loops(db=db, patient_id=patient.id)
            if loop["document_id"] in documents
        ]
        notes.sort(key=lambda item: (STATE_ORDER.get(item["state"], 9), item["due_date"] is None, str(item["due_date"])))

    # ---------------- source evidence ----------------
    sources = [
        {
            "ref": document.ref,
            "label": document.label,
            "source": document.source,
            "document_date": document.document_date,
            "received_at": document.created_at,
            "pages": document.page_count,
            "sha256": document.sha256,
            "scan_engine": document.scan_engine,
            "content_kind": document.content_kind,
            "needs_manual_review": document.needs_manual_review,
        }
        for document in sorted(documents.values(), key=lambda d: d.created_at)
    ]

    # Handwritten / mixed / unclear documents: flagged for review even
    # when nothing could be extracted from them.
    for document in sorted(documents.values(), key=lambda d: d.created_at):
        if document.needs_manual_review:
            uncertain.append({
                "section": "DOCUMENT", "item": document.label, "field": None,
                "note": (
                    "Handwritten document: needs doctor review."
                    if document.content_kind in ("HANDWRITTEN", "MIXED")
                    else "Document could not be classified: needs doctor review."
                ),
                "evidence": None,
            })

    return mask_tree({
        "generated_at": datetime.utcnow(),
        "patient": patient_section,
        "consent": {
            "ref": consent.ref,
            "purpose": consent.purpose,
            "scopes": sorted(scopes),
            "expires_at": consent.expires_at,
            "can_view_source": "SOURCE_DOCUMENTS" in scopes,
        },
        "not_shared": [
            {"scope": key, "label": label} for key, label in SCOPES.items() if key not in scopes
        ],
        "medications": medications,
        "prescribers": prescribers,
        "labs": labs,
        "conflicts": conflicts if "LABS" in scopes else [],
        "allergies": allergies,
        "notes": notes,
        "uncertain": uncertain,
        "missing": missing,
        "sources": sources,
        "notice": NOTICE,
    }, terms)
