"""
WhatsApp/Twilio as an untrusted channel. All Twilio network calls
are mocked; signatures use a synthetic signing token.
"""

import httpx
import pytest

import app.whatsapp as whatsapp
from app.config import settings
from app.database import SessionLocal
from app.models import Document, ProcessedMessage, Transaction, WhatsAppMessage

from tests.conftest import (
    INITIAL_REPORT_TEXT,
    extract_otp,
    make_image,
    make_pdf,
    message_sid,
    new_patient,
    signed_post,
)


PATIENT_PHONE = "+91 91234 56789"
PATIENT_FROM = "whatsapp:+919123456789"
MEDIA_URL = "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MMtest/Media/MEtest"

MEDICAL_WORDS = ("HbA1c", "9.4", "Glucose", "186", "Metformin", "diagnos", "result")


@pytest.fixture()
def enrolled(client):
    return new_patient(phone=PATIENT_PHONE)


@pytest.fixture()
def fake_twilio(monkeypatch):
    state = {"content": make_pdf(INITIAL_REPORT_TEXT), "downloads": [], "deleted": []}

    def download(url):
        state["downloads"].append(url)
        if isinstance(state["content"], Exception):
            raise state["content"]
        return state["content"]

    def delete(url):
        state["deleted"].append(url)
        return True

    monkeypatch.setattr(whatsapp, "download_twilio_media", download)
    monkeypatch.setattr(whatsapp, "delete_twilio_media", delete)
    monkeypatch.setattr(whatsapp, "check_message_freshness", lambda form: "VERIFIED")
    return state


def media_form(content_type="application/pdf", sid=None, sender=PATIENT_FROM):
    return {
        "AccountSid": "AC" + "0" * 32,
        "MessageSid": sid or message_sid(),
        "From": sender,
        "NumMedia": "1",
        "MediaUrl0": MEDIA_URL,
        "MediaContentType0": content_type,
    }


def replies(response) -> str:
    return response.text


def test_unsigned_and_wrongly_signed_requests_are_rejected(client, enrolled, fake_twilio):
    form = media_form()

    unsigned = client.post("/api/v1/webhooks/whatsapp", data=form)
    assert unsigned.status_code == 403

    wrong = signed_post(client, form, token="not-the-token")
    assert wrong.status_code == 403

    tampered = dict(form, From="whatsapp:+910000000000")
    signature = whatsapp.compute_twilio_signature(
        "http://testserver/api/v1/webhooks/whatsapp", sorted(form.items()), "synthetic-signing-token"
    )
    assert client.post(
        "/api/v1/webhooks/whatsapp", data=tampered, headers={"X-Twilio-Signature": signature}
    ).status_code == 403

    assert fake_twilio["downloads"] == []


