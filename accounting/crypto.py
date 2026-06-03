"""Symmetric encryption for secrets at rest (e.g. ERP API keys).

We store integration credentials encrypted in the database — never plaintext,
never in chat. Encryption uses Fernet (AES-128-CBC + HMAC) with a key derived
from ``settings.SECRET_KEY`` (or an explicit ``EA_SECRET_ENCRYPTION_KEY`` if
set). Sessions already depend on a stable SECRET_KEY, so the derived key is
stable too; rotating SECRET_KEY invalidates stored secrets (they'd need
re-entry), which is acceptable for credentials.

API: ``encrypt_secret(plaintext) -> token`` and ``decrypt_secret(token) ->
plaintext`` (returns "" for empty input or an undecryptable token).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    explicit = getattr(settings, "EA_SECRET_ENCRYPTION_KEY", "")
    if explicit:
        # Expected to be a urlsafe-base64 32-byte Fernet key.
        return Fernet(explicit.encode() if isinstance(explicit, str) else explicit)
    # Derive a stable 32-byte key from SECRET_KEY.
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext):
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""
