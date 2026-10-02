"""
Rate limiting: configurable limits, 429 responses, demo-only reset,
and its place in the WhatsApp pipeline (after signature, replay,
account and freshness checks). All data is synthetic.
"""

import pytest

import app.whatsapp as whatsapp
from app.config import ConfigurationError, Settings, settings
from app.database import SessionLocal
from app.models import AuditEvent, ProcessedMessage, WhatsAppMessage
from app.security import ratelimit

from tests.conftest import (
    INITIAL_REPORT_TEXT,
    make_pdf,
    message_sid,
    new_patient,
    signed_post,
)


PATIENT_PHONE = "+91 91234 56789"
PATIENT_FROM = "whatsapp:+919123456789"
WEBHOOK = "/api/v1/webhooks/whatsapp"
RESET = "/api/v1/demo/rate-limit/reset"


@pytest.fixture()
def enrolled(client):
    return new_patient(phone=PATIENT_PHONE)


@pytest.fixture()
def fake_twilio(monkeypatch):
    content = make_pdf(INITIAL_REPORT_TEXT)
    monkeypatch.setattr(whatsapp, "download_twilio_media", lambda url: content)
    monkeypatch.setattr(whatsapp, "delete_twilio_media", lambda url: True)
    monkeypatch.setattr(whatsapp, "check_message_freshness", lambda form: "VERIFIED")


def text_form(sid=None, sender=PATIENT_FROM):
    return {
        "AccountSid": "AC" + "0" * 32,
        "MessageSid": sid or message_sid("SM"),
        "From": sender,
        "NumMedia": "0",
    }


def assert_rate_limited(response, window):
    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "rate_limit_exceeded"
    assert body["message"] == "Too many requests. Please try again later."
    assert isinstance(body["retry_after"], int)
    assert 1 <= body["retry_after"] <= window + 1
    assert response.headers["Retry-After"] == str(body["retry_after"])
    # Nothing about the patient or the secrets is echoed.
    assert "9123456789" not in response.text
    assert "synthetic-signing-token" not in response.text


# 1, 2, 9. Valid requests below the limit are accepted ---------------

