"""
The judge walkthrough (docs/AI_DEMO.md) end to end through the real
HTTP API from a clean database: WhatsApp demo message -> OTP ->
consent -> second report -> doctor loops -> evidence finder.
Nothing is inserted directly into the database.
"""

import pytest

from app.followthrough import encoder as enc

from tests.conftest import case_of
from tests.test_security import consent_via_portal, demo_patient_ref, send_demo, verify_portal


pytestmark = pytest.mark.skipif(enc.verify() is not None, reason="encoder not fetched")


def test_judge_walkthrough_from_clean_database(client, doctor):
    # 1. Follow-up plan over WhatsApp, verified, consented to Dr. Example.
    verify_portal(client, send_demo(client, "followthrough_plan"))
    consent_via_portal(client)

    # 2. Later lab report, verified, shared under the existing consent.
    verify_portal(client, send_demo(client, "followthrough_labs"))
    consent_ref = client.get("/api/v1/portal/session").json()["consents"][0]["ref"]
    applied = client.post(f"/api/v1/portal/consents/{consent_ref}/apply")
    assert applied.status_code == 200, applied.text

    case = case_of(demo_patient_ref(client))
    loops = {loop["instruction"]: loop for loop in doctor.get(f"/api/v1/doctor/patients/{case}/loops").json()}

    # A: semantic match the keyword rules cannot make.
    psa = loops["Repeat PSA after 2 months"]
    assert psa["state"] == "POTENTIAL_MATCH"
    assert psa["potential_matches"][0]["method"] == "AI_SEMANTIC"

    # B: lexical trap considered and rejected.
    vitamin_d = loops["Check vitamin D levels after 3 months"]["potential_matches"]
    assert [m["observation"]["observation_type"] for m in vitamin_d] == ["Vitamin D (25-OH)"]

    # C: rule match kept; a visit instruction stays open.
    assert loops["Repeat kidney function test in 3 months"]["potential_matches"][0]["method"] == "RULE"
    assert loops["Review in 4 weeks"]["potential_matches"] == []

    # E: abstention recorded, loop unchanged.
    thyroid = loops["Check thyroid function after 6 weeks"]
    assert thyroid["ai_decision"]["decision"] == "AI_ABSTAINED"
    assert thyroid["potential_matches"] == []

    # D: the doctor finds the thyroid evidence the follow-through abstained on.
    answer = doctor.post(f"/api/v1/doctor/patients/{case}/evidence-finder", json={"query": "When was thyroid last checked?"}).json()
    assert answer["decision"] == "ANSWERED"
    assert answer["results"][0]["evidence"]["quote"] == "TSH: 3.2 mIU/L"

    unanswerable = doctor.post(f"/api/v1/doctor/patients/{case}/evidence-finder", json={"query": "MRI brain"}).json()
    assert unanswerable["decision"] == "AI_ABSTAINED"

    # Human decides: confirm the PSA suggestion.
    confirmed = doctor.post(f"/api/v1/doctor/commitments/{psa['id']}/confirm-completion", json={})
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "CLOSED"
