"""
DEMO IMPLEMENTATION: synthetic hackathon demo controls.

Available only when JEEVAFLOW_DEMO_MODE is on (never in production)
and only to loopback clients. Everything here acts on the single
synthetic demo patient and synthetic documents.

The WhatsApp simulator builds a Twilio-style form, signs it with the
configured signing token and sends it through the REAL webhook code
path (signature check, replay protection, OTP, quarantine, scan,
encryption). Only the Twilio media download is replaced by the local
synthetic file, and media deletion/freshness are reported SIMULATED.
The phone view shows the OTP so the demo can be run without a phone.
"""

import secrets
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from ..config import settings
from ..database import get_db
from ..demo import (
    CONFLICT_REPORT,
    DEMO_DOCUMENTS,
    FOLLOWTHROUGH_LABS,
    FOLLOWTHROUGH_PLAN,
    FOLLOWUP_REPORT,
    INITIAL_REPORT,
    PRESCRIPTION,
    demo_document_bytes,
    reset_patient_records,
)
from ..identity import get_identity_db
from ..models import OutboundMessage
from ..seed import DEMO_STAFF, demo_identity, demo_patient
from ..security import audit, ratelimit
from ..whatsapp import REPLIES, WHATSAPP_WEBHOOK_PATH, compute_twilio_signature, portal_link
from .deps import client_ip
from .webhook import expected_webhook_url, process_webhook


router = APIRouter(prefix="/api/v1/demo", tags=["demo"])

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}

DOCUMENTS = {
    "prescription": (PRESCRIPTION, "Synthetic prescription"),
    "lab_report": (INITIAL_REPORT, "Synthetic lab report (15 Jun 2026)"),
    "followup": (FOLLOWUP_REPORT, "Synthetic follow-up HbA1c (20 Sep 2026)"),
    "conflict": (CONFLICT_REPORT, "Synthetic transcribed copy (same day, different value)"),
    "followthrough_plan": (FOLLOWTHROUGH_PLAN, "AI follow-through: follow-up plan (5 Oct 2026)"),
    "followthrough_labs": (FOLLOWTHROUGH_LABS, "AI follow-through: later lab report (28 Dec 2026)"),
}


LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "testserver"}


def demo_only(request: Request):
    """
    Demo mode, a loopback client, a local Host header and no proxy
    headers. Tunnels such as ngrok also connect from loopback, so the
    Host / X-Forwarded-For checks keep the demo controls off the
    public URL.
    """

    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()

    if (
        not settings.demo_mode
        or client_ip(request) not in LOOPBACK
        or host not in LOCAL_HOSTS
        or request.headers.get("x-forwarded-for")
        or request.headers.get("forwarded")
    ):
        raise HTTPException(status_code=404, detail="Not found.")


class SimulateRequest(BaseModel):
    document: Optional[str] = None
    tamper_signature: bool = False


@router.get("", dependencies=[Depends(demo_only)])
def demo_info(db: Session = Depends(get_db), idb: Session = Depends(get_identity_db)):
    identity = demo_identity(idb)
    patient = demo_patient(db)

    return {
        "enabled": True,
        "patient": {
            "ref": patient.ref if patient else None,
            "phone_masked": identity.phone_masked if identity else None,
        },
        "documents": [{"key": key, "name": name, "title": title} for key, (name, title) in DOCUMENTS.items()],
        "staff": [{"username": u, "display_name": d, "role": r} for u, d, r in DEMO_STAFF],
        "signing": "TWILIO_AUTH_TOKEN" if settings.twilio_auth_token else "DEMO_SIGNING_TOKEN",
        "portal_base_url": settings.portal_base_url,
    }