def test_valid_signed_request_below_limit_is_accepted(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setitem(settings.rate_limits, "sender", 3)

    for _ in range(3):
        response = signed_post(client, text_form())
        assert response.status_code == 200
        assert "verification code" in response.text


# 3, 4. Exceeding the limit -> 429 with a correct Retry-After --------

def test_sender_limit_returns_429_with_retry_after(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setitem(settings.rate_limits, "sender", 2)
    monkeypatch.setattr(settings, "rate_limit_window", 60)

    assert signed_post(client, text_form()).status_code == 200
    assert signed_post(client, text_form()).status_code == 200

    blocked = signed_post(client, text_form())
    assert_rate_limited(blocked, 60)
    assert blocked.json()["retry_after"] >= 59
    assert "<Message>" not in blocked.text  # no outbound reply to a flood

    db = SessionLocal()
    try:
        assert db.query(WhatsAppMessage).order_by(WhatsAppMessage.id.desc()).first().status == "RATE_LIMITED"
        assert db.query(AuditEvent).filter(AuditEvent.action == "WEBHOOK_RATE_LIMITED").count() == 1
    finally:
        db.close()


def test_patient_limit_returns_429(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setitem(settings.rate_limits, "patient", 1)

    assert signed_post(client, text_form()).status_code == 200
    assert_rate_limited(signed_post(client, text_form()), settings.rate_limit_window)


def test_ip_flood_guard_limits_invalid_signatures_only(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setitem(settings.rate_limits, "ip", 1)

    assert signed_post(client, text_form(), token="forged-token").status_code == 403
    assert signed_post(client, text_form()).status_code == 200
    assert_rate_limited(
        signed_post(client, text_form(), token="forged-token"),
        settings.rate_limit_window,
    )


def test_api_rate_limit_uses_json_429(client, monkeypatch):
    monkeypatch.setitem(ratelimit.DEMO_FIXED_LIMITS, "login_ip", (1, 600))

    client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
    blocked = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})

    assert_rate_limited(blocked, 600)


# 7. Invalid signatures are rejected and never reach the sender or
#    patient limits ----------------------------------------------------

def test_invalid_signature_rejected_and_not_counted(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setitem(settings.rate_limits, "sender", 1)

    for _ in range(5):
        assert signed_post(client, text_form(), token="forged-token").status_code == 403
        assert client.post(WEBHOOK, data=text_form()).status_code == 403

    # The sender's single allowed message is still available.
    assert signed_post(client, text_form()).status_code == 200


# 8. Replays are rejected before rate limiting and do not consume it -

def test_replayed_message_sid_rejected_and_not_counted(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setitem(settings.rate_limits, "sender", 2)

    form = text_form()
    assert signed_post(client, form).status_code == 200

    for _ in range(5):
        replay = signed_post(client, form)
        assert replay.status_code == 200
        assert "<Message>" not in replay.text

    assert signed_post(client, text_form()).status_code == 200

    db = SessionLocal()
    try:
        assert db.query(ProcessedMessage).count() == 2
    finally:
        db.close()


# One request is counted once per bucket (no double counting) -------

def test_each_request_counted_once_per_bucket(client, enrolled, fake_twilio):
    assert signed_post(client, text_form()).status_code == 200

    counts = {bucket: len(events) for (bucket, _), events in ratelimit._events.items()}
    assert counts == {"webhook_sender": 1, "webhook_patient": 1}


# 5. Demo reset clears only the counters ------------------------------

def test_demo_reset_clears_counters_only(client, enrolled, fake_twilio, monkeypatch):
    monkeypatch.setitem(settings.rate_limits, "sender", 1)

    assert signed_post(client, text_form()).status_code == 200
    assert signed_post(client, text_form()).status_code == 429

    db = SessionLocal()
    try:
        before = (db.query(WhatsAppMessage).count(), db.query(ProcessedMessage).count())
        audit_before = db.query(AuditEvent).count()
    finally:
        db.close()

    response = client.post(RESET)
    assert response.status_code == 200
    assert response.json()["cleared"] >= 1

    db = SessionLocal()
    try:
        assert (db.query(WhatsAppMessage).count(), db.query(ProcessedMessage).count()) == before
        # Existing audit events are kept; the reset itself is audited.
        assert db.query(AuditEvent).count() == audit_before + 1
        assert db.query(AuditEvent).filter(AuditEvent.action == "DEMO_RATE_LIMIT_RESET").count() == 1
    finally:
        db.close()

    assert signed_post(client, text_form()).status_code == 200


def test_demo_reset_rejects_non_local_requests(client):
    assert client.post(RESET, headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 404


# 6. Production: no demo reset, no debug headers ----------------------

def test_production_disables_reset_and_debug_headers(client, enrolled, fake_twilio, monkeypatch):
    demo = signed_post(client, text_form())
    # Headers describe the most constrained bucket (the sender here).
    assert demo.headers["X-RateLimit-Limit"] == str(settings.rate_limits["sender"])
    assert demo.headers["X-RateLimit-Remaining"] == str(settings.rate_limits["sender"] - 1)
    assert "X-RateLimit-Remaining" in demo.headers
    assert "X-RateLimit-Reset" in demo.headers

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    monkeypatch.setattr(settings, "twilio_auth_token", "synthetic-signing-token")
    url = "https://example.test" + WEBHOOK

    assert client.post(RESET).status_code == 404

    response = signed_post(client, text_form(), url=url)
    assert response.status_code == 200
    assert "X-RateLimit-Limit" not in response.headers

    monkeypatch.setitem(settings.rate_limits, "sender", 0)
    blocked = signed_post(client, text_form(), url=url)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert "X-RateLimit-Limit" not in blocked.headers


# Configuration --------------------------------------------------------

def _settings(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_limits_come_from_environment(monkeypatch):
    configured = _settings(
        monkeypatch,
        JEEVAFLOW_ENV="demo",
        JEEVAFLOW_RATE_LIMIT_PER_IP="11",
        JEEVAFLOW_RATE_LIMIT_PER_SENDER="12",
        JEEVAFLOW_RATE_LIMIT_PER_USER="13",
        JEEVAFLOW_RATE_LIMIT_PER_PATIENT="14",
        JEEVAFLOW_RATE_LIMIT_WINDOW_SECONDS="15",
    )

    assert configured.demo_mode is True
    assert configured.rate_limits == {"ip": 11, "sender": 12, "user": 13, "patient": 14}
    assert configured.rate_limit_window == 15
    configured.validate()


def test_production_defaults_are_stricter(monkeypatch):
    for name in ("IP", "SENDER", "USER", "PATIENT"):
        monkeypatch.delenv(f"JEEVAFLOW_RATE_LIMIT_PER_{name}", raising=False)
    monkeypatch.delenv("JEEVAFLOW_RATE_LIMIT_WINDOW_SECONDS", raising=False)

    demo = _settings(monkeypatch, JEEVAFLOW_ENV="demo")
    production = _settings(monkeypatch, JEEVAFLOW_ENV="production")

    assert production.demo_mode is False

    for dimension in ("sender", "patient"):
        demo_rate = demo.rate_limits[dimension] / demo.rate_limit_window
        production_rate = production.rate_limits[dimension] / production.rate_limit_window
        assert production_rate < demo_rate


@pytest.mark.parametrize("value", ["0", "-5", "unlimited"])
def test_invalid_limits_fail_closed(monkeypatch, value):
    configured = _settings(monkeypatch, JEEVAFLOW_ENV="demo", JEEVAFLOW_RATE_LIMIT_PER_SENDER=value)

    assert configured.rate_limits["sender"] > 0  # never unlimited
    with pytest.raises(ConfigurationError):
        configured.validate()


def test_production_rejects_looser_overrides(monkeypatch):
    configured = _settings(
        monkeypatch,
        JEEVAFLOW_ENV="production",
        JEEVAFLOW_COOKIE_SECURE="true",
        PUBLIC_BASE_URL="https://example.test",
        TWILIO_AUTH_TOKEN="synthetic",
        JEEVAFLOW_RATE_LIMIT_PER_PATIENT="1000",
    )

    with pytest.raises(ConfigurationError, match="looser"):
        configured.validate()


def test_unknown_environment_rejected(monkeypatch):
    with pytest.raises(ConfigurationError):
        _settings(monkeypatch, JEEVAFLOW_ENV="staging").validate()


# 11. Restart behaviour -------------------------------------------------

def test_counters_are_in_memory_and_reset_on_restart():
    """
    Counters live only in process memory (documented in ratelimit.py
    and the README): a backend restart starts with empty counters.
    reset() is what a fresh process starts with.
    """

    ratelimit.reset()
    ratelimit.hit("webhook_sender", "whatsapp:+910000000000")
    assert ratelimit._events

    ratelimit.reset()
    assert not ratelimit._events


def test_fixed_limits_relaxed_only_outside_production(monkeypatch):
    for bucket, (limit, window) in ratelimit.FIXED_LIMITS.items():
        demo_limit, demo_window = ratelimit.limit_for(bucket)
        assert limit <= demo_limit and demo_window == window

    monkeypatch.setattr(settings, "environment", "production")
    for bucket, value in ratelimit.FIXED_LIMITS.items():
        assert ratelimit.limit_for(bucket) == value
