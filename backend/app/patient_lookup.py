"""
Patient identification.

This module only answers "which registered patient is this?".
It never interprets medical content and never creates patients.
"""

import re
from typing import Optional

from sqlalchemy.orm import Session

from .config import settings
from .identity import PatientIdentity, find_patient_ref_by_phone
from .models import Patient
from .security.crypto import identity_index, new_ref


def normalize_phone(
    raw: Optional[str],
    default_country_code: Optional[str] = None,
) -> Optional[str]:
    """
    Normalize a phone number to E.164 (e.g. +919876543210).

    Accepted forms include:
        whatsapp:+919876543210
        +919876543210
        919876543210
        +91 98765 43210
        09876543210
        9876543210

    Returns None when the input cannot be normalized safely.
    """

    if raw is None:
        return None

    country = (
        default_country_code
        or settings.default_country_code
    ).lstrip("+")

    value = str(raw).strip()

    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]

    value = value.strip()

    has_plus = value.startswith("+")

    digits = re.sub(r"\D", "", value)

    if not digits:
        return None

    if has_plus:
        e164 = digits

    elif digits.startswith("00"):
        e164 = digits[2:]

    elif len(digits) == 10:
        e164 = country + digits

    elif len(digits) == 11 and digits.startswith("0"):
        e164 = country + digits[1:]

    elif (
        digits.startswith(country)
        and len(digits) == len(country) + 10
    ):
        e164 = digits

    else:
        return None

    # E.164 allows at most 15 digits; anything under 8 is not
    # a usable mobile number.
    if not 8 <= len(e164) <= 15:
        return None

    return "+" + e164


def mask_phone(raw: Optional[str]) -> Optional[str]:
    """
    Mask a phone number for UI display (last 2 digits only).
    """

    normalized = normalize_phone(raw)

    value = normalized or (raw or "")

    if len(value) <= 4:
        return "****"

    return value[:3] + "*" * (len(value) - 5) + value[-2:]


class PatientLookupResult:
    def __init__(self, status: str, patient: Optional[Patient] = None):
        # FOUND | NOT_FOUND | INVALID_PHONE
        self.status = status
        self.patient = patient


def find_patient_by_phone(
    db: Session,
    idb: Session,
    raw_phone: Optional[str],
) -> PatientLookupResult:
    """
    Identity lookup through the HMAC blind index in the identity
    database. The phone number is never compared in plaintext and
    never stored in the medical database.
    """

    normalized = normalize_phone(raw_phone)

    if normalized is None:
        return PatientLookupResult("INVALID_PHONE")

    patient_ref = find_patient_ref_by_phone(idb, normalized)

    if patient_ref is None:
        return PatientLookupResult("NOT_FOUND")

    patient = db.query(Patient).filter(Patient.ref == patient_ref).first()

    if patient is None:
        return PatientLookupResult("NOT_FOUND")

    return PatientLookupResult("FOUND", patient=patient)


def enroll_patient(
    db: Session,
    idb: Session,
    name: str,
    phone: Optional[str],
    date_of_birth: Optional[str] = None,
    synthetic: bool = False,
) -> Patient:
    """
    Create the pseudonymous medical record and the separate identity
    record. Raises ValueError for an invalid or already-enrolled
    phone number (callers return a generic error).
    """

    normalized = normalize_phone(phone) if phone else None

    if phone and normalized is None:
        raise ValueError("INVALID_PHONE")

    if normalized and find_patient_ref_by_phone(idb, normalized):
        raise ValueError("ALREADY_ENROLLED")

    patient = Patient(ref=new_ref("pt"))
    db.add(patient)
    db.flush()

    idb.add(
        PatientIdentity(
            patient_ref=patient.ref,
            phone_index=identity_index(normalized) if normalized else None,
            phone=normalized,
            phone_masked=mask_phone(normalized) if normalized else None,
            name=name,
            date_of_birth=date_of_birth,
            synthetic=synthetic,
        )
    )

    idb.commit()
    db.commit()

    return patient
