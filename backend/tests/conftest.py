"""
Test configuration.

Every test runs against fresh temporary medical and identity
databases and a fresh vault, with freshly generated keys. No network
access and no Twilio credentials are needed. All data is synthetic.
"""

import base64
import io
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="jeevaflow-tests-"))


def _key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


os.environ.update({
    "JEEVAFLOW_ENV": "development",
    "JEEVAFLOW_DEMO_MODE": "true",
    "JEEVAFLOW_DATABASE_URL": f"sqlite:///{_TMP / 'medical.db'}",
    "JEEVAFLOW_IDENTITY_DATABASE_URL": f"sqlite:///{_TMP / 'identity.db'}",
    "JEEVAFLOW_VAULT_DIR": str(_TMP / "vault"),
    "JEEVAFLOW_KEK": _key(),
    "JEEVAFLOW_IDENTITY_KEY": _key(),
    "JEEVAFLOW_OTP_SECRET": _key(),
    "JEEVAFLOW_AUDIT_KEY": _key(),
    "JEEVAFLOW_SESSION_SECRET": _key(),
    "JEEVAFLOW_DEMO_STAFF_PASSWORD": "Synthetic-Test-Password-1",
    "JEEVAFLOW_DEMO_TOTP_SEED": _key(),
    "JEEVAFLOW_DEMO_TWILIO_SIGNING_TOKEN": "synthetic-signing-token",
    "JEEVAFLOW_DEMO_PATIENT_PHONE": "+91 98765 43210",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "TWILIO_API_KEY_SID": "",
    "TWILIO_API_KEY_SECRET": "",
    "TWILIO_WHATSAPP_NUMBER": "",
    "PUBLIC_BASE_URL": "",
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.identity import IdentityBase, IdentitySessionLocal, identity_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import DocumentSource, Patient  # noqa: E402
from app.patient_lookup import enroll_patient  # noqa: E402
from app.security import consent as consent_module  # noqa: E402
from app.security import ratelimit  # noqa: E402
from app.security.auth import totp_now  # noqa: E402
from app.seed import demo_totp_secret  # noqa: E402


STAFF_PASSWORD = os.environ["JEEVAFLOW_DEMO_STAFF_PASSWORD"]
ALL_SCOPES = ["DEMOGRAPHICS", "MEDICATIONS", "LABS", "ALLERGIES", "INSTRUCTIONS", "SOURCE_DOCUMENTS"]


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    IdentityBase.metadata.drop_all(bind=identity_engine)
    ratelimit.reset()

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def other_client(client):
    """
    A second browser (separate cookie jar) against the same app.
    """

    with TestClient(app) as second:
        yield second


# ============================================================
# STAFF
# ============================================================

def staff_ref(username: str) -> str:
    from app.identity import StaffUser

    idb = IdentitySessionLocal()

    try:
        return idb.query(StaffUser).filter(StaffUser.username == username).one().ref
    finally:
        idb.close()


def login(test_client, username: str = "dr.example") -> str:
    """
    Password + TOTP. Returns the CSRF token and sets it as a default
    header on the client.
    """

    response = test_client.post("/api/v1/auth/login", json={"username": username, "password": STAFF_PASSWORD})
    assert response.status_code == 200, response.text

    from app.identity import StaffUser

    # Each TOTP step is single-use; clear the last step so tests can
    # log in repeatedly within one 30-second window.
    idb = IdentitySessionLocal()
    try:
        user = idb.query(StaffUser).filter(StaffUser.username == username).one()
        user.totp_last_step = None
        idb.commit()
    finally:
        idb.close()

    response = test_client.post("/api/v1/auth/mfa", json={"code": totp_now(demo_totp_secret(username))})
    assert response.status_code == 200, response.text

    csrf = response.json()["csrf_token"]
    test_client.headers["X-CSRF-Token"] = csrf

    return csrf


@pytest.fixture()
def doctor(other_client):
    login(other_client, "dr.example")
    return other_client


# ============================================================
# PATIENTS AND CONSENT
# ============================================================

def new_patient(name: str = "Synthetic Test Patient", phone: str = "+91 91234 56789"):
    db, idb = SessionLocal(), IdentitySessionLocal()

    try:
        patient = enroll_patient(db, idb, name, phone, "1990-01-01")
        return SimpleNamespace(id=patient.id, ref=patient.ref)
    finally:
        db.close()
        idb.close()


def grant_consent(patient_ref: str, doctor_username: str = "dr.example", scopes=None, days: int = 7):
    from app.identity import StaffUser

    db, idb = SessionLocal(), IdentitySessionLocal()

    try:
        patient = db.query(Patient).filter(Patient.ref == patient_ref).one()
        user = idb.query(StaffUser).filter(StaffUser.username == doctor_username).one()
        consent = consent_module.grant(
            db, patient, user.ref, user.display_name, scopes or ALL_SCOPES, days
        )
        db.commit()
        return consent.ref
    finally:
        db.close()
        idb.close()


def case_of(patient_ref: str) -> str:
    """
    The doctor-facing case alias for a patient ref.
    """

    db = SessionLocal()

    try:
        return db.query(Patient).filter(Patient.ref == patient_ref).one().case_alias
    finally:
        db.close()


@pytest.fixture()
def patient_ref(client):
    patient = new_patient()
    grant_consent(patient.ref)
    return patient.ref


def ingest(patient_ref: str, content: bytes, content_type: str = "application/pdf", source: str = DocumentSource.WEB.value):
    """
    Run the secure pipeline directly (receive + process). Returns the
    ingestion result dict.
    """

    from app.pipeline import ingest_document

    db = SessionLocal()

    try:
        patient = db.query(Patient).filter(Patient.ref == patient_ref).one()
        return ingest_document(db, patient, content, content_type, source).to_dict(db)
    finally:
        db.close()


# ============================================================
# WHATSAPP
# ============================================================

def signed_post(test_client, form: dict, token: str = "synthetic-signing-token", url: str = "http://testserver/api/v1/webhooks/whatsapp"):
    from app.whatsapp import compute_twilio_signature

    signature = compute_twilio_signature(url, sorted(form.items()), token)

    return test_client.post(
        "/api/v1/webhooks/whatsapp", data=form, headers={"X-Twilio-Signature": signature}
    )


def message_sid(prefix: str = "MM") -> str:
    return prefix + secrets.token_hex(16)


def extract_otp(text: str) -> str:
    return re.search(r"code is (\d{6})", text).group(1)


# ============================================================
# DOCUMENTS (synthetic)
# ============================================================

def make_pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    content = document.tobytes(no_new_id=True)
    document.close()
    return content


def make_scanned_pdf(text: str) -> bytes:
    jpeg = make_image(text, image_format="JPEG")
    document = pymupdf.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=jpeg)
    content = document.tobytes(no_new_id=True, deflate=True)
    document.close()
    return content


def make_image(text: str, dpi: int = 200, blur: float = 0, image_format: str = "PNG") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    pixmap = page.get_pixmap(dpi=dpi)
    document.close()

    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")

    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))

    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


INITIAL_REPORT_TEXT = """SYNTHETIC TEST REPORT
Report Date: 15/06/2026
HbA1c: 9.4 %
Glucose: 186 mg/dL
Repeat HbA1c after 3 months.
Follow-up with physician.
"""

FOLLOWUP_REPORT_TEXT = """SYNTHETIC TEST REPORT
Report Date: 20/09/2026
HbA1c: 8.1 %
"""

PRESCRIPTION_TEXT = """SYNTHETIC TEST PRESCRIPTION
Dr. Synthetic Prescriber
Reg. No: SYN-00001
Date: 20/09/2026
Allergies: Penicillin (rash)
1. Tab Metformin 500 mg 1-0-1 x 30 days
2. Tab Glimepiride 1 mg
3. Tab Zyxorin 50 mg BD x 5 days
"""
