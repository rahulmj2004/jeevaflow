"""
Private encrypted document vault.

Only AES-256-GCM ciphertext is written to disk, under an opaque
random object name, in a directory readable by the service user
only. The wrapped DEK is stored in the database (document_keys);
the KEK that unwraps it is never stored with either.

Deleting a document destroys its DEK first (crypto-shredding), then
removes the object, so any remaining copy (backup, disk block) is
unreadable.
"""

import os
import secrets
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .models import Document, DocumentKey
from .security.crypto import decrypt_document, encrypt_document


class VaultError(Exception):
    pass


def _vault_dir() -> Path:
    directory = settings.vault_dir
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _object_path(storage_key: str) -> Path:
    # Only ever resolve inside the vault.
    return _vault_dir() / Path(storage_key).name


def store(db: Session, document: Document, plaintext: bytes):
    blob, wrapped, key_id = encrypt_document(plaintext, document.ref)

    storage_key = secrets.token_hex(20) + ".jfv"
    path = _object_path(storage_key)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    with os.fdopen(fd, "wb") as handle:
        handle.write(blob)

    document.storage_key = storage_key

    db.add(DocumentKey(document_id=document.id, key_id=key_id, wrapped_dek=wrapped))
    db.flush()


def load(db: Session, document: Document) -> bytes:
    key = db.query(DocumentKey).filter(DocumentKey.document_id == document.id).first()

    if key is None or key.wrapped_dek is None or not document.storage_key:
        raise VaultError("Document key unavailable.")

    path = _object_path(document.storage_key)

    if not path.is_file():
        raise VaultError("Document object unavailable.")

    return decrypt_document(path.read_bytes(), key.wrapped_dek, document.ref)


def destroy(db: Session, document: Document):
    """
    Crypto-shred: destroy the DEK, then delete the ciphertext.
    """

    key = db.query(DocumentKey).filter(DocumentKey.document_id == document.id).first()

    if key is not None:
        key.wrapped_dek = None
        key.destroyed_at = datetime.utcnow()
        db.flush()

    if document.storage_key:
        path = _object_path(document.storage_key)

        if path.exists():
            # Best-effort overwrite before unlinking. On SSDs and
            # copy-on-write filesystems this is not guaranteed, which
            # is why the key is destroyed first.
            size = path.stat().st_size

            with open(path, "r+b") as handle:
                handle.write(secrets.token_bytes(size))

            path.unlink()

        document.storage_key = None
