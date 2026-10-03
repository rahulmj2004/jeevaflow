"""
WhatsApp via Twilio: an UNTRUSTED transport.

    WhatsApp -> Twilio -> signed webhook -> replay / account /
    freshness checks -> enrolled? -> transaction + OTP
    -> media fetched server-side -> secure ingestion (quarantine)
    -> media deleted from Twilio

Replies are generic workflow messages only: never diagnoses,
medications, results, prescription text, history or previews.
The OTP is delivered to the registered WhatsApp number; it is not
stored or logged. Credentials come from the environment, REST calls
prefer a restricted API key, and credentials are only ever sent to
https://*.twilio.com.
"""

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .identity import get_identity
from .models import (
    DocumentSource,
    OutboundMessage,
    Patient,
    ProcessedMessage,
    WhatsAppMessage,
)
from .patient_lookup import find_patient_by_phone, mask_phone
from .pipeline import receive_document
from .security import audit, ratelimit
from .security.otp import create_transaction
from .storage import ALLOWED_TYPES, FileValidationError, normalize_content_type


WHATSAPP_WEBHOOK_PATH = "/api/v1/webhooks/whatsapp"

MAX_MEDIA_ITEMS = 5
MAX_WEBHOOK_BODY_BYTES = 64 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 20.0
MESSAGE_FRESHNESS = timedelta(minutes=10)

_MESSAGE_SID = re.compile(r"^(SM|MM)[0-9a-fA-F]{32}$")
_ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")


REPLIES = {
    "RECEIVED_VERIFY": (
        "Your document has been securely received. Verification required: "
        "open {link} and enter the code from the next message."
    ),
    "PORTAL_ACCESS": (
        "Verification required. Open {link} to manage what you share with "
        "JeevaFlow, and enter the code from the next message."
    ),
    "OTP": (
        "Your JeevaFlow verification code is {otp}. It expires in 5 minutes. "
        "Never share this code with anyone."
    ),
    "HANDWRITTEN_RECEIVED": (
        "Your handwritten document was received and will be reviewed by a doctor."
    ),
    "PROCESSED": (
        "Your document has been processed. Continue to the verified "
        "JeevaFlow portal."
    ),
    "ALREADY_PROCESSED": (
        "This document was already received. Continue to the verified "
        "JeevaFlow portal."
    ),
    "UNENROLLED": (
        "We received your message. If this number is enrolled with "
        "JeevaFlow, verification instructions will follow."
    ),
    "REJECTED": (
        "The file could not be accepted. Please send a clear photo "
        "(JPG or PNG) or a PDF."
    ),
    "RETRY_LATER": "Too many requests. Please try again later.",
    "FAILED": "We could not receive the document. Please try again.",
    "MALFORMED": "We could not process this message. Please try again.",
}


class MediaDownloadError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def portal_link(transaction_ref: str) -> str:
    return f"{settings.portal_base_url}/portal?txn={transaction_ref}"


# ============================================================
# TWIML
# ============================================================

def twiml_messages(messages: list[str]) -> str:
    body = "".join(f"<Message>{escape(message)}</Message>" for message in messages)

    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


# ============================================================
# SIGNATURE
# ============================================================

def compute_twilio_signature(url: str, params: list[tuple[str, str]], auth_token: str) -> str:
    """
    HMAC-SHA1 over the full URL followed by every POST parameter
    (sorted by name) as name+value, base64 encoded.
    """

    payload = url + "".join(f"{key}{value}" for key, value in sorted(params))

    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()

    return base64.b64encode(digest).decode("ascii")


def is_valid_twilio_signature(
    url: str,
    params: list[tuple[str, str]],
    signature: Optional[str],
    auth_token: str,
) -> bool:
    if not signature or not auth_token:
        return False

    return hmac.compare_digest(compute_twilio_signature(url, params, auth_token), signature)


# ============================================================
# TWILIO REST (download, delete, freshness, notify)
# ============================================================

def is_twilio_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()

    return parsed.scheme == "https" and (host == "twilio.com" or host.endswith(".twilio.com"))


