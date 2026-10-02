"""
Create local development secrets in backend/.env.

    venv/bin/python -m app.security.init_secrets

Only missing values are added; existing values are never changed or
printed. The file is restricted to the current user (chmod 600).
For production, provide these values from a secret manager instead.
"""

import base64
import os
import secrets
import stat

from ..config import BACKEND_DIR, REQUIRED_KEYS


ENV_PATH = BACKEND_DIR / ".env"


def _key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _password() -> str:
    # Readable demo password for the synthetic staff accounts.
    words = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    return f"Demo-{words}"


GENERATED = {
    **{name: _key for name in REQUIRED_KEYS},
    "JEEVAFLOW_DEMO_STAFF_PASSWORD": _password,
    "JEEVAFLOW_DEMO_TOTP_SEED": _key,
    "JEEVAFLOW_DEMO_TWILIO_SIGNING_TOKEN": lambda: secrets.token_hex(16),
}


def existing_names(text: str) -> set[str]:
    names = set()

    for line in text.splitlines():
        line = line.strip()

        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)

            if value.strip():
                names.add(name.strip())

    return names


def main():
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    present = existing_names(text)

    added = []
    lines = []

    for name, factory in GENERATED.items():
        if name not in present and not os.getenv(name):
            lines.append(f"{name}={factory()}")
            added.append(name)

    if lines:
        block = (
            "\n# --- JeevaFlow local secrets (generated; never commit) ---\n"
            + "\n".join(lines)
            + "\n"
        )

        with open(ENV_PATH, "a") as handle:
            handle.write(("\n" if text and not text.endswith("\n") else "") + block)

    os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)

    if added:
        print("Added to backend/.env: " + ", ".join(added))
    else:
        print("All secrets already present in backend/.env.")


if __name__ == "__main__":
    main()
