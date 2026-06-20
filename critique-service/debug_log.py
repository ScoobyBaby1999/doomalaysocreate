"""Debug logging system — structured logs to disk + stderr for instant debugging.

Every critical function logs its entry, exit, duration, and errors. Logs are
written to:
  1. debug/*.log files (one per category — glm, auth, agent, conscious, errors)
  2. debug/current.jsonl (rolling combined log for grepping)
  3. stderr (HF Space logs capture this)

View logs in the browser via GET /api/debug/logs?cat=glm&tail=50
(gated by the rotation token or DEBUG_TOKEN env var).

Fire-and-forget: dlog never raises. Observability must not break a request.
"""
from __future__ import annotations
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

# --- config -----------------------------------------------------------------
def _resolve_debug_dir() -> Path:
    """Find a writable directory for debug logs. Tries in order:
    1. LOOM_DEBUG_DIR env var (explicit override)
    2. <app>/debug (next to critique-service/)
    3. /tmp/loom-debug (always writable on HF Spaces)
    4. ~/.loom-debug (home directory)
    Returns the first writable one, or a dummy path if none work."""
    candidates = []
    env_dir = os.environ.get("LOOM_DEBUG_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(__file__).resolve().parent.parent / "debug")
    candidates.append(Path("/tmp/loom-debug"))
    candidates.append(Path.home() / ".loom-debug")
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            test_file = c / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            return c
        except (OSError, PermissionError):
            continue
    return Path("/tmp/loom-debug-fallback")

_DEBUG_DIR = _resolve_debug_dir()
_DISK_ENABLED = _DEBUG_DIR.exists() and os.environ.get("LOOM_DEBUG", "1").strip() not in ("0", "false", "no", "")

_COMBINED_LOG = _DEBUG_DIR / "current.jsonl"

_CATEGORY_FILES = {
    "startup": _DEBUG_DIR / "startup.log",
    "glm": _DEBUG_DIR / "glm.log",
    "auth": _DEBUG_DIR / "auth.log",
    "agent": _DEBUG_DIR / "agent.log",
    "conscious": _DEBUG_DIR / "conscious.log",
    "errors": _DEBUG_DIR / "errors.log",
    "http": _DEBUG_DIR / "http.log",
}

_STDERR_ENABLED = os.environ.get("LOOM_STDERR", "1").strip() not in ("0", "false", "no", "")
_MAX_FILE_BYTES = 1 * 1024 * 1024


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_FILE_BYTES:
            old = path.with_suffix(path.suffix + ".old")
            if old.exists():
                old.unlink()
            path.rename(old)
    except Exception:
        pass


def _write_log(category: str, level: str, fn: str, msg: str,
               data: dict | None, ms: float | None) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "cat": category,
        "fn": fn,
        "msg": msg,
    }
    if data:
        safe_data = {}
        for k, v in data.items():
            kl = k.lower()
            if k.endswith("_preview") or k.endswith("_len") or k == "providers_with_keys":
                safe_data[k] = v
            elif any(s in kl for s in ("token", "secret", "key", "password", "auth")):
                safe_data[k] = f"<redacted:{len(str(v))}chars>" if v else "<empty>"
            else:
                safe_data[k] = v
        record["data"] = safe_data
    if ms is not None:
        record["ms"] = round(ms, 1)

    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"

    if _STDERR_ENABLED:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass

    if _DISK_ENABLED:
        cat_file = _CATEGORY_FILES.get(category)
        if cat_file:
            _rotate_if_needed(cat_file)
            try:
                with open(cat_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                pass
        _rotate_if_needed(_COMBINED_LOG)
        try:
            with open(_COMBINED_LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def dlog(category: str, fn: str, msg: str, *,
         level: str = "INFO", data: dict | None = None, ms: float | None = None) -> None:
    try:
        _write_log(category, level, fn, msg, data, ms)
    except Exception:
        pass


def derror(category: str, fn: str, msg: str, exc: Exception | None = None,
           data: dict | None = None) -> None:
    try:
        err_data = dict(data or {})
        if exc:
            err_data["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            err_data["traceback"] = traceback.format_exc().split("\n")[-5:]
        _write_log(category, "ERROR", fn, msg, err_data, None)
        _write_log("errors", "ERROR", fn, msg, err_data, None)
    except Exception:
        pass


def dtimed(category: str, fn: str, msg: str, data: dict | None = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            dlog(category, fn, f"ENTRY: {msg}", data=data, level="DEBUG")
            try:
                result = func(*args, **kwargs)
                ms = (time.monotonic() - t0) * 1000
                dlog(category, fn, f"EXIT OK: {msg}", data=data, ms=ms)
                return result
            except Exception as exc:
                ms = (time.monotonic() - t0) * 1000
                derror(category, fn, f"EXIT ERROR: {msg}", exc=exc, data=data)
                dlog(category, fn, f"failed in {ms:.1f}ms", data=data, ms=ms, level="ERROR")
                raise
        wrapper.__name__ = getattr(func, "__name__", fn)
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator


def log_startup_env() -> None:
    env_checks = [
        "CRITIQUE_TOKEN", "CRITIQUE_ROTATION_SECRET", "JWT_SECRET",
        "DEBUG_TOKEN",
        "PUTER_API_TOKEN", "ZAI_API_KEY", "NVIDIA_API_KEY",
        "OPENROUTER_API_KEY", "SILICONFLOW_API_KEY",
        "GLM_BRIDGE_URL", "GLM_CHAT_SCRIPT", "GLM_NODE_BIN",
        "ANTHROPIC_API_KEY", "AGENT_FORCE_TIER", "AGENT_MODEL",
        "SPACE_ID", "SPACE_HOST", "OAUTH_CLIENT_ID",
    ]
    present = {}
    for key in env_checks:
        val = os.environ.get(key, "").strip()
        present[key] = {
            "set": bool(val),
            "len": len(val) if val else 0,
            "preview": (val[:4] + "..." + val[-4:]) if len(val) > 12 else ("<set>" if val else "<not set>"),
        }
    dlog("startup", "log_startup_env", "environment variable detection",
         data={"env": present, "debug_dir": str(_DEBUG_DIR),
               "disk_enabled": _DISK_ENABLED, "stderr_enabled": _STDERR_ENABLED})


def get_recent_logs(category: str | None = None, tail: int = 50,
                    level: str | None = None) -> list[dict]:
    if category and category in _CATEGORY_FILES:
        files = [_CATEGORY_FILES[category]]
    else:
        files = [_COMBINED_LOG]
    results: list[dict] = []
    for f in files:
        if not f.exists():
            continue
        try:
            lines = f.read_text(encoding="utf-8").strip().split("\n")
            for line in reversed(lines[-tail * 2:]):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if level and entry.get("level") != level:
                    continue
                results.append(entry)
                if len(results) >= tail:
                    break
        except Exception:
            pass
        if len(results) >= tail:
            break
    return results[:tail]


def list_log_categories() -> dict:
    out = {}
    for cat, path in _CATEGORY_FILES.items():
        if path.exists():
            try:
                size = path.stat().st_size
                lines = sum(1 for _ in open(path, encoding="utf-8"))
                out[cat] = {"file": path.name, "bytes": size, "lines": lines}
            except Exception:
                out[cat] = {"file": path.name, "error": "read failed"}
        else:
            out[cat] = {"file": path.name, "exists": False}
    out["_combined"] = {
        "file": _COMBINED_LOG.name,
        "bytes": _COMBINED_LOG.stat().st_size if _COMBINED_LOG.exists() else 0,
    }
    return out


def clear_logs(category: str | None = None) -> dict:
    cleared = []
    targets = [_CATEGORY_FILES[category]] if category and category in _CATEGORY_FILES \
        else list(_CATEGORY_FILES.values()) + [_COMBINED_LOG]
    for path in targets:
        try:
            if path.exists():
                path.unlink()
                cleared.append(path.name)
        except Exception:
            pass
    return {"cleared": cleared}
