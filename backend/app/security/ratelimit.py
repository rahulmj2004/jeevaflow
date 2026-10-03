"""
Sliding-window rate limits and quotas.

Two kinds of buckets:

* Dimension limits (IP, WhatsApp sender, staff user, patient) read
  JEEVAFLOW_RATE_LIMIT_PER_* and JEEVAFLOW_RATE_LIMIT_WINDOW_SECONDS
  through settings, with stricter defaults in production.
* Fixed brute-force quotas (login, MFA, OTP verification, evidence
  views, portal uploads) that are the same in every environment.

Each request is counted once per bucket it passes through; a
rejected request is not counted. Rate limiting never replaces
authentication: on the WhatsApp webhook the per-IP flood guard runs
only for invalid signatures, while sender and patient limits apply to
valid signed requests after replay, account and freshness checks.

DEMO IMPLEMENTATION: in-process memory (single server instance).
Counters are intentionally reset when the backend restarts.
PRODUCTION REQUIRED: a shared store (e.g. Redis) so limits hold
across instances and restarts.
"""

import hashlib
import threading
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from fastapi.responses import JSONResponse

from ..config import settings


RATE_LIMIT_MESSAGE = "Too many requests. Please try again later."


@dataclass
class Usage:
    limit: int
    remaining: int
    reset_after: int


class RateLimited(Exception):
    def __init__(self, bucket: str, retry_after: int, limit: int):
        super().__init__(bucket)
        self.bucket = bucket
        self.retry_after = retry_after
        self.limit = limit


# bucket -> dimension configured in settings.rate_limits
DIMENSION_BUCKETS = {
    "webhook_ip": "ip",
    "portal_ip": "ip",
    "webhook_sender": "sender",
    "doctor_api_user": "user",
    "webhook_patient": "patient",
}

# bucket -> (limit, window seconds); brute-force protections.
# Production values.
FIXED_LIMITS = {
    "otp_verify_ip": (20, 600),
    "login_ip": (20, 600),
    "login_user": (10, 600),
    "mfa_ip": (20, 600),
    "evidence_view_user": (60, 600),
    "upload_patient": (20, 3600),
    # Rejected uploads: lockout after 5, same in every environment.
    "upload_reject_patient": (5, 3600),
    "upload_reject_ip": (5, 3600),
}

# Demo/development: a local demo sends every browser tab and every
# presenter from 127.0.0.1, so per-IP buckets are shared by everyone.
# Still bounded (floods are blocked); the 5-failure account lockout
# is unchanged.
DEMO_FIXED_LIMITS = {
    "otp_verify_ip": (100, 600),
    "login_ip": (100, 600),
    "login_user": (30, 600),
    "mfa_ip": (100, 600),
    "evidence_view_user": (300, 600),
    "upload_patient": (100, 3600),
    "upload_reject_patient": (5, 3600),
    "upload_reject_ip": (5, 3600),
}

_lock = threading.Lock()
_events: dict[tuple[str, str], deque] = defaultdict(deque)
_distinct: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)

# Usage of every bucket checked during the current request, so the
# middleware can add X-RateLimit-* headers (demo/development only).
_request_usage: ContextVar[Optional[list]] = ContextVar("ratelimit_usage", default=None)


def limit_for(bucket: str) -> tuple[int, int]:
    if bucket in DIMENSION_BUCKETS:
        return settings.rate_limits[DIMENSION_BUCKETS[bucket]], settings.rate_limit_window

    if settings.is_production:
        return FIXED_LIMITS[bucket]

    return DEMO_FIXED_LIMITS[bucket]


def _key(value: str) -> str:
    # Keys (phone numbers, IPs) are hashed; nothing readable is kept.
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _track(usage: Usage):
    collected = _request_usage.get()

    if collected is not None:
        collected.append(usage)


def hit(bucket: str, key: str) -> Usage:
    limit, window = limit_for(bucket)
    now = time.monotonic()
    slot = (bucket, _key(key))

    with _lock:
        events = _events[slot]

        while events and events[0] <= now - window:
            events.popleft()

        if len(events) >= limit:
            retry_after = max(1, int(window - (now - events[0])) + 1)
            _track(Usage(limit, 0, retry_after))
            raise RateLimited(bucket, retry_after, limit)

        events.append(now)

        usage = Usage(limit, limit - len(events), max(1, int(window - (now - events[0])) + 1))

    _track(usage)

    return usage


def exhausted(bucket: str, key: str) -> bool:
    """
    True when the bucket is full. Does not count a request.
    """

    limit, window = limit_for(bucket)
    now = time.monotonic()

    with _lock:
        events = _events.get((bucket, _key(key)))

        return bool(events) and sum(1 for at in events if at > now - window) >= limit


def distinct_count(bucket: str, key: str, item: str, window: int) -> int:
    """
    Number of distinct items (e.g. patients) seen for a key within a
    window. Used for bulk-access anomaly detection.
    """

    now = time.monotonic()
    slot = (bucket, _key(key))

    with _lock:
        seen = _distinct[slot]
        seen[_key(item)] = now

        for name, at in list(seen.items()):
            if at <= now - window:
                del seen[name]

        return len(seen)


def reset() -> int:
    """
    Clear rate-limit counters only. Returns how many were cleared.
    """

    with _lock:
        cleared = len(_events) + len(_distinct)
        _events.clear()
        _distinct.clear()

    return cleared


# ------------------------------------------------------------
# HTTP helpers
# ------------------------------------------------------------

def start_request() -> tuple[list, object]:
    collected: list = []
    return collected, _request_usage.set(collected)


def end_request(token):
    _request_usage.reset(token)


def usage_headers(collected: list) -> dict:
    """
    X-RateLimit-* for the most constrained bucket of the request.
    Only sent outside production.
    """

    if settings.is_production or not collected:
        return {}

    tightest = min(collected, key=lambda usage: (usage.remaining, -usage.reset_after))

    return {
        "X-RateLimit-Limit": str(tightest.limit),
        "X-RateLimit-Remaining": str(tightest.remaining),
        "X-RateLimit-Reset": str(tightest.reset_after),
    }


def rate_limit_body(retry_after: int) -> dict:
    return {
        "error": "rate_limit_exceeded",
        "message": RATE_LIMIT_MESSAGE,
        "retry_after": retry_after,
    }


def rate_limit_response(retry_after: int, extra: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(
        {**rate_limit_body(retry_after), **(extra or {})},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )
