"""
Runtime configuration.

All secrets come from environment variables (optionally loaded from
backend/.env, which is git-ignored). Nothing in this module may be
logged or returned by the API in raw form.

Generate the local secrets once with:

    venv/bin/python -m app.security.init_secrets
"""

import base64
import os
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BACKEND_DIR / ".env")


class ConfigurationError(RuntimeError):
    pass


ENVIRONMENTS = ("development", "demo", "production")

# Default rate limits per environment: requests per key per window.
# demo/development: enough for a live demo, still blocks floods.
# production: never looser than these (enforced by validate()).
RATE_LIMIT_PROFILES = {
    "demo": {"ip": 120, "sender": 30, "user": 120, "patient": 30, "window": 60},
    "production": {"ip": 300, "sender": 20, "user": 600, "patient": 5, "window": 900},
}

RATE_LIMIT_DIMENSIONS = ("ip", "sender", "user", "patient")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int, errors: list) -> int:
    """
    A positive integer from the environment. Invalid, zero or negative
    values are reported (validate() fails closed) instead of meaning
    "unlimited".
    """

    raw = os.getenv(name)

    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw.strip())
    except ValueError:
        value = 0

    if value < 1:
        errors.append(f"{name} must be a positive integer.")
        return default

    return value


def _key(name: str) -> bytes:
    """
    A required 256-bit secret, base64 encoded in the environment.
    """

    raw = os.getenv(name, "").strip()

    if not raw:
        raise ConfigurationError(
            f"{name} is not set. Run "
            "`venv/bin/python -m app.security.init_secrets` "
            "to create local secrets in backend/.env."
        )

    try:
        value = base64.b64decode(raw, validate=True)
    except Exception:
        raise ConfigurationError(f"{name} is not valid base64.") from None

    if len(value) != 32:
        raise ConfigurationError(f"{name} must decode to 32 bytes.")

    return value


# Names of the secrets the application refuses to start without.
REQUIRED_KEYS = (
    "JEEVAFLOW_KEK",
    "JEEVAFLOW_IDENTITY_KEY",
    "JEEVAFLOW_OTP_SECRET",
    "JEEVAFLOW_AUDIT_KEY",
    "JEEVAFLOW_SESSION_SECRET",
)


