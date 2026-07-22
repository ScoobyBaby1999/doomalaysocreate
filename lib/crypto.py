"""Token/secret encryption at rest — AES-256-GCM + Fernet (legacy compat).

The encryption key is loaded from the ``ENCRYPTION_KEY`` environment variable.
Two formats are accepted:

  1. A **Fernet** key (32 url-safe base64 bytes) — legacy format. Detected
     when the value matches the Fernet regex (44 chars, base64). Used by the
     legacy ``encrypt_token`` / ``decrypt_token`` helpers, which we keep for
     backward-compat with the existing ``users.github_token_encrypted`` /
     ``hf_token_encrypted`` columns.
  2. A **raw 32-byte key** encoded as url-safe base64 — the AES-256-GCM
     master key used by the new ``encrypt_secret`` / ``decrypt_secret``
     helpers. This is the preferred format for new code (provider API keys,
     per-user secrets, anything written by /api/keys).

If ``ENCRYPTION_KEY`` is absent, a per-process ephemeral key is generated
(data is lost on restart — fine for ephemeral Space containers, but NOT
suitable for durable storage). On HF Spaces we STRONGLY recommend setting
``ENCRYPTION_KEY`` as a Space secret.

GCM format: ``v2:`` prefix + base64(nonce(12) || ciphertext || tag(16)).
Fernet format: opaque base64 token (starts with ``gAAAAA``).

Both helpers transparently detect the format on decrypt (Fernet fails on a
``v2:`` token, so we fall back to AES-GCM, and vice-versa). This makes the
planned migration from Fernet to AES-GCM a no-op for callers: existing rows
staying on Fernet keep decrypting, new writes use AES-GCM, and after a
migration pass everything is on AES-GCM.
"""
from __future__ import annotations

import base64
import os
import secrets


_fernet = None
_aes_key: bytes | None = None
_KEY_PREFIX = "v2:"


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

def _load_master_key() -> bytes:
    """Load the AES-256 master key from ENCRYPTION_KEY (or generate one).

    Accepts either a Fernet key (32 url-safe base64 bytes — derived to a
    32-byte raw key via SHA-256 of the decoded bytes) or a raw 32-byte key
    encoded as url-safe base64. If absent, generates an ephemeral per-process
    key (data is lost on restart — logged via os.environ so downstream
    services can detect it)."""
    global _aes_key
    if _aes_key is not None:
        return _aes_key
    raw = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not raw:
        # Generate an ephemeral key. This is acceptable for ephemeral HF
        # Spaces (where /data is also ephemeral), but the user should set
        # ENCRYPTION_KEY as a Space secret for durability.
        raw_bytes = secrets.token_bytes(32)
        os.environ["ENCRYPTION_KEY_EPHEMERAL"] = "1"
        _aes_key = raw_bytes
        return _aes_key
    # Try url-safe base64 decode (the canonical format we document for new
    # deployments). Fall back to raw bytes if it's not valid base64.
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) == 32:
            _aes_key = decoded
            return _aes_key
        # Fernet keys are 44-char url-safe base64 of 32 bytes — already 32
        # bytes after decode. Anything else: hash to 32 bytes.
        import hashlib
        _aes_key = hashlib.sha256(decoded).digest()
        return _aes_key
    except Exception:
        # Not valid base64 — treat the raw string as the key material.
        import hashlib
        _aes_key = hashlib.sha256(raw.encode("utf-8")).digest()
        return _aes_key


def _get_fernet():
    """Lazy-init Fernet cipher (legacy compat for the existing
    ``*_token_encrypted`` columns written by db.upsert_user)."""
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
        # The ENCRYPTION_KEY is a raw 32-byte key (AES-256 master), not a
        # valid Fernet key. Derive a Fernet key from it via HKDF so the
        # legacy encrypt_token/decrypt_token paths still work for existing
        # columns. New code should use encrypt_secret/decrypt_secret instead.
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        master = _load_master_key()
        # HKDF -> 32 bytes -> url-safe base64 (Fernet key format)
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"doomalaysocreate-fernet-v1",
        ).derive(master)
        fernet_key = base64.urlsafe_b64encode(derived)
        _fernet = Fernet(fernet_key)
    return _fernet


# ---------------------------------------------------------------------------
# AES-256-GCM (preferred for new code)
# ---------------------------------------------------------------------------

def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string with AES-256-GCM.

    Returns ``v2:`` + base64(nonce || ciphertext || tag). The nonce is 12
    bytes (random per call), the tag is 16 bytes (appended by the GCM
    implementation). Authenticated: any tampering makes decrypt fail.
    """
    if plaintext is None:
        return None  # type: ignore[return-value]
    if plaintext == "":
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _load_master_key()
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # AESGCM.encrypt returns ciphertext||tag (tag is the last 16 bytes).
    token = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    return f"{_KEY_PREFIX}{token}"


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a secret. Accepts either the new ``v2:`` AES-GCM format or
    the legacy Fernet format (transparent fallback for migrated rows)."""
    if ciphertext is None:
        return None  # type: ignore[return-value]
    if ciphertext == "":
        return ""
    if ciphertext.startswith(_KEY_PREFIX):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        token = ciphertext[len(_KEY_PREFIX):]
        try:
            blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except Exception as exc:
            raise ValueError("invalid ciphertext (not base64)") from exc
        if len(blob) < 12 + 16:
            raise ValueError("invalid ciphertext (too short)")
        nonce, ct = blob[:12], blob[12:]
        key = _load_master_key()
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    # Legacy Fernet token — try the Fernet path.
    try:
        return decrypt_token(ciphertext)
    except Exception as exc:
        raise ValueError(f"unrecognised ciphertext format: {type(exc).__name__}") from exc


def is_encrypted(value: str) -> bool:
    """True iff ``value`` looks like an encrypted blob we wrote."""
    if not value:
        return False
    if value.startswith(_KEY_PREFIX):
        return True
    # Fernet tokens always start with 'gAAAA' (version byte 0x80 + timestamp).
    return value.startswith("gAAAA")


# ---------------------------------------------------------------------------
# Legacy Fernet helpers (kept for backward compat with existing columns)
# ---------------------------------------------------------------------------

def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string for storage (legacy Fernet format).

    Kept for backward compatibility with the existing
    ``users.github_token_encrypted`` and ``users.hf_token_encrypted`` columns
    (and the JWT payload's ``github_token_enc`` / ``hf_token_enc`` claims).
    New code should use :func:`encrypt_secret` instead.
    """
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored token (legacy Fernet format). Returns the original
    plaintext string. Raises if the ciphertext is not a valid Fernet token."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# Migration helper — re-encrypt any legacy Fernet value to AES-GCM
# ---------------------------------------------------------------------------

def migrate_to_aes_gcm(ciphertext: str) -> str:
    """If ``ciphertext`` is a legacy Fernet token, re-encrypt it as AES-GCM.
    If it's already ``v2:`` AES-GCM, returns it unchanged. If empty/None,
    returns it unchanged. Useful for one-time migrations of DB columns."""
    if not ciphertext:
        return ciphertext
    if ciphertext.startswith(_KEY_PREFIX):
        return ciphertext
    try:
        plaintext = decrypt_token(ciphertext)
    except Exception:
        # Not a Fernet token — leave it alone (might be plaintext from a
        # very old row; the caller can decide what to do).
        return ciphertext
    return encrypt_secret(plaintext)