def test_webhook_fails_closed_without_signing_token(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_twilio_signing_token", "")
    assert client.post("/api/v1/webhooks/whatsapp", data=media_form()).status_code == 503


def test_media_is_quarantined_until_otp_and_consent(client, enrolled, fake_twilio):
    response = signed_post(client, media_form())

    assert response.status_code == 200
    assert "securely received" in response.text
    assert "verification code is" in response.text
    assert "/portal?txn=txn_" in response.text

    db = SessionLocal()
    try:
        document = db.query(Document).one()
        assert document.processing_status == "QUARANTINED"
        assert document.source == "WHATSAPP"
        # Nothing has been read from it yet.
        assert db.query(Transaction).one().status == "PENDING_VERIFICATION"
        assert db.query(WhatsAppMessage).one().reply_kind == "RECEIVED_VERIFY"
    finally:
        db.close()

    assert fake_twilio["deleted"] == [MEDIA_URL]


def test_replies_contain_no_medical_information(client, enrolled, fake_twilio):
    response = signed_post(client, media_form())

    for word in MEDICAL_WORDS:
        assert word not in response.text

    for template in whatsapp.REPLIES.values():
        for word in ("HbA1c", "Metformin", "diagnos", "prescri", "lab "):
            assert word.lower() not in template.lower()


def test_replayed_message_sid_is_ignored(client, enrolled, fake_twilio):
    form = media_form()

    first = signed_post(client, form)
    replay = signed_post(client, form)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert "<Message>" not in replay.text
    assert len(fake_twilio["downloads"]) == 1

    db = SessionLocal()
    try:
        assert db.query(Transaction).count() == 1
        assert db.query(ProcessedMessage).count() == 1
    finally:
        db.close()


def test_malformed_message_sid_rejected(client, enrolled, fake_twilio):
    response = signed_post(client, media_form(sid="MMnot-a-real-sid"))

    assert "could not process" in response.text
    assert fake_twilio["downloads"] == []


def test_account_mismatch_rejected(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "AC" + "1" * 32)

    response = signed_post(client, media_form())

    assert "could not process" in response.text
    assert fake_twilio["downloads"] == []


def test_stale_message_is_dropped(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setattr(whatsapp, "check_message_freshness", lambda form: "STALE")

    response = signed_post(client, media_form())

    assert "<Message>" not in response.text
    assert fake_twilio["downloads"] == []


def test_unenrolled_number_gets_generic_reply_and_no_download(client, fake_twilio):
    response = signed_post(client, media_form(sender="whatsapp:+919999999999"))

    assert "If this number is enrolled" in response.text
    assert fake_twilio["downloads"] == []

    db = SessionLocal()
    try:
        assert db.query(Document).count() == 0
    finally:
        db.close()


def test_text_message_starts_portal_access(client, enrolled, fake_twilio):
    form = {"AccountSid": "AC" + "0" * 32, "MessageSid": message_sid("SM"), "From": PATIENT_FROM, "Body": "hi", "NumMedia": "0"}
    response = signed_post(client, form)

    assert "Verification required" in response.text
    assert len(extract_otp(response.text)) == 6

    db = SessionLocal()
    try:
        assert db.query(Transaction).one().purpose == "PORTAL_ACCESS"
    finally:
        db.close()


def test_unsupported_and_malicious_media(client, enrolled, fake_twilio):
    audio = signed_post(client, media_form("audio/ogg"))
    assert "could not be accepted" in audio.text
    assert fake_twilio["downloads"] == []

    fake_twilio["content"] = b"MZ\x90\x00 executable pretending to be a PDF"
    exe = signed_post(client, media_form())
    assert "could not be accepted" in exe.text

    fake_twilio["content"] = make_pdf("hello") + b"\n% /JavaScript (app.alert(1))"
    js = signed_post(client, media_form())
    assert "could not be accepted" in js.text

    db = SessionLocal()
    try:
        assert db.query(Document).count() == 0
    finally:
        db.close()


def test_download_failure_and_oversized_media(client, enrolled, fake_twilio):
    fake_twilio["content"] = whatsapp.MediaDownloadError("FILE_TOO_LARGE")
    response = signed_post(client, media_form())

    assert "could not be accepted" in response.text

    db = SessionLocal()
    try:
        assert "FILE_TOO_LARGE" in db.query(WhatsAppMessage).one().detail
    finally:
        db.close()


def test_image_media_is_accepted(client, enrolled, fake_twilio):
    fake_twilio["content"] = make_image(INITIAL_REPORT_TEXT, image_format="JPEG")

    response = signed_post(client, media_form("image/jpeg"))

    assert "securely received" in response.text


def test_download_sends_credentials_only_to_twilio(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "secret-token")
    monkeypatch.setattr(settings, "twilio_api_key_sid", "SKscoped")
    monkeypatch.setattr(settings, "twilio_api_key_secret", "scoped-secret")

    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"%PDF-1.4")

    real_client = httpx.Client
    monkeypatch.setattr(
        whatsapp.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    assert whatsapp.download_twilio_media(MEDIA_URL) == b"%PDF-1.4"

    import base64
    # The restricted API key is used, not the account auth token.
    assert base64.b64decode(seen["auth"].split()[1]).decode() == "SKscoped:scoped-secret"

    for url in ("https://evil.example.com/media", "http://api.twilio.com/insecure"):
        with pytest.raises(whatsapp.MediaDownloadError) as error:
            whatsapp.download_twilio_media(url)
        assert error.value.code == "UNTRUSTED_URL"


def test_download_enforces_size_and_status(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "secret-token")
    monkeypatch.setattr(settings, "max_upload_bytes", 10)

    responses = iter([httpx.Response(200, content=b"x" * 50), httpx.Response(404, content=b"missing")])
    real_client = httpx.Client
    monkeypatch.setattr(
        whatsapp.httpx, "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(lambda request: next(responses)), **kwargs),
    )

    with pytest.raises(whatsapp.MediaDownloadError) as error:
        whatsapp.download_twilio_media(MEDIA_URL)
    assert error.value.code == "FILE_TOO_LARGE"

    with pytest.raises(whatsapp.MediaDownloadError) as error:
        whatsapp.download_twilio_media(MEDIA_URL)
    assert error.value.code == "HTTP_ERROR"


def test_media_deletion_targets_only_twilio_media(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "secret-token")

    calls = []
    monkeypatch.setattr(whatsapp.httpx, "delete", lambda url, **kwargs: calls.append(url) or httpx.Response(204))

    assert whatsapp.delete_twilio_media(MEDIA_URL) is True
    assert calls == [MEDIA_URL + ".json"]
    assert whatsapp.delete_twilio_media("https://evil.example.com/Media/ME1") is False


def test_freshness_check(monkeypatch):
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "secret-token")

    def respond(minutes_ago):
        created = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        return lambda url, **kwargs: httpx.Response(200, json={"date_created": created})

    monkeypatch.setattr(whatsapp.httpx, "get", respond(1))
    assert whatsapp.check_message_freshness({"MessageSid": "MM" + "0" * 32}) == "VERIFIED"

    monkeypatch.setattr(whatsapp.httpx, "get", respond(60))
    assert whatsapp.check_message_freshness({"MessageSid": "MM" + "0" * 32}) == "STALE"


def test_twiml_is_escaped():
    assert "<script>" not in whatsapp.twiml_messages(["<script>alert(1)</script>"])