class Settings:
    def __init__(self):
        # development | demo | production
        self.environment = os.getenv(
            "JEEVAFLOW_ENV", "development"
        ).strip().lower()

        # Demo mode: synthetic demo patient, demo staff accounts and
        # the demo control endpoints. Never available in production.
        self.demo_mode = (
            _bool("JEEVAFLOW_DEMO_MODE", True)
            and self.environment != "production"
        )

        self.database_url = os.getenv(
            "JEEVAFLOW_DATABASE_URL",
            f"sqlite:///{BACKEND_DIR / 'jeevaflow.db'}",
        )

        # Identity data (phone, name, date of birth, staff accounts)
        # lives in a separate database from medical data.
        self.identity_database_url = os.getenv(
            "JEEVAFLOW_IDENTITY_DATABASE_URL",
            f"sqlite:///{BACKEND_DIR / 'identity.db'}",
        )

        # Private, encrypted object store (ciphertext only).
        self.vault_dir = Path(
            os.getenv(
                "JEEVAFLOW_VAULT_DIR",
                str(BACKEND_DIR / "vault"),
            )
        )

        self.max_upload_bytes = int(
            os.getenv(
                "JEEVAFLOW_MAX_UPLOAD_BYTES",
                str(10 * 1024 * 1024),
            )
        )

        self.max_pdf_pages = int(os.getenv("JEEVAFLOW_MAX_PDF_PAGES", "30"))

        self.document_retention_days = int(
            os.getenv("JEEVAFLOW_DOCUMENT_RETENTION_DAYS", "90")
        )

        self.default_country_code = os.getenv(
            "JEEVAFLOW_DEFAULT_COUNTRY_CODE",
            "91",
        ).lstrip("+")

        self.cors_origins = [
            origin.strip()
            for origin in os.getenv(
                "JEEVAFLOW_CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if origin.strip()
        ]

        # Where patients complete verification and consent.
        self.portal_base_url = os.getenv(
            "JEEVAFLOW_PORTAL_BASE_URL",
            "http://localhost:5173",
        ).rstrip("/")

        # Cookies are Secure unless explicitly running plain-HTTP
        # local development.
        self.cookie_secure = _bool(
            "JEEVAFLOW_COOKIE_SECURE",
            self.environment == "production",
        )

        # Twilio. The auth token is used ONLY to verify webhook
        # signatures. REST calls prefer a restricted API key.
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.twilio_api_key_sid = os.getenv("TWILIO_API_KEY_SID", "")
        self.twilio_api_key_secret = os.getenv("TWILIO_API_KEY_SECRET", "")
        self.twilio_whatsapp_number = os.getenv(
            "TWILIO_WHATSAPP_NUMBER",
            "",
        )
        self.public_base_url = os.getenv(
            "PUBLIC_BASE_URL",
            "",
        ).rstrip("/")

        # Optional ClamAV binary (clamdscan / clamscan) for malware
        # scanning. Without it a built-in heuristic scanner runs.
        self.clamav_binary = os.getenv("JEEVAFLOW_CLAMAV_BINARY", "")

        # Demo-only values (synthetic accounts and patient).
        self.demo_patient_phone = os.getenv(
            "JEEVAFLOW_DEMO_PATIENT_PHONE",
            "+91 98765 43210",
        )
        self.demo_staff_password = os.getenv(
            "JEEVAFLOW_DEMO_STAFF_PASSWORD", ""
        )
        self.demo_totp_seed = os.getenv("JEEVAFLOW_DEMO_TOTP_SEED", "")
        self.demo_twilio_signing_token = os.getenv(
            "JEEVAFLOW_DEMO_TWILIO_SIGNING_TOKEN", ""
        )

        # Rate limits: requests per key per window, per dimension.
        self._rate_limit_errors = []
        profile = self.rate_limit_profile

        self.rate_limits = {
            dimension: _positive_int(
                f"JEEVAFLOW_RATE_LIMIT_PER_{dimension.upper()}",
                profile[dimension],
                self._rate_limit_errors,
            )
            for dimension in RATE_LIMIT_DIMENSIONS
        }
        self.rate_limit_window = _positive_int(
            "JEEVAFLOW_RATE_LIMIT_WINDOW_SECONDS",
            profile["window"],
            self._rate_limit_errors,
        )

        # Secrets (validated at startup by load_keys()).
        self._keys = None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def rate_limit_profile(self) -> dict:
        return RATE_LIMIT_PROFILES[
            "production" if self.is_production else "demo"
        ]

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def twilio_rest_auth(self):
        """
        Credentials for Twilio REST calls. A restricted API key is
        preferred over the account auth token.
        """

        if self.twilio_api_key_sid and self.twilio_api_key_secret:
            return (self.twilio_api_key_sid, self.twilio_api_key_secret)

        if self.twilio_configured:
            return (self.twilio_account_sid, self.twilio_auth_token)

        return None

    @property
    def twilio_signing_token(self) -> str:
        """
        Secret used to verify X-Twilio-Signature. In demo mode, a
        local demo token signs simulated messages when no real
        Twilio account is configured.
        """

        if self.twilio_auth_token:
            return self.twilio_auth_token

        if self.demo_mode:
            return self.demo_twilio_signing_token

        return ""

    def keys(self) -> dict:
        if self._keys is None:
            self._keys = {name: _key(name) for name in REQUIRED_KEYS}

        return self._keys

    def validate(self):
        """
        Fail closed on unsafe configuration.
        """

        self.keys()

        if self.environment not in ENVIRONMENTS:
            raise ConfigurationError(
                "JEEVAFLOW_ENV must be one of: " + ", ".join(ENVIRONMENTS) + "."
            )

        if self._rate_limit_errors:
            raise ConfigurationError(" ".join(self._rate_limit_errors))

        if self.is_production:
            profile = self.rate_limit_profile

            # Overrides may only make production stricter.
            if self.rate_limit_window < profile["window"] or any(
                self.rate_limits[dimension] > profile[dimension]
                for dimension in RATE_LIMIT_DIMENSIONS
            ):
                raise ConfigurationError(
                    "Production rate limits may not be looser than the "
                    "production defaults."
                )

            if not self.cookie_secure:
                raise ConfigurationError(
                    "Secure cookies are required in production."
                )

            if not self.public_base_url.startswith("https://"):
                raise ConfigurationError(
                    "PUBLIC_BASE_URL must be an https:// URL in production."
                )

            if not self.twilio_auth_token:
                raise ConfigurationError(
                    "TWILIO_AUTH_TOKEN is required in production."
                )


settings = Settings()
