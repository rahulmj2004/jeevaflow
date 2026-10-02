"""
Application-level encryption.

Documents (envelope encryption):

    plaintext --AES-256-GCM(random DEK, unique nonce)--> ciphertext
    DEK       --AES-256-GCM(KEK via KeyProvider)-------> wrapped DEK

The KEK never touches the database or the object store. The
KeyProvider interface is the seam for AWS KMS, Azure Key Vault,
Google Cloud KMS, HashiCorp Vault or an HSM: only wrap_key() and
unwrap_key() need a new implementation.

Database fields:

    medical columns  -> AES-256-GCM with a key derived from the KEK
    identity columns -> AES-256-GCM with a key derived from the
                        separate IDENTITY key (different database)
    phone lookup     -> HMAC-SHA256 blind index (no plaintext phone)
"""

import base64
import hashlib
import hmac
import secrets
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.types import Text, TypeDecorator

from ..config import settings


NONCE_BYTES = 12
BLOB_MAGIC = b"JFV1"
FIELD_PREFIX = "enc:v1:"


class DecryptionError(Exception):
    pass


def new_ref(prefix: str) -> str:
    """
    Opaque, non-sequential identifier (~128 bits of randomness).
    """

    return f"{prefix}_{secrets.token_urlsafe(16)}"


CASE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_case_alias() -> str:
    """
    Doctor-facing case ID (CASE-XXXX-XXXX). Purely random: never
    derived from name, phone or any other identity field.
    """

    chars = "".join(secrets.choice(CASE_ALPHABET) for _ in range(8))

    return f"CASE-{chars[:4]}-{chars[4:]}"


def derive_key(master: bytes, purpose: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"jeevaflow:{purpose}".encode(),
    ).derive(master)


# ============================================================
# KEY PROVIDER (KEK)
# ============================================================

class KeyProvider:
    """
    Wraps and unwraps data encryption keys.
    """

    key_id: str = "abstract"

    def wrap_key(self, dek: bytes, context: str) -> bytes:
        raise NotImplementedError

    def unwrap_key(self, wrapped: bytes, context: str) -> bytes:
        raise NotImplementedError


class LocalKeyProvider(KeyProvider):
    """
    DEMO IMPLEMENTATION: KEK from an environment variable.
    PRODUCTION REQUIRED: a KMS/HSM-backed provider, so the KEK
    cannot be read by the application host at all.
    """

    def __init__(self, kek: bytes, key_id: str = "local-env-kek-v1"):
        self._aead = AESGCM(kek)
        self.key_id = key_id

    def wrap_key(self, dek: bytes, context: str) -> bytes:
        nonce = secrets.token_bytes(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, dek, context.encode())

    def unwrap_key(self, wrapped: bytes, context: str) -> bytes:
        try:
            return self._aead.decrypt(
                wrapped[:NONCE_BYTES],
                wrapped[NONCE_BYTES:],
                context.encode(),
            )
        except Exception:
            raise DecryptionError("Key unwrap failed.") from None


_provider: Optional[KeyProvider] = None


def key_provider() -> KeyProvider:
    global _provider

    if _provider is None:
        _provider = LocalKeyProvider(settings.keys()["JEEVAFLOW_KEK"])

    return _provider


# ============================================================
# DOCUMENT ENVELOPE ENCRYPTION
# ============================================================

def encrypt_document(plaintext: bytes, document_ref: str) -> tuple[bytes, bytes, str]:
    """
    Returns (ciphertext blob, wrapped DEK, key id).

    The document reference is bound as associated data, so a
    ciphertext cannot be swapped onto another document.
    """

    dek = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(NONCE_BYTES)
    aad = f"document:{document_ref}".encode()

    blob = BLOB_MAGIC + nonce + AESGCM(dek).encrypt(nonce, plaintext, aad)

    provider = key_provider()
    wrapped = provider.wrap_key(dek, f"dek:{document_ref}")

    return blob, wrapped, provider.key_id


def decrypt_document(blob: bytes, wrapped_dek: bytes, document_ref: str) -> bytes:
    if not blob.startswith(BLOB_MAGIC):
        raise DecryptionError("Unknown ciphertext format.")

    dek = key_provider().unwrap_key(wrapped_dek, f"dek:{document_ref}")

    body = blob[len(BLOB_MAGIC):]

    try:
        return AESGCM(dek).decrypt(
            body[:NONCE_BYTES],
            body[NONCE_BYTES:],
            f"document:{document_ref}".encode(),
        )
    except Exception:
        raise DecryptionError("Document integrity check failed.") from None


# ============================================================
# FIELD ENCRYPTION
# ============================================================

class FieldCipher:
    def __init__(self, key: bytes, purpose: str):
        self._aead = AESGCM(key)
        self._aad = purpose.encode()

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, value.encode("utf-8"), self._aad)

        return FIELD_PREFIX + base64.b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        if not value.startswith(FIELD_PREFIX):
            raise DecryptionError("Field is not encrypted.")

        raw = base64.b64decode(value[len(FIELD_PREFIX):])

        try:
            return self._aead.decrypt(
                raw[:NONCE_BYTES], raw[NONCE_BYTES:], self._aad
            ).decode("utf-8")
        except Exception:
            raise DecryptionError("Field integrity check failed.") from None


_ciphers: dict[str, FieldCipher] = {}


def medical_cipher() -> FieldCipher:
    if "medical" not in _ciphers:
        _ciphers["medical"] = FieldCipher(
            derive_key(settings.keys()["JEEVAFLOW_KEK"], "medical-fields-v1"),
            "medical-field",
        )

    return _ciphers["medical"]


def identity_cipher() -> FieldCipher:
    if "identity" not in _ciphers:
        _ciphers["identity"] = FieldCipher(
            derive_key(settings.keys()["JEEVAFLOW_IDENTITY_KEY"], "identity-fields-v1"),
            "identity-field",
        )

    return _ciphers["identity"]


def identity_index(value: str) -> str:
    """
    Deterministic blind index for exact-match lookup (phone number)
    without storing the plaintext.
    """

    key = derive_key(settings.keys()["JEEVAFLOW_IDENTITY_KEY"], "identity-index-v1")

    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def keyed_hash(purpose: str, value: str) -> str:
    """
    HMAC-SHA256 for tokens (sessions, evidence tokens) so that a
    database copy does not contain usable bearer tokens.
    """

    key = derive_key(settings.keys()["JEEVAFLOW_SESSION_SECRET"], purpose)

    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


class EncryptedText(TypeDecorator):
    """
    Transparent AES-256-GCM column encryption.
    """

    impl = Text
    cache_ok = True

    def __init__(self, cipher: str = "medical", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cipher = cipher

    def _cipher(self) -> FieldCipher:
        return identity_cipher() if self.cipher == "identity" else medical_cipher()

    def process_bind_param(self, value, dialect):
        if value is None:
            return None

        return self._cipher().encrypt(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None

        return self._cipher().decrypt(value)


def reset_cached_keys():
    """
    Test helper: drop cached key material.
    """

    global _provider

    _provider = None
    _ciphers.clear()
