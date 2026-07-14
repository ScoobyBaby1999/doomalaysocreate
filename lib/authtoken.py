from __future__ import annotations
import base64
import hashlib
import hmac
import os
import time

# Auto-rotating bearer tokens (TOTP-style, stdlib only).
#
# The problem: a static CRITIQUE_TOKEN that gets pasted into chats/logs/screenshots is a
# standing liability - once seen, it's compromised forever. The fix: keep ONE long-lived
# root secret (CRITIQUE_ROTATION_SECRET) that NEVER travels on the wire, and derive the
# actual bearer token from HMAC(secret, current_time_window). The wire token rotates every
# window automatically; a leaked one self-expires once the window passes. No HF API writes,
# no restarts, no coordination - server and client each compute it from the shared secret
# and the clock.
#
# A one-window grace (the previous window stays valid) absorbs clock skew and in-flight
# requests so rotation never breaks a legitimate caller.
#
# Backwards compatible: a static CRITIQUE_TOKEN still works. Enabling rotation is opt-in
# (set CRITIQUE_ROTATION_SECRET); for maximum safety set ONLY the rotation secret so no
# standing token exists.

#   default window: 6 hours. Was 1 hour, but users who left the tab open
#   overnight hit "missing or invalid bearer token" because the 1h window +
#   1 grace (2h tolerance) had rolled past. 6h + grace=2 = 18h tolerance,
#   which covers a full sleep cycle. The client (token.ts) MUST use the same
#   WINDOW_S or token derivation diverges — keep them in sync.
TOKEN_WINDOW_S = int(os.environ.get("TOKEN_WINDOW_S", "21600"))
#   how many past windows still validate (grace). 2 => current + 2 previous.
TOKEN_GRACE_WINDOWS = int(os.environ.get("TOKEN_GRACE_WINDOWS", "2"))


def _window_index(now: float, window_s: int) -> int:
    return int(now // max(1, window_s))


def make_token(secret: str, *, now: float | None = None, window_s: int = TOKEN_WINDOW_S) -> str:
    """The current wire token derived from the root secret + the active time window."""
    now = time.time() if now is None else now
    return _token_for_window(secret, _window_index(now, window_s))


def _token_for_window(secret: str, window: int) -> str:
    mac = hmac.new(secret.encode("utf-8"), str(window).encode("ascii"), hashlib.sha256).digest()
    #   18 bytes -> 24 url-safe chars; window is NOT encoded in the token (server tries a
    #   small set of recent windows), so the token reveals nothing about timing.
    return base64.urlsafe_b64encode(mac[:18]).decode("ascii")


def valid_tokens(secret: str, *, now: float | None = None, window_s: int = TOKEN_WINDOW_S,
                 grace: int = TOKEN_GRACE_WINDOWS) -> list[str]:
    """All wire tokens currently acceptable: the active window plus `grace` past windows."""
    if not secret:
        return []
    now = time.time() if now is None else now
    cur = _window_index(now, window_s)
    return [_token_for_window(secret, cur - i) for i in range(max(0, grace) + 1)]


def token_matches(presented: str, secret: str, *, now: float | None = None,
                  window_s: int = TOKEN_WINDOW_S, grace: int = TOKEN_GRACE_WINDOWS) -> bool:
    """Constant-time check of a presented token against every acceptable window token."""
    ok = False
    for candidate in valid_tokens(secret, now=now, window_s=window_s, grace=grace):
        #   compare ALL candidates (no early exit) to keep timing independent of which
        #   window matched.
        if hmac.compare_digest(presented, candidate):
            ok = True
    return ok


def seconds_until_rotation(*, now: float | None = None, window_s: int = TOKEN_WINDOW_S) -> int:
    now = time.time() if now is None else now
    return int(window_s - (now % max(1, window_s)))
