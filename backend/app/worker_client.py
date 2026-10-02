"""
Runs the isolated processing worker (app/worker.py).

Each job gets a fresh process with a minimal environment (no
secrets), a private temporary directory that is deleted afterwards,
a timeout, and runs one at a time.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from .config import BACKEND_DIR


WORKER_TIMEOUT_SECONDS = 120

# One job at a time.
_slot = threading.BoundedSemaphore(1)


class WorkerError(Exception):
    def __init__(self, code: str, security_scan: dict = None):
        super().__init__(code)
        self.code = code
        self.security_scan = security_scan


def _environment(workdir: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": workdir,
        "TMPDIR": workdir,
        "TEMP": workdir,
        "TMP": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "OMP_THREAD_LIMIT": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "random",
    }

    if os.environ.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]

    return env


def run_worker(op: str, content: bytes, content_type: str, **params) -> dict:
    job = {
        "op": op,
        "content_type": content_type,
        "data": base64.b64encode(content).decode(),
        **params,
    }

    with _slot:
        workdir = tempfile.mkdtemp(prefix="jf-worker-")

        try:
            os.chmod(workdir, 0o700)

            completed = subprocess.run(
                [
                    sys.executable, "-E", "-s", "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import app.worker as w; w.main()",
                    str(BACKEND_DIR),
                ],
                input=json.dumps(job).encode(),
                capture_output=True,
                cwd=workdir,
                env=_environment(workdir),
                timeout=WORKER_TIMEOUT_SECONDS,
            )

        except subprocess.TimeoutExpired:
            raise WorkerError("WORKER_TIMEOUT") from None

        finally:
            # Delete every temporary file the job created (OCR
            # intermediates, renders). stderr is discarded unread.
            shutil.rmtree(workdir, ignore_errors=True)

    try:
        result = json.loads(completed.stdout or b"{}")
    except ValueError:
        raise WorkerError("WORKER_ERROR") from None

    if not result.get("ok"):
        raise WorkerError(result.get("code") or "WORKER_ERROR", result.get("security_scan"))

    return result


def decode(result: dict, key: str) -> bytes:
    return base64.b64decode(result[key])