@router.post("/whatsapp", dependencies=[Depends(demo_only)])
async def simulate_whatsapp(
    body: SimulateRequest,
    request: Request,
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    identity = demo_identity(idb)

    if identity is None or not identity.phone:
        raise HTTPException(status_code=409, detail="Demo patient is not set up.")

    if body.document is not None and body.document not in DOCUMENTS:
        raise HTTPException(status_code=422, detail="Unknown demo document.")

    account = settings.twilio_account_sid or "AC" + "0" * 32
    sid = ("MM" if body.document else "SM") + secrets.token_hex(16)

    form = {
        "AccountSid": account,
        "MessageSid": sid,
        "From": f"whatsapp:{identity.phone}",
        "To": settings.twilio_whatsapp_number or "whatsapp:+14155238886",
        "Body": "" if body.document else "Hi",
        "NumMedia": "1" if body.document else "0",
    }

    content = None

    if body.document:
        name = DOCUMENTS[body.document][0]
        content = demo_document_bytes(name)
        form["MediaUrl0"] = (
            f"https://api.twilio.com/2010-04-01/Accounts/{account}/Messages/{sid}/Media/ME{secrets.token_hex(16)}"
        )
        form["MediaContentType0"] = "application/pdf"

    encoded = urlencode(form).encode()

    scope = {
        "type": "http",
        "method": "POST",
        "path": WHATSAPP_WEBHOOK_PATH,
        "raw_path": WHATSAPP_WEBHOOK_PATH.encode(),
        "query_string": b"",
        "scheme": request.url.scheme,
        "server": request.scope.get("server"),
        "client": request.scope.get("client"),
        "root_path": "",
        "headers": [
            (b"host", request.headers.get("host", "localhost").encode()),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(encoded)).encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    simulated = StarletteRequest(scope, receive)

    signature = compute_twilio_signature(
        expected_webhook_url(simulated), sorted(form.items()), settings.twilio_signing_token
    )

    if body.tamper_signature:
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]

    scope["headers"].append((b"x-twilio-signature", signature.encode()))
    simulated = StarletteRequest(scope, receive)

    response, outcome = await process_webhook(
        simulated, db, idb,
        downloader=lambda url: content,
        deleter=lambda url: None,
        freshness=lambda payload: "SIMULATED",
    )

    if response.status_code == 429:
        # Mirror the webhook's 429 so the console shows the real
        # abuse-prevention response (body + Retry-After).
        return ratelimit.rate_limit_response(
            outcome["retry_after"],
            {
                "http_status": 429,
                "signature": outcome.get("signature"),
                "status": "RATE_LIMITED",
                "messages": [],
                "transaction_ref": None,
                "portal_link": None,
                "twiml": None,
            },
        )

    return {
        "http_status": response.status_code,
        "signature": outcome.get("signature", "REJECTED" if response.status_code == 403 else None),
        "status": outcome.get("status"),
        "messages": outcome.get("messages", []),
        "transaction_ref": outcome.get("transaction_ref"),
        "portal_link": portal_link(outcome["transaction_ref"]) if outcome.get("transaction_ref") else None,
        "twiml": response.body.decode() if response.media_type == "application/xml" else None,
    }


@router.get("/phone", dependencies=[Depends(demo_only)])
def demo_phone(db: Session = Depends(get_db)):
    """
    Notifications the synthetic patient's phone has received after
    the first reply (generic templates only).
    """

    patient = demo_patient(db)

    if patient is None:
        return []

    rows = (
        db.query(OutboundMessage)
        .filter(OutboundMessage.patient_id == patient.id)
        .order_by(OutboundMessage.id.asc())
        .all()
    )

    return [
        {"kind": row.kind, "body": REPLIES[row.kind], "delivery": row.delivery_status, "created_at": row.created_at}
        for row in rows
    ]


@router.post("/reset", dependencies=[Depends(demo_only)])
def reset_demo(db: Session = Depends(get_db)):
    patient = demo_patient(db)

    if patient is None:
        raise HTTPException(status_code=409, detail="Demo patient is not set up.")

    counts = reset_patient_records(db, patient.id)

    audit.record_now(
        "DEMO_RESET", actor_type="SYSTEM", actor_ref="demo-console",
        object_type="PATIENT", object_ref=patient.ref,
    )

    return {"deleted": counts}


@router.post("/rate-limit/reset", dependencies=[Depends(demo_only)])
def reset_rate_limits():
    """
    Clear in-memory rate-limit counters only. Patient data, documents,
    consent, audit events, keys, users and WhatsApp records are not
    touched. Unavailable (404) outside demo mode and in production.
    """

    cleared = ratelimit.reset()

    audit.record_now("DEMO_RATE_LIMIT_RESET", actor_type="SYSTEM", actor_ref="demo-console")

    return {"cleared": cleared}


@router.get("/files/{name}", dependencies=[Depends(demo_only)])
def demo_file(name: str):
    if name not in DEMO_DOCUMENTS:
        raise HTTPException(status_code=404, detail="Not found.")

    return Response(
        demo_document_bytes(name),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )
