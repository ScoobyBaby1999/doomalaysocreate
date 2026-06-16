"""Self-validating JWT for stateless loom auth.

Uses HMAC-SHA256 + base64url (no external deps).  The token contains
encrypted provider credentials so the backend can survive a DB wipe
without requiring re-authentication.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# shared secret: prefer dedicated JWT_SECRET, fall back to rotation secret.
# NO hardcoded fallback — if neither is set the module refuses to load.
_raw = os.environ.get("JWT_SECRET") or os.environ.get("CRITIQUE_ROTATION_SECRET")
if not _raw:
    raise RuntimeError(
        "JWT_SECRET or CRITIQUE_ROTATION_SECRET must be set"
    )
JWT_SECRET = _raw.encode()

# default audience: the SPACE_ID env var (set per-Space by HF).  When absent
# the caller must supply an explicit audience to generate_jwt / verify_jwt.
_DEFAULT_AUD = os.environ.get("SPACE_ID", "")

# default token lifetime -- 7 days.  Short enough that a leaked JWT
# expires quickly; long enough that users aren't annoyed.
DEFAULT_EXP_HOURS = int(os.environ.get("JWT_EXP_HOURS", "168"))

# refresh window: you may refresh a token during the last REFRESH_HOURS
# of its life.  Currently unused (re-auth on expiry is acceptable).
REFRESH_WINDOW_HOURS = int(os.environ.get("JWT_REFRESH_HOURS", "24"))


# ---------------------------------------------------------------------------
# base64url helpers
# ---------------------------------------------------------------------------
def _b64url_encode(data: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """base64url decode, tolerant of missing padding."""
    pad = 4 - len(text) % 4
    if pad != 4:
        text += "=" * pad
    return base64.urlsafe_b64decode(text)


# ---------------------------------------------------------------------------
# JWT primitives
# ---------------------------------------------------------------------------
def _sign(header_b64: str, payload_b64: str) -> str:
    to_sign = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(JWT_SECRET, to_sign, hashlib.sha256).digest()
    return _b64url_encode(sig)


def _verify_sig(header_b64: str, payload_b64: str, sig_b64: str) -> bool:
    expected = _sign(header_b64, payload_b64)
    return hmac.compare_digest(expected, sig_b64)


# ---------------------------------------------------------------------------
# ID derivation (deterministic, stable across DB rebuilds)
# ---------------------------------------------------------------------------
def derive_user_id(github_id: int) -> str:
    """Return a stable 16-char hex user_id from a github_id + secret."""
    return hashlib.sha256(f"{github_id}:{JWT_SECRET}:loom".encode()).hexdigest()[:16]


def derive_user_id_from_hf(hf_name: str) -> str:
    """Return a stable 16-char hex user_id from a HF username + secret.
    Used for HF-only users who haven't linked GitHub."""
    return hashlib.sha256(f"hf:{hf_name}:{JWT_SECRET}:loom".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def generate_jwt(
    *,
    user_id: str,
    github_id: int = 0,
    github_username: str = "",
    github_token_encrypted: str = "",
    hf_id: str = "",
    hf_token_encrypted: str = "",
    exp_hours: int = DEFAULT_EXP_HOURS,
    audience: str = "",
) -> str:
    """Mint a new JWT with embedded encrypted credentials.

    ``user_id`` is the deterministic internal ID (from ``derive_user_id`` or
    ``derive_user_id_from_hf``).  It becomes the JWT ``sub`` claim so auth
    lookups are stable across DB rebuilds.
    ``audience`` identifies the Space that should accept this token.
    When empty, uses SPACE_ID env var.  When that is also empty the
    payload carries no aud claim (callers must accept tokens from any
    source — not recommended).
    """
    now = int(time.time())
    payload = {
        "sub": user_id,
        "github_id": github_id,
        "github_username": github_username,
        "github_token_enc": github_token_encrypted,
        "hf_id": hf_id,
        "hf_token_enc": hf_token_encrypted,
        "iat": now,
        "exp": now + (exp_hours * 3600),
    }
    aud = audience or _DEFAULT_AUD
    if aud:
        payload["aud"] = aud
    header_b64 = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig_b64 = _sign(header_b64, payload_b64)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def verify_jwt(token: str, expected_aud: str | None = None) -> dict | None:
    """Verify a JWT.  Returns payload dict or None if invalid/expired/misaddressed.

    When ``expected_aud`` is None the SPACE_ID env var is used as the
    expected audience.  When both are empty the aud claim is not checked
    (not recommended).
    """
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    if not _verify_sig(header_b64, payload_b64, sig_b64):
        return None
    try:
        payload: dict = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    exp = payload.get("exp")
    if not exp or exp < time.time():
        return None
    aud = payload.get("aud")
    if aud:
        want = expected_aud or _DEFAULT_AUD
        if want and aud != want:
            return None
    return payload


# ---------------------------------------------------------------------------
# Refresh helper
# ---------------------------------------------------------------------------
def refresh_jwt(token: str, exp_hours: int = DEFAULT_EXP_HOURS) -> str | None:
    """Re-sign an existing JWT with a new expiry.  Returns new JWT or None."""
    payload = verify_jwt(token)
    if not payload:
        return None
    # strip old exp/iat so they don't collide
    for k in ("exp", "iat"):
        payload.pop(k, None)
    return generate_jwt(
        user_id=payload["sub"],
        github_id=payload.get("github_id", 0),
        github_username=payload.get("github_username", ""),
        github_token_encrypted=payload.get("github_token_enc", ""),
        hf_id=payload.get("hf_id", ""),
        hf_token_encrypted=payload.get("hf_token_enc", ""),
        exp_hours=exp_hours,
        audience=payload.get("aud", ""),
    )
