"""
Download the AI Follow-Through Engine's local biomedical encoder
ONCE, at a pinned revision, and verify every file against its pinned
SHA-256 (about 440 MB).

    cd backend && venv/bin/python scripts/fetch_followthrough_model.py

This is the only network access the feature ever needs. At runtime
the model is loaded from disk inside the network-blocked worker, and
a missing or modified file disables the model (rule checks still run,
and the keyword rule matcher still runs).
"""

import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.followthrough.encoder import MODEL_DIR, MODEL_SOURCE, PINNED_FILES, sha256_file, verify  # noqa: E402


def main() -> int:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    for name, (remote, expected) in PINNED_FILES.items():
        target = MODEL_DIR / name

        if target.is_file() and sha256_file(target) == expected:
            print(f"ok       {name}")
            continue

        with tempfile.NamedTemporaryFile(dir=MODEL_DIR, delete=False) as handle:
            temporary = Path(handle.name)

        print(f"fetching {name}")
        urllib.request.urlretrieve(f"{MODEL_SOURCE}/{remote}", temporary)

        if sha256_file(temporary) != expected:
            temporary.unlink()
            print(f"CHECKSUM MISMATCH for {name}; nothing installed.")
            return 1

        temporary.replace(target)

    problem = verify()
    print("verified" if problem is None else problem)

    return 0 if problem is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
