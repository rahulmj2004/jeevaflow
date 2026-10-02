"""
Twilio WhatsApp webhook (untrusted channel entry point).

     body size cap -> HTTPS (production)
     -> X-Twilio-Signature (always enforced, fails closed; invalid attempts
         are rate-limited per IP)
    -> handle_inbound_message (replay, account, freshness,
       per-sender limit, identity, per-patient limit, processing)

Valid signed traffic is limited per sender and patient. This avoids
throttling unrelated senders that share a proxy or ngrok address.
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..identity import get_identity_db
from ..security import audit, ratelimit
from ..whatsapp import (
    MAX_WEBHOOK_BODY_BYTES,
    REPLIES,
    WHATSAPP_WEBHOOK_PATH,
    handle_inbound_message,
    is_valid_twilio_signature,
    twiml_messages,
)
from .deps import client_ip


router = APIRouter(tags=["whatsapp"])


def expected_webhook_url(request: Request) -> str:
    """
    The URL Twilio signed. Behind ngrok/a proxy the public URL is
    configured explicitly, because the local request URL differs.
    """

    if settings.public_base_url:
        url = settings.public_base_url + request.url.path
    else:
        url = str(request.url).split("?")[0]

    if request.url.query:
        url += "?" + request.url.query

    return url


def _xml(messages: list[str], status: int = 200) -> Response:
    return Response(twiml_messages(messages), status_code=status, media_type="application/xml")


async def process_webhook(request: Request, db: Session, idb: Session, **handlers) -> tuple[Response, dict]:
    declared = request.headers.get("content-length")

    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BODY_BYTES:
        return Response("Payload too large.", status_code=413, media_type="text/plain"), {"status": "TOO_LARGE"}

    if settings.is_production and not settings.public_base_url.startswith("https://"):
        return Response("HTTPS required.", status_code=403, media_type="text/plain"), {"status": "HTTPS_REQUIRED"}

    signing_token = settings.twilio_signing_token

    if not signing_token:
        # Fail closed: never accept unsigned webhooks.
        return Response("Webhook not configured.", status_code=503, media_type="text/plain"), {"status": "NOT_CONFIGURED"}

    try:
        form = await request.form()
        params = [(key, value) for key, value in form.multi_items() if isinstance(value, str)]
    except Exception:
        return _xml([REPLIES["MALFORMED"]], 400), {"status": "MALFORMED"}

    if not is_valid_twilio_signature(
        expected_webhook_url(request), params, request.headers.get("X-Twilio-Signature"), signing_token
    ):
        try:
            ratelimit.hit("webhook_ip", client_ip(request))
        except ratelimit.RateLimited as exc:
            audit.record_now(
                "WEBHOOK_RATE_LIMITED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
                result="DENIED", reason="IP",
            )
            return ratelimit.rate_limit_response(exc.retry_after), {
                "status": "RATE_LIMITED", "signature": "REJECTED", "retry_after": exc.retry_after,
            }

        audit.record_now(
            "WEBHOOK_SIGNATURE_REJECTED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            result="DENIED", reason="INVALID_SIGNATURE",
        )
        return Response("Invalid signature.", status_code=403, media_type="text/plain"), {
            "status": "SIGNATURE_REJECTED", "signature": "REJECTED",
        }

    audit.record_now(
        "WEBHOOK_SIGNATURE_VERIFIED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
    )

    outcome = await run_in_threadpool(handle_inbound_message, db, idb, dict(params), **handlers)

    if outcome.status == "RATE_LIMITED":
        return ratelimit.rate_limit_response(outcome.retry_after), {
            "status": "RATE_LIMITED", "signature": "VERIFIED", "retry_after": outcome.retry_after,
        }

    return _xml(outcome.messages), {
        "status": outcome.status,
        "signature": "VERIFIED",
        "messages": outcome.messages,
        "transaction_ref": outcome.transaction_ref,
        "replay": outcome.replay,
    }


@router.post(WHATSAPP_WEBHOOK_PATH)
async def whatsapp_webhook(
    request: Request,
    db: Session = Depends(get_db),
    idb: Session = Depends(get_identity_db),
):
    response, _ = await process_webhook(request, db, idb)

    return response
