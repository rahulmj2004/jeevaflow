"""
Authentication, sessions and CSRF.

Staff:   username + password (scrypt)  ->  TOTP (RFC 6238)  ->  session
Patient: WhatsApp transaction + OTP     ->  short-lived portal session

Sessions are server-side. Cookies carry a random bearer token; the
database stores only an HMAC of it. Every state-changing request
must also send an X-CSRF-Token header derived from the session.
Authorization is deny-by-default: every route declares the session
kind and roles it accepts.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..config import settings
from ..identity import AuthSession, Role, StaffUser, get_identity_db
from . import audit
from .crypto import keyed_hash


STAFF_COOKIE = "jf_staff"
STAFF_MFA_COOKIE = "jf_staff_mfa"
PATIENT_COOKIE = "jf_patient"

STAFF_SESSION_TTL = timedelta(hours=2)
STAFF_IDLE_TIMEOUT = timedelta(minutes=20)
MFA_PENDING_TTL = timedelta(minutes=5)
PATIENT_SESSION_TTL = timedelta(minutes=15)

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)

GENERIC_AUTH_ERROR = "Authentication required."
GENERIC_FORBIDDEN = "Access not permitted."

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ============================================================
# PASSWORDS (scrypt)
# ============================================================

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)

    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **_SCRYPT,
        salt=base64.b64encode(salt).decode(),
        digest=base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            dklen=32,
            n=int(n),
            r=int(r),
            p=int(p),
        )
    except Exception:
        return False

    return hmac.compare_digest(candidate, base64.b64decode(digest))


# Used so unknown usernames cost the same time as known ones.
_DUMMY_HASH = None


def dummy_hash() -> str:
    global _DUMMY_HASH

    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))

    return _DUMMY_HASH


# ============================================================
# TOTP (RFC 6238)
# ============================================================

TOTP_STEP = 30
TOTP_DIGITS = 6


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10**TOTP_DIGITS
    return str(code).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str, at: Optional[float] = None) -> str:
    return _hotp(secret_b32, int((at or time.time()) // TOTP_STEP))


def verify_totp(secret_b32: str, code: str, last_step: Optional[int]) -> Optional[int]:
    """
    Returns the matched time step, or None. A step at or before
    last_step is refused, so a code cannot be replayed.
    """

    if not code or not code.isdigit() or len(code) != TOTP_DIGITS:
        return None

    current = int(time.time() // TOTP_STEP)

    for step in (current - 1, current, current + 1):
        if last_step is not None and step <= last_step:
            continue

        if hmac.compare_digest(_hotp(secret_b32, step), code):
            return step

    return None


def totp_uri(secret_b32: str, username: str) -> str:
    return f"otpauth://totp/JeevaFlow:{username}?secret={secret_b32}&issuer=JeevaFlow"


# ============================================================
# SESSIONS
# ============================================================

def _token_hash(token: str) -> str:
    return keyed_hash("session-token", token)


def csrf_for(token: str) -> str:
    return keyed_hash("csrf", token)[:43]


def create_session(
    idb: Session,
    kind: str,
    subject_ref: str,
    role: str,
    ttl: timedelta,
    transaction_ref: Optional[str] = None,
    mfa_verified: bool = False,
) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()

    idb.add(
        AuthSession(
            token_hash=_token_hash(token),
            kind=kind,
            subject_ref=subject_ref,
            role=role,
            transaction_ref=transaction_ref,
            mfa_verified=mfa_verified,
            created_at=now,
            last_seen_at=now,
            expires_at=now + ttl,
        )
    )
    idb.commit()

    return token


def set_session_cookie(response: Response, name: str, token: str, ttl: timedelta):
    response.set_cookie(
        name,
        token,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api",
    )


def clear_cookie(response: Response, name: str):
    response.delete_cookie(name, path="/api", secure=settings.cookie_secure, samesite="strict")


def revoke_sessions(idb: Session, subject_ref: str, kind: Optional[str] = None):
    query = idb.query(AuthSession).filter(
        AuthSession.subject_ref == subject_ref,
        AuthSession.revoked_at.is_(None),
    )

    if kind:
        query = query.filter(AuthSession.kind == kind)

    for row in query.all():
        row.revoked_at = datetime.utcnow()

    idb.commit()


def load_session(idb: Session, token: Optional[str], kind: str) -> Optional[AuthSession]:
    if not token or len(token) > 100:
        return None

    row = (
        idb.query(AuthSession)
        .filter(AuthSession.token_hash == _token_hash(token))
        .first()
    )

    now = datetime.utcnow()

    if row is None or row.kind != kind or row.revoked_at is not None:
        return None

    if row.expires_at <= now:
        return None

    if kind == "STAFF" and row.last_seen_at + STAFF_IDLE_TIMEOUT <= now:
        row.revoked_at = now
        idb.commit()
        audit.record_now(
            "SESSION_EXPIRED", actor_type="STAFF", actor_ref=row.subject_ref,
            result="DENIED", reason="IDLE_TIMEOUT",
        )
        return None

    row.last_seen_at = now
    idb.commit()

    return row


def _check_csrf(request: Request, token: str):
    if request.method in SAFE_METHODS:
        return

    sent = request.headers.get("X-CSRF-Token", "")

    if not sent or not hmac.compare_digest(sent, csrf_for(token)):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")


# ============================================================
# PRINCIPALS
# ============================================================

@dataclass
class StaffPrincipal:
    user_ref: str
    role: str
    display_name: str
    username: str
    session_hash: str


@dataclass
class PatientPrincipal:
    patient_ref: str
    transaction_ref: Optional[str]
    session_hash: str


def current_staff(
    request: Request,
    idb: Session = Depends(get_identity_db),
) -> StaffPrincipal:
    token = request.cookies.get(STAFF_COOKIE)
    row = load_session(idb, token, "STAFF")

    if row is None or not row.mfa_verified:
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

    user = idb.query(StaffUser).filter(StaffUser.ref == row.subject_ref).first()

    if user is None or not user.active:
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

    _check_csrf(request, token)

    return StaffPrincipal(
        user_ref=user.ref,
        role=user.role,
        display_name=user.display_name,
        username=user.username,
        session_hash=row.token_hash,
    )


def require_roles(*roles: str):
    allowed = set(roles)

    def dependency(principal: StaffPrincipal = Depends(current_staff)) -> StaffPrincipal:
        if principal.role not in allowed:
            audit.record_now(
                "DOCTOR_ACCESS_DENIED", actor_type="STAFF",
                actor_ref=principal.user_ref, result="DENIED", reason="ROLE",
            )
            raise HTTPException(status_code=403, detail=GENERIC_FORBIDDEN)

        return principal

    return dependency


require_doctor = require_roles(Role.DOCTOR)
require_auditor = require_roles(Role.AUDITOR, Role.ADMIN)
require_admin = require_roles(Role.ADMIN)
require_any_staff = require_roles(*Role.STAFF)


def current_patient(
    request: Request,
    idb: Session = Depends(get_identity_db),
) -> PatientPrincipal:
    token = request.cookies.get(PATIENT_COOKIE)
    row = load_session(idb, token, "PATIENT")

    if row is None:
        raise HTTPException(status_code=401, detail=GENERIC_AUTH_ERROR)

    _check_csrf(request, token)

    return PatientPrincipal(
        patient_ref=row.subject_ref,
        transaction_ref=row.transaction_ref,
        session_hash=row.token_hash,
    )
