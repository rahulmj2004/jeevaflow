"""
Live end-to-end smoke test against a running server (demo mode).

    venv/bin/python test_api.py [http://127.0.0.1:8000]

Runs the whole secured flow with synthetic data:
WhatsApp (signed) -> OTP -> consent -> encrypted processing ->
doctor MFA -> consented form -> evidence view -> revoke -> denied
-> audit chain verified.
"""

import re
import sys

import httpx

from app.security.auth import totp_now
from app.seed import demo_totp_secret
from app.config import settings


BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ORIGIN = {"Origin": settings.cors_origins[0]}


def check(condition, label):
    print(("PASS " if condition else "FAIL ") + label)

    if not condition:
        sys.exit(1)


def staff(client, username):
    client.post(f"{BASE}/api/v1/auth/login", json={"username": username, "password": settings.demo_staff_password}, headers=ORIGIN)
    response = client.post(f"{BASE}/api/v1/auth/mfa", json={"code": totp_now(demo_totp_secret(username))}, headers=ORIGIN)
    check(response.status_code == 200, f"{username}: password + TOTP MFA")
    client.headers.update({"X-CSRF-Token": response.json()["csrf_token"], **ORIGIN})


def main():
    patient, doctor, auditor = httpx.Client(), httpx.Client(), httpx.Client()

    check(patient.get(f"{BASE}/api/v1/health").status_code == 200, "health")
    patient.post(f"{BASE}/api/v1/demo/reset", headers=ORIGIN)

    sent = patient.post(f"{BASE}/api/v1/demo/whatsapp", json={"document": "prescription"}, headers=ORIGIN).json()
    check(sent["signature"] == "VERIFIED", "WhatsApp webhook signature verified")
    check("securely received" in sent["messages"][0], "generic WhatsApp reply")

    otp = re.search(r"code is (\d{6})", sent["messages"][1]).group(1)
    verified = patient.post(
        f"{BASE}/api/v1/portal/verify", json={"transaction_ref": sent["transaction_ref"], "code": otp}, headers=ORIGIN
    )
    check(verified.status_code == 200, "OTP verified")
    patient.headers.update({"X-CSRF-Token": verified.json()["csrf_token"], **ORIGIN})

    session = patient.get(f"{BASE}/api/v1/portal/session").json()
    doctor_ref = next(d["ref"] for d in session["options"]["doctors"] if d["display_name"].startswith("Dr. Example"))
    granted = patient.post(
        f"{BASE}/api/v1/portal/consents",
        json={"doctor_ref": doctor_ref, "scopes": [s["key"] for s in session["options"]["scopes"]], "duration_days": 7},
    ).json()
    check(granted["processing"]["processed"] == 1, "consent granted, document processed")

    staff(doctor, "dr.example")
    patients = doctor.get(f"{BASE}/api/v1/doctor/patients").json()
    check(len(patients) == 1, "doctor sees only the consented patient")

    form = doctor.get(f"{BASE}/api/v1/doctor/patients/{patients[0]['patient_ref']}/form").json()
    check(any(m["label"] == "Metformin 500 mg" for m in form["medications"]), "doctor-ready form with medications")

    evidence_id = form["medications"][0]["evidence"]["evidence_id"]
    token = doctor.post(f"{BASE}/api/v1/doctor/evidence/{evidence_id}/view-token").json()["token"]
    view = doctor.get(f"{BASE}/api/v1/doctor/evidence/view/{token}")
    check(view.status_code == 200 and view.headers["content-type"] == "image/png", "watermarked evidence view")
    check(doctor.get(f"{BASE}/api/v1/doctor/evidence/view/{token}").status_code == 403, "evidence token is single use")

    patient.post(f"{BASE}/api/v1/portal/consents/{granted['consent']['ref']}/revoke")
    denied = doctor.get(f"{BASE}/api/v1/doctor/patients/{patients[0]['patient_ref']}/form")
    check(denied.status_code == 403, "access denied immediately after revocation")

    staff(auditor, "auditor")
    check(auditor.post(f"{BASE}/api/v1/audit/verify").json()["valid"], "audit chain verified")

    print("\nAll live checks passed.")


if __name__ == "__main__":
    main()
