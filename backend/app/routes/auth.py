"""
Staff authentication: password, then TOTP MFA.

Errors are generic ("Invalid credentials.") whether the username
exists or not; unknown usernames still pay the password-hash cost.
Accounts lock for 15 minutes after 5 failed passwords.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..identity import AuthSession, Role, StaffUser, get_identity_db
from ..security import audit
from ..security.auth import (
    LOCKOUT,
    MAX_FAILED_LOGINS,
    MFA_PENDING_TTL,
    STAFF_COOKIE,
    STAFF_MFA_COOKIE,
    STAFF_SESSION_TTL,
    StaffPrincipal,
    clear_cookie,
    create_session,
    csrf_for,
    current_staff,
    dummy_hash,
    load_session,
    set_session_cookie,
    verify_password,
    verify_totp,
)
from .deps import client_ip, limit


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

INVALID = "Invalid credentials."


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=80)
    password: str = Field(..., min_length=1, max_length=200)


class MfaRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, idb: Session = Depends(get_identity_db)):
    limit("login_ip", client_ip(request))
    limit("login_user", body.username.lower())

    user = (
        idb.query(StaffUser)
        .filter(StaffUser.username == body.username.strip().lower(), StaffUser.active.is_(True))
        .first()
    )

    now = datetime.utcnow()

    if user is None:
        verify_password(body.password, dummy_hash())
        audit.record_now("LOGIN_FAILED", actor_type="STAFF", result="DENIED", reason="BAD_CREDENTIALS")
        raise HTTPException(status_code=401, detail=INVALID)

    if user.locked_until and user.locked_until > now:
        verify_password(body.password, dummy_hash())
        audit.record_now("LOGIN_LOCKED", actor_type="STAFF", actor_ref=user.ref, result="DENIED")
        raise HTTPException(status_code=401, detail=INVALID)

    if not verify_password(body.password, user.password_hash):
        user.failed_logins += 1

        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT
            user.failed_logins = 0

        idb.commit()
        audit.record_now("LOGIN_FAILED", actor_type="STAFF", actor_ref=user.ref, result="DENIED", reason="BAD_CREDENTIALS")
        raise HTTPException(status_code=401, detail=INVALID)

    user.failed_logins = 0
    idb.commit()

    token = create_session(idb, "STAFF_MFA_PENDING", user.ref, user.role, MFA_PENDING_TTL)
    set_session_cookie(response, STAFF_MFA_COOKIE, token, MFA_PENDING_TTL)

    audit.record_now("LOGIN_PASSWORD_OK", actor_type="STAFF", actor_ref=user.ref)

    return {"mfa_required": True, "method": "TOTP"}


@router.post("/mfa")
def mfa(body: MfaRequest, request: Request, response: Response, idb: Session = Depends(get_identity_db)):
    limit("mfa_ip", client_ip(request))

    token = request.cookies.get(STAFF_MFA_COOKIE)
    pending = load_session(idb, token, "STAFF_MFA_PENDING")

    if pending is None:
        raise HTTPException(status_code=401, detail=INVALID)

    user = idb.query(StaffUser).filter(StaffUser.ref == pending.subject_ref).first()

    step = verify_totp(user.totp_secret, body.code, user.totp_last_step) if user else None

    if step is None:
        audit.record_now("MFA_FAILED", actor_type="STAFF", actor_ref=pending.subject_ref, result="DENIED")
        raise HTTPException(status_code=401, detail=INVALID)

    user.totp_last_step = step
    pending.revoked_at = datetime.utcnow()
    idb.commit()

    session_token = create_session(
        idb, "STAFF", user.ref, user.role, STAFF_SESSION_TTL, mfa_verified=True
    )

    clear_cookie(response, STAFF_MFA_COOKIE)
    set_session_cookie(response, STAFF_COOKIE, session_token, STAFF_SESSION_TTL)

    audit.record_now("MFA_VERIFIED", actor_type="STAFF", actor_ref=user.ref)

    return {
        "user_ref": user.ref,
        "display_name": user.display_name,
        "role": user.role,
        "csrf_token": csrf_for(session_token),
    }


@router.get("/me")
def me(request: Request, principal: StaffPrincipal = Depends(current_staff)):
    return {
        "user_ref": principal.user_ref,
        "display_name": principal.display_name,
        "role": principal.role,
        "csrf_token": csrf_for(request.cookies.get(STAFF_COOKIE)),
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    principal: StaffPrincipal = Depends(current_staff),
    idb: Session = Depends(get_identity_db),
):
    row = idb.query(AuthSession).filter(AuthSession.token_hash == principal.session_hash).first()

    if row is not None:
        row.revoked_at = datetime.utcnow()
        idb.commit()

    clear_cookie(response, STAFF_COOKIE)
    audit.record_now("LOGOUT", actor_type="STAFF", actor_ref=principal.user_ref)

    return {"logged_out": True}


ROLE_LABELS = {
    Role.DOCTOR: "Doctor",
    Role.ADMIN: "Administrator",
    Role.AUDITOR: "Auditor",
}
