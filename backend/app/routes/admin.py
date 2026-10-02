"""
Administration (ADMIN role). Admins manage enrolment and operations
but have no access to medical content.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..identity import get_identity_db
from ..models import WhatsAppMessage
from ..patient_lookup import enroll_patient
from ..retention import run_retention
from ..security import audit
from ..security.auth import StaffPrincipal, require_admin
from ..whatsapp import integration_status, serialize_message


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class EnrollRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., min_length=6, max_length=40)
    date_of_birth: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/patients")
def enroll(
    body: EnrollRequest,
    principal: StaffPrincipal = Depends(require_admin),
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    try:
        patient = enroll_patient(db, idb, body.name, body.phone, body.date_of_birth)
    except ValueError:
        # Generic: does not reveal whether the number is already enrolled.
        raise HTTPException(status_code=422, detail="The patient could not be enrolled.")

    audit.record_now(
        "PATIENT_ENROLLED", actor_type="STAFF", actor_ref=principal.user_ref,
        object_type="PATIENT", object_ref=patient.ref,
    )

    return {"patient_ref": patient.ref}


@router.get("/whatsapp/status")
def whatsapp_status(principal: StaffPrincipal = Depends(require_admin)):
    return integration_status()


@router.get("/whatsapp/messages")
def whatsapp_messages(
    limit: int = Query(20, ge=1, le=100),
    principal: StaffPrincipal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(WhatsAppMessage).order_by(WhatsAppMessage.id.desc()).limit(limit).all()

    return [serialize_message(row) for row in rows]


@router.post("/retention/run")
def retention(principal: StaffPrincipal = Depends(require_admin), db: Session = Depends(get_db)):
    return run_retention(db)
