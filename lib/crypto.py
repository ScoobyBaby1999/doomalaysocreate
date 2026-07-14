"""Token encryption at rest for GitHub/HF OAuth tokens.

Uses Fernet (AES-128-CBC with HMAC-SHA256) from the ``cryptography`` package.
The encryption key is loaded from the ``ENCRYPTION_KEY`` environment variable
(a Fernet key, 32 url-safe base64 bytes).  If the env var is absent, a
per-process ephemeral key is generated (data is lost on restart — fine for
ephemeral Space containers, but NOT suitable for durable storage).
"""
from __future__ import annotations

import os
import secrets


_fernet = None


def _get_fernet():
    """Lazy-init Fernet cipher from ENCRYPTION_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet
    from cryptography.fernet import Fernet
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        key = Fernet.generate_key().decode()
        os.environ["ENCRYPTION_KEY"] = key
    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        key = Fernet.generate_key().decode()
        os.environ["ENCRYPTION_KEY"] = key
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string for storage. Returns a URL-safe base64 string."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored token. Returns the original plaintext string."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")


