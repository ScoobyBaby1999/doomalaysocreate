from __future__ import annotations
import json
import os
import sys
import time
from typing import Any

# structured operations log - one json line per notable event (call ok/fail,
# cooldowns, pacing waits...). the dashboard version of loom wrote these to a
# jsonl file on disk; the critique service is a stateless cloud sidecar with no
# writable volume, so we emit to stderr instead. hugging face captures stderr in
# the Space logs, which is exactly where you'd look anyway.
#
# fire-and-forget: log_event never raises. observability must not break a request.

# set LOOM_LOG=0 to silence the per-call telemetry entirely.
_ENABLED = os.environ.get("LOOM_LOG", "1").strip() not in ("0", "false", "no", "")


def log_event(kind: str, **fields: Any) -> None:
    if not _ENABLED:
        return
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
    }
    record.update(fields)
    try:
        sys.stderr.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
        sys.stderr.flush()
    except (OSError, TypeError, ValueError):
        pass


def _json_default(obj: Any) -> Any:
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "__fspath__"):
        return os.fspath(obj)
    return repr(obj)
