"""
JeevaFlow API.

WhatsApp is only the communication channel. JeevaFlow is the
controlled healthcare-data environment.

    routes/webhook.py   Twilio webhook (untrusted channel)
    routes/portal.py    patient: OTP verification, consent, documents
    routes/auth.py      staff: password + TOTP MFA
    routes/doctor.py    doctor: consent-checked clinical access
    routes/security.py  security dashboard, audit chain
    routes/admin.py     enrolment and operations (no medical content)
    routes/demo.py      synthetic demo controls (demo mode only)
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.formparsers import MultiPartParser
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings
from .database import init_db
from .demo import write_demo_files
from .retention import run_retention_job
from .routes import admin, auth, demo, doctor, portal, security, webhook
from .seed import ensure_demo_patient, ensure_demo_staff
from .security import ratelimit
from .security.phi_log import install_phi_safe_logging, log_event
from .whatsapp import WHATSAPP_WEBHOOK_PATH


VERSION = "2.0.0"

# Keep uploaded files in memory: by default Starlette spools uploads
# above 1 MB to a plaintext temporary file on disk.
MultiPartParser.spool_max_size = settings.max_upload_bytes + 1

RETENTION_INTERVAL_SECONDS = 300

STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


async def _retention_loop():
    while True:
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)

        try:
            await asyncio.to_thread(run_retention_job)
        except Exception:
            log_event("INTERNAL_ERROR", reason="RETENTION_JOB")


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_phi_safe_logging()
    settings.validate()
    init_db()
    ensure_demo_patient()
    ensure_demo_staff()

    if settings.demo_mode:
        write_demo_files()

    run_retention_job()
    log_event("STARTUP", status="READY")

    task = asyncio.create_task(_retention_loop())

    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    title="JeevaFlow API",
    version=VERSION,
    description="Healthcare journey platform. AI reads, rules validate, humans decide.",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)


# ============================================================
# MIDDLEWARE
# ============================================================

class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Origin check for state-changing requests, a request-size cap and
    security headers on every response.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in STATE_CHANGING and request.url.path != WHATSAPP_WEBHOOK_PATH:
            origin = request.headers.get("origin")

            if origin and origin not in settings.cors_origins:
                return JSONResponse({"detail": "Origin not allowed."}, status_code=403)

        declared = request.headers.get("content-length")

        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + 1024 * 1024:
            return JSONResponse({"detail": "Request too large."}, status_code=413)

        collected, token = ratelimit.start_request()

        try:
            response = await call_next(request)
        finally:
            ratelimit.end_request(token)

        headers = response.headers

        for name, value in ratelimit.usage_headers(collected).items():
            headers[name] = value
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        if request.url.path.startswith("/api/"):
            headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"

            if "cache-control" not in headers:
                headers["Cache-Control"] = "no-store"

        if settings.is_production:
            headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

        return response


app.add_middleware(SecurityMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)


# ============================================================
# ERRORS (generic, no stack traces, no echoed input)
# ============================================================

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Pydantic's default error echoes the submitted input, which may
    # be an OTP, a password or medical text. Return locations only.
    return JSONResponse(
        {
            "detail": [
                {"loc": list(error.get("loc", [])), "msg": error.get("msg", "Invalid value.")}
                for error in exc.errors()
            ]
        },
        status_code=422,
    )


@app.exception_handler(ratelimit.RateLimited)
async def rate_limited(request: Request, exc: ratelimit.RateLimited):
    log_event("RATE_LIMITED", code=exc.bucket, path=request.url.path[:80])
    return ratelimit.rate_limit_response(exc.retry_after)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    log_event("INTERNAL_ERROR", code=type(exc).__name__, path=request.url.path[:80])

    return JSONResponse({"detail": "An internal error occurred."}, status_code=500)


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def root():
    return {"service": "JeevaFlow API", "version": VERSION, "status": "running"}


@app.get("/api/v1/health")
def health_check():
    return {"status": "healthy", "service": "JeevaFlow API", "version": VERSION}


for module in (webhook, portal, auth, doctor, security, admin, demo):
    app.include_router(module.router)
