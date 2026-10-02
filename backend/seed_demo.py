"""
Initialise the synthetic demo without starting the server.

    venv/bin/python seed_demo.py

Creates the secured databases, the synthetic demo patient, the
synthetic demo staff accounts and the synthetic demo PDFs.
Requires the secrets in backend/.env
(venv/bin/python -m app.security.init_secrets).
"""

from app.config import settings
from app.database import init_db
from app.demo import write_demo_files
from app.seed import ensure_demo_patient, ensure_demo_staff


if __name__ == "__main__":
    settings.validate()
    init_db()

    print("Demo patient ready." if ensure_demo_patient() else "Demo mode is off.")
    print("Demo staff ready." if ensure_demo_staff() else "Demo staff not created (demo secrets missing).")

    for path in write_demo_files():
        print(f"Synthetic PDF: {path.name}")
