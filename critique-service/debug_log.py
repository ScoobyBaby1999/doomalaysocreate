"""Debug logging system — structured logs to stderr + in-memory buffer.

Every critical function logs its entry, exit, duration, and errors. Logs are
written to:
  1. stderr (HF Space logs capture this automatically)
  2. In-memory ring buffer (for /api/debug/logs endpoint)

View logs in the browser via GET /api/debug/logs?cat=glm&tail=50
(gated by the rotation token or CRITIQUE_TOKEN env var).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import deque
from typing import Any

# --- config -------------------------------------------------------------------
_MAX_MEMORY_LOGS = 5000
_STDERR_ENABLED = os.environ.get("DOOMALAYSOCREATE_LOG", "1").strip() not in ("0", "false", "no", "")

# --- in-memory ring buffer ----------------------------------------------------
_memory_logs: deque[dict] = deque(maxlen=_MAX_MEMORY_LOGS)
_categories: set[str] = set()

# --- sensitive field patterns for auto-redaction ----------------------------
_SENSITIVE_PATTERNS = [
    (r"\b(token|secret|key|password|auth)\s*[:=]\S+", "[REDACTED]"),
    (r"\b(Bearer\s+)[A-Za-z0-9-_.]+", r"\1[REDACTED]"),
    (r"\b(api[_-]?key)\s*[:=]\S+", r"\1=[REDACTED]"),
]


def _redact(msg: str) -> str:
    import re
    for pattern, replacement in _SENSITIVE_PATTERNS:
        msg = re.sub(pattern, replacement, msg, flags=re.IGNORECASE)
    return msg


def log_event(category: str, **fields: Any) -> None:
    """Fire-and-forget: never raises. Observability must not break a request."""
    try:
        record = {
            "ts": time.time(),
            "cat": category,
            "fields": {k: _redact(str(v)) for k, v in fields.items()},
        }
        _memory_logs.append(record)
        _categories.add(category)
        if _STDERR_ENABLED:
            print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)
    except Exception:
        pass  # never break the calling code


def get_recent_logs(category: str | None = None,
                    tail: int = 100,
                    min_level: str | None = None) -> list[dict]:
    """Return recent log entries, optionally filtered by category."""
    all_logs = list(_memory_logs)
    if category:
        all_logs = [r for r in all_logs if r.get("cat") == category]
    return all_logs[-tail:]


def list_log_categories() -> list[str]:
    return sorted(_categories)
