"""
DEMO IMPLEMENTATION: print the current TOTP codes of the synthetic
demo staff accounts, for presenting the MFA step without a phone.

    venv/bin/python -m app.security.demo_totp

Add `--uri` to print otpauth:// URIs for an authenticator app.
Never use this for real accounts.
"""

import sys
import time

from ..config import settings
from ..seed import DEMO_STAFF, demo_totp_secret
from .auth import TOTP_STEP, totp_now, totp_uri


def main():
    if not settings.demo_mode or not settings.demo_totp_seed:
        print("Demo mode is off or JEEVAFLOW_DEMO_TOTP_SEED is not set.")
        return

    remaining = TOTP_STEP - int(time.time()) % TOTP_STEP

    for username, display_name, role in DEMO_STAFF:
        secret = demo_totp_secret(username)

        if "--uri" in sys.argv:
            print(f"{username:<12} {totp_uri(secret, username)}")
        else:
            print(f"{username:<12} {role:<8} {totp_now(secret)}")

    if "--uri" not in sys.argv:
        print(f"(codes change in {remaining}s; each code works once)")


if __name__ == "__main__":
    main()
