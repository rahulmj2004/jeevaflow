"""
PHI-safe logging.

1. Application code logs through log_event() only: an allow-listed
   event name plus allow-listed fields whose values must look like
   opaque references or status codes. Anything else is replaced by
   [BLOCKED] before it reaches a handler.

2. A filter on every handler drops records from any other
   jeevaflow.* logger, strips exception messages and tracebacks
   (they can contain document text) and scrubs phone-number-like
   digit runs from third-party log lines (uvicorn, httpx).
"""

import logging
import re


SECURITY_LOGGER = "jeevaflow.security"

ALLOWED_EVENTS = None  # set lazily from audit.ACTIONS plus extras below

EXTRA_EVENTS = {
    "LOG_FIELD_BLOCKED",
    "WORKER_FAILED",
    "INTERNAL_ERROR",
    "RATE_LIMITED",
    "STARTUP",
    "AI_FOLLOWTHROUGH_FAILED",
}

ALLOWED_FIELDS = {
    "result", "actor", "object", "reason", "code", "count",
    "status", "engine", "duration_ms", "path",
}

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/\-]{0,80}$")

_PHONE_LIKE = re.compile(r"\+?\d[\d \-]{7,}\d")

logger = logging.getLogger(SECURITY_LOGGER)


def _allowed_events():
    global ALLOWED_EVENTS

    if ALLOWED_EVENTS is None:
        from .audit import ACTIONS

        ALLOWED_EVENTS = set(ACTIONS) | EXTRA_EVENTS

    return ALLOWED_EVENTS


def _clean(value):
    if value is None or isinstance(value, (int, float, bool)):
        return value

    value = str(value)

    if not _SAFE_VALUE.match(value) or _PHONE_LIKE.search(value):
        return "[BLOCKED]"

    return value


def log_event(event: str, **fields):
    if event not in _allowed_events():
        event = "LOG_FIELD_BLOCKED"
        fields = {}

    parts = [event]

    for key, value in fields.items():
        if value is None:
            continue

        if key not in ALLOWED_FIELDS:
            parts.append(f"{key}=[BLOCKED]")
            continue

        parts.append(f"{key}={_clean(value)}")

    logger.info(" ".join(parts))


class PHISafeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""

        if name.startswith("jeevaflow") and name != SECURITY_LOGGER:
            return False

        if record.exc_info:
            exc_type = record.exc_info[0]
            record.exc_info = None
            record.exc_text = None
            record.msg = f"{record.msg} [exception: {exc_type.__name__ if exc_type else 'unknown'}]"

        if name != SECURITY_LOGGER:
            try:
                message = record.getMessage()
            except Exception:
                return False

            record.msg = _PHONE_LIKE.sub("[REDACTED]", message)
            record.args = ()

        return True


_installed = set()


def install_phi_safe_logging():
    """
    Attach the filter to every configured handler. Safe to call
    repeatedly.
    """

    logging.basicConfig(level=logging.INFO)

    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        for handler in logging.getLogger(name).handlers:
            if id(handler) not in _installed:
                handler.addFilter(PHISafeFilter())
                _installed.add(id(handler))

    logger.setLevel(logging.INFO)