def download_twilio_media(url: str) -> bytes:
    if not is_twilio_url(url):
        raise MediaDownloadError("UNTRUSTED_URL")

    auth = settings.twilio_rest_auth

    if auth is None:
        raise MediaDownloadError("TWILIO_NOT_CONFIGURED")

    limit = settings.max_upload_bytes

    try:
        # Redirects to Twilio's media CDN are followed; httpx drops the
        # Authorization header when the host changes.
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("GET", url, auth=auth) as response:
                if response.status_code != 200:
                    raise MediaDownloadError("HTTP_ERROR")

                declared = response.headers.get("content-length")

                if declared and declared.isdigit() and int(declared) > limit:
                    raise MediaDownloadError("FILE_TOO_LARGE")

                chunks, size = [], 0

                for chunk in response.iter_bytes():
                    size += len(chunk)

                    if size > limit:
                        raise MediaDownloadError("FILE_TOO_LARGE")

                    chunks.append(chunk)

                return b"".join(chunks)

    except MediaDownloadError:
        raise

    except httpx.HTTPError:
        raise MediaDownloadError("NETWORK_ERROR") from None


def delete_twilio_media(url: str) -> bool:
    """
    Remove the media from Twilio once it is in the vault, so Twilio
    is never the long-term store.
    """

    parsed = urlparse(url or "")

    if not is_twilio_url(url) or parsed.hostname != "api.twilio.com" or "/Media/" not in parsed.path:
        return False

    auth = settings.twilio_rest_auth

    if auth is None:
        return False

    target = url if parsed.path.endswith(".json") else url.split("?")[0] + ".json"

    try:
        response = httpx.delete(target, auth=auth, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return False

    return response.status_code in (200, 204, 404)


def check_message_freshness(form: dict) -> str:
    """
    Twilio does not sign a timestamp into webhooks, so freshness is
    confirmed against Twilio's record of the message.

    Returns VERIFIED | STALE | UNAVAILABLE.
    """

    auth = settings.twilio_rest_auth

    if auth is None or not settings.twilio_account_sid:
        return "UNAVAILABLE"

    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Messages/{form['MessageSid']}.json"
    )

    try:
        response = httpx.get(url, auth=auth, timeout=DOWNLOAD_TIMEOUT_SECONDS)

        if response.status_code != 200:
            return "UNAVAILABLE"

        created = parsedate_to_datetime(response.json()["date_created"])
    except Exception:
        return "UNAVAILABLE"

    if datetime.now(timezone.utc) - created > MESSAGE_FRESHNESS:
        return "STALE"

    return "VERIFIED"


def send_notification(db: Session, idb: Session, patient: Patient, kind: str, transaction_ref: Optional[str]):
    """
    Generic workflow notification to the patient's registered number.
    Without Twilio credentials the send is SIMULATED (recorded only).
    """

    body = REPLIES[kind]
    status = "SIMULATED"

    auth = settings.twilio_rest_auth

    if auth is not None and settings.twilio_whatsapp_number and settings.twilio_account_sid:
        identity = get_identity(idb, patient.ref)

        if identity is not None and identity.phone and not identity.synthetic:
            try:
                response = httpx.post(
                    "https://api.twilio.com/2010-04-01/Accounts/"
                    f"{settings.twilio_account_sid}/Messages.json",
                    data={
                        "From": settings.twilio_whatsapp_number,
                        "To": f"whatsapp:{identity.phone}",
                        "Body": body,
                    },
                    auth=auth,
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
                status = "SENT" if response.status_code in (200, 201) else "FAILED"
            except httpx.HTTPError:
                status = "FAILED"

    db.add(
        OutboundMessage(
            patient_id=patient.id, transaction_ref=transaction_ref, kind=kind, delivery_status=status
        )
    )
    audit.record(
        db, "NOTIFICATION_SENT", actor_type="SERVICE", actor_ref="notifier",
        object_type="PATIENT", object_ref=patient.ref, result=status, reason=kind,
    )


# ============================================================
# INBOUND MESSAGE HANDLING
# ============================================================

@dataclass
class InboundOutcome:
    status: str
    messages: list[str] = field(default_factory=list)
    entry: Optional[WhatsAppMessage] = None
    transaction_ref: Optional[str] = None
    replay: bool = False
    retry_after: Optional[int] = None


def _log(
    db: Session,
    form: dict,
    status: str,
    reply_kind: Optional[str],
    patient: Optional[Patient] = None,
    num_media: int = 0,
    detail: Optional[str] = None,
    document_ids: Optional[list[int]] = None,
    transaction_ref: Optional[str] = None,
) -> WhatsAppMessage:
    entry = WhatsAppMessage(
        message_sid=(form.get("MessageSid") or "")[:64] or None,
        sender_masked=mask_phone(form.get("From")),
        patient_id=patient.id if patient else None,
        transaction_ref=transaction_ref,
        num_media=num_media,
        status=status,
        detail=detail,
        document_ids=",".join(str(item) for item in document_ids) if document_ids else None,
        reply_kind=reply_kind,
    )

    db.add(entry)
    db.commit()
    db.refresh(entry)

    return entry


def handle_inbound_message(
    db: Session,
    idb: Session,
    form: dict,
    downloader: Optional[Callable[[str], bytes]] = None,
    deleter: Optional[Callable[[str], bool]] = None,
    freshness: Optional[Callable[[dict], str]] = None,
) -> InboundOutcome:
    """
    Process one signature-verified Twilio webhook payload.
    """

    downloader = downloader or download_twilio_media
    deleter = deleter or delete_twilio_media
    freshness = freshness or check_message_freshness

    sid = (form.get("MessageSid") or "").strip()

    if not _MESSAGE_SID.match(sid):
        return InboundOutcome("MALFORMED", [REPLIES["MALFORMED"]], _log(db, form, "MALFORMED", "MALFORMED"))

    # ---------------- replay protection ----------------
    db.add(ProcessedMessage(message_sid=sid))

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        audit.record_now(
            "WEBHOOK_REPLAY_BLOCKED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="MESSAGE", object_ref=sid, result="DENIED", reason="DUPLICATE_MESSAGE_SID",
        )
        return InboundOutcome("REPLAY", [], None, replay=True)

    # ---------------- account scope ----------------
    account = (form.get("AccountSid") or "").strip()

    if settings.twilio_account_sid and account != settings.twilio_account_sid:
        audit.record_now(
            "WEBHOOK_SIGNATURE_REJECTED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="MESSAGE", object_ref=sid, result="DENIED", reason="ACCOUNT_MISMATCH",
        )
        return InboundOutcome("MALFORMED", [REPLIES["MALFORMED"]], _log(db, form, "ACCOUNT_MISMATCH", "MALFORMED"))

    if account and not _ACCOUNT_SID.match(account):
        return InboundOutcome("MALFORMED", [REPLIES["MALFORMED"]], _log(db, form, "MALFORMED", "MALFORMED"))

    # ---------------- freshness ----------------
    fresh = freshness(form)

    if fresh == "STALE":
        audit.record_now(
            "WEBHOOK_REPLAY_BLOCKED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="MESSAGE", object_ref=sid, result="DENIED", reason="STALE_MESSAGE",
        )
        return InboundOutcome("STALE", [], _log(db, form, "STALE", None))

    try:
        num_media = int(form.get("NumMedia") or 0)
    except (TypeError, ValueError):
        num_media = -1

    sender = (form.get("From") or "").strip()

    if num_media < 0 or not sender:
        return InboundOutcome("MALFORMED", [REPLIES["MALFORMED"]], _log(db, form, "MALFORMED", "MALFORMED"))

    # ---------------- rate limits (after signature, replay, account
    # and freshness checks; a blocked message gets no reply, so a
    # flood is not amplified into outbound messages) ----------------
    try:
        ratelimit.hit("webhook_sender", sender)
    except ratelimit.RateLimited as exc:
        audit.record_now(
            "WEBHOOK_RATE_LIMITED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="MESSAGE", object_ref=sid, result="DENIED", reason="SENDER",
        )
        return InboundOutcome(
            "RATE_LIMITED", [], _log(db, form, "RATE_LIMITED", None, num_media=num_media),
            retry_after=exc.retry_after,
        )

    # ---------------- identity (no medical logic) ----------------
    lookup = find_patient_by_phone(db, idb, sender)

    if lookup.status != "FOUND":
        audit.record_now(
            "MESSAGE_UNENROLLED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="MESSAGE", object_ref=sid, result="DENIED",
        )
        # Media from unknown senders is never downloaded.
        return InboundOutcome(
            "UNENROLLED", [REPLIES["UNENROLLED"]],
            _log(db, form, "UNENROLLED", "UNENROLLED", num_media=num_media),
        )

    patient = lookup.patient

    # Every accepted message issues one OTP, so this also bounds OTP
    # issuance per patient.
    try:
        ratelimit.hit("webhook_patient", patient.ref)
    except ratelimit.RateLimited as exc:
        audit.record_now(
            "WEBHOOK_RATE_LIMITED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="MESSAGE", object_ref=sid, result="DENIED", reason="PATIENT",
        )
        return InboundOutcome(
            "RATE_LIMITED", [],
            _log(db, form, "RATE_LIMITED", None, patient=patient, num_media=num_media),
            retry_after=exc.retry_after,
        )

    detail_codes = [f"freshness:{fresh}"]

    # ---------------- text only: portal access ----------------
    if num_media == 0:
        transaction, otp = create_transaction(db, patient, "PORTAL_ACCESS")
        db.commit()

        return InboundOutcome(
            "PORTAL_ACCESS",
            [REPLIES["PORTAL_ACCESS"].format(link=portal_link(transaction.ref)), REPLIES["OTP"].format(otp=otp)],
            _log(
                db, form, "PORTAL_ACCESS", "PORTAL_ACCESS", patient=patient,
                detail=";".join(detail_codes), transaction_ref=transaction.ref,
            ),
            transaction_ref=transaction.ref,
        )

    # ---------------- media ----------------
    transaction, otp = create_transaction(db, patient, "UPLOAD")
    db.commit()

    accepted, already_processed, document_ids = 0, 0, []

    for index in range(min(num_media, MAX_MEDIA_ITEMS)):
        media_url = form.get(f"MediaUrl{index}") or ""
        declared = normalize_content_type(form.get(f"MediaContentType{index}"))

        if not media_url or declared not in ALLOWED_TYPES:
            detail_codes.append(f"media{index}:UNSUPPORTED")
            continue

        try:
            content = downloader(media_url)
        except MediaDownloadError as exc:
            detail_codes.append(f"media{index}:{exc.code}")
            continue
        except Exception:
            detail_codes.append(f"media{index}:DOWNLOAD_ERROR")
            continue

        audit.record_now(
            "MEDIA_DOWNLOADED", actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="TRANSACTION", object_ref=transaction.ref,
        )

        try:
            received = receive_document(
                db, patient, content, declared, DocumentSource.WHATSAPP.value,
                transaction=transaction, actor_ref="whatsapp-webhook",
            )
        except FileValidationError as exc:
            detail_codes.append(f"media{index}:{exc.code}")
            continue
        finally:
            del content

        # deleter returns True / False, or None when simulated (demo).
        deleted = deleter(media_url)
        audit.record_now(
            "MEDIA_DELETE_FAILED" if deleted is False else "MEDIA_DELETED_FROM_PROVIDER",
            actor_type="SERVICE", actor_ref="whatsapp-webhook",
            object_type="TRANSACTION", object_ref=transaction.ref,
            result={True: "SUCCESS", False: "FAILED", None: "SIMULATED"}[deleted],
        )

        document_ids.append(received.document.id)
        detail_codes.append(f"media{index}:{received.status}")

        if received.duplicate and received.document.processing_status != "QUARANTINED":
            already_processed += 1
        else:
            accepted += 1

    if accepted:
        status = "RECEIVED"
        messages = [
            REPLIES["RECEIVED_VERIFY"].format(link=portal_link(transaction.ref)),
            REPLIES["OTP"].format(otp=otp),
        ]
        reply_kind = "RECEIVED_VERIFY"
    elif already_processed:
        status, messages, reply_kind = "DUPLICATE", [REPLIES["ALREADY_PROCESSED"]], "ALREADY_PROCESSED"
    else:
        status, messages, reply_kind = "REJECTED", [REPLIES["REJECTED"]], "REJECTED"

    entry = _log(
        db, form, status, reply_kind, patient=patient, num_media=num_media,
        detail=";".join(detail_codes), document_ids=document_ids, transaction_ref=transaction.ref,
    )

    return InboundOutcome(status, messages, entry, transaction_ref=transaction.ref)


def serialize_message(entry: WhatsAppMessage) -> dict:
    return {
        "id": entry.id,
        "message_sid": entry.message_sid,
        "sender": entry.sender_masked,
        "num_media": entry.num_media,
        "status": entry.status,
        "detail": entry.detail,
        "reply_kind": entry.reply_kind,
        "transaction_ref": entry.transaction_ref,
        "created_at": entry.created_at,
    }


def integration_status() -> dict:
    """
    Credential-free view of the WhatsApp configuration.
    """

    return {
        "twilio_configured": settings.twilio_configured,
        "scoped_api_key": bool(settings.twilio_api_key_sid and settings.twilio_api_key_secret),
        "whatsapp_number": (
            mask_phone(settings.twilio_whatsapp_number) if settings.twilio_whatsapp_number else None
        ),
        "webhook_url": (
            settings.public_base_url + WHATSAPP_WEBHOOK_PATH if settings.public_base_url else None
        ),
        "webhook_https": settings.public_base_url.startswith("https://"),
        "signature_validation": "ENFORCED" if settings.twilio_signing_token else "NOT_CONFIGURED",
    }
