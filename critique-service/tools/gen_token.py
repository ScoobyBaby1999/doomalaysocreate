from __future__ import annotations
import os
import sys
from pathlib import Path

# Print the CURRENT auto-rotating bearer token for the gateway, derived from the root
# secret. The root secret never travels - this recomputes the wire token from the clock,
# so clients (CLI, frontend, the critique skill) call this each time instead of holding a
# standing token.
#
#   secret source (first found): $CRITIQUE_ROTATION_SECRET, else a file path passed as
#   argv[1], else ./.rotation_secret (gitignored). Never echo the secret itself.
#
#   usage:
#     CRITIQUE_ROTATION_SECRET=... python tools/gen_token.py
#     python tools/gen_token.py /path/to/secretfile
#     TOKEN=$(python tools/gen_token.py); curl -H "Authorization: Bearer $TOKEN" ...

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import authtoken  # noqa: E402


def _load_secret() -> str:
    s = os.environ.get("CRITIQUE_ROTATION_SECRET", "").strip()
    if s:
        return s
    candidates = []
    if len(sys.argv) > 1:
        candidates.append(Path(sys.argv[1]))
    candidates.append(Path(__file__).resolve().parent.parent / ".rotation_secret")
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def main() -> int:
    secret = _load_secret()
    if not secret:
        sys.stderr.write("no rotation secret: set CRITIQUE_ROTATION_SECRET, pass a file "
                         "path, or create ./.rotation_secret\n")
        return 2
    sys.stdout.write(authtoken.make_token(secret) + "\n")
    sys.stderr.write(f"(valid for ~{authtoken.seconds_until_rotation()}s more; "
                     f"window={authtoken.TOKEN_WINDOW_S}s, prev window also accepted)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
