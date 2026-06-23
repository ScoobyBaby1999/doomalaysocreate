"""Debug logging system — structured logs to stderr + in-memory buffer for cloud environments.

Every critical function logs its entry, exit, duration, and errors. Logs are
written to:
  1. stderr (HF Space logs capture this automatically)
  2. In-memory ring buffer (for /api/debug/logs endpoint)
  3. Optional: HF Dataset (for historical persistence, optional)

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
import threading
from collections import deque
from typing import Any, Callable

# --- config -----------------------------------------------------------------
_MAX_MEMORY_LOGS = 5000  # Max entries in memory ring buffer
_STDERR_ENABLED = os.environ.get("DOOMALAYSOCREATE_STDERR", "1").strip() not in ("0", "false", "no", "")
_MEMORY_LOGS_ENABLED = True

# In-memory ring buffer for recent logs
_log_buffer: deque = deque(maxlen=_MAX_MEMORY_LOGS)
_buffer_lock = threading.Lock()

# Category filter for in-memory logs (None = all categories)
_LOG_CATEGORIES = {
    "startup", "glm", "auth", "agent", "conscious", "errors", "http", "startup"
}

# Optional HF Dataset persistence (like metrics)
_HF_DATASET_ENABLED = False
_HF_DATASET_REPO = os.environ.get("DEBUG_HF_REPO", "").strip()
_HF_TOKEN = (os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_TOKEN", "")).strip()

# Background sync thread for HF Dataset persistence
_hf_sync_thread = None
_hf_sync_running = False

try:
    import threading
except ImportError:
    threading = None

def _write_log(category: str, level: str, fn: str, msg: str,
               data: dict | None, ms: float | None) -> None:
    """Write a log record to stderr and in-memory buffer."""
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

    # Always write to stderr (HF Spaces captures this)
    if _STDERR_ENABLED:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass

    # Write to in-memory ring buffer for /api/debug/logs endpoint
    if _MEMORY_LOGS_ENABLED:
        with _buffer_lock:
            _log_buffer.append(record)

def dlog(category: str, fn: str, msg: str, *,
         level: str = "INFO", data: dict | None = None, ms: float | None = None) -> None:
    """Log a debug message. Fire-and-forget, never raises."""
    try:
        _write_log(category, level, fn, msg, data, ms)
    except Exception:
        pass


def derror(category: str, fn: str, msg: str, exc: Exception | None = None,
           data: dict | None = None) -> None:
    """Log an error with exception info."""
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
    """Decorator that logs entry/exit with timing."""
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
        "SPACE_ID", "SPACE_HOST", "HF_CLIENT_ID",
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
         data={"env": present, "stderr_enabled": _STDERR_ENABLED, "memory_logs_enabled": _MEMORY_LOGS_ENABLED})


def get_recent_logs(category: str | None = None, tail: int = 50,
                    level: str | None = None) -> list[dict]:
    """Get recent logs from memory buffer (most recent first)."""
    with _buffer_lock:
        logs = list(_log_buffer)
    
    results: list[dict] = []
    for entry in reversed(logs):
        if category and entry.get("cat") != category:
            continue
        if level and entry.get("level") != level:
            continue
        results.append(entry)
        if len(results) >= tail:
            break
    return results[:tail]


def list_log_categories() -> dict:
    with _buffer_lock:
        categories = {}
        for entry in _log_buffer:
            cat = entry.get("cat", "unknown")
            if cat not in categories:
                categories[cat] = {"count": 0, "levels": {}}
            categories[cat]["count"] = categories[cat].get("count", 0) + 1
            lvl = entry.get("level", "UNKNOWN")
            categories[cat]["levels"][lvl] = categories[cat]["levels"].get(lvl, 0) + 1
    return categories


def clear_logs(category: str | None = None) -> dict:
    """Clear logs from memory buffer."""
    cleared = 0
    with _buffer_lock:
        if category:
            _log_buffer[:] = [e for e in _log_buffer if e.get("cat") != category]
            # Can't easily count removed, approximate
        else:
            cleared = len(_log_buffer)
            _log_buffer.clear()
    return {"cleared": cleared, "remaining": len(_log_buffer)}


# --- Optional HF Dataset persistence (background sync) -----------------------

def _enable_hf_dataset_sync() -> None:
    """Enable background sync to HF Dataset for log persistence."""
    global _HF_DATASET_ENABLED, _hf_sync_thread, _hf_sync_running
    
    if not _HF_DATASET_REPO or not _HF_TOKEN:
        return
    
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=_HF_TOKEN)
        # Create repo if doesn't exist
        api.create_repo(_HF_DATASET_REPO, repo_type="dataset", private=True, exist_ok=True)
        _HF_DATASET_ENABLED = True
        
        # Start background sync thread
        if threading and not _hf_sync_running:
            _hf_sync_running = True
            _hf_sync_thread = threading.Thread(target=_hf_sync_loop, daemon=True, name="debug-hf-sync")
            _hf_sync_thread.start()
            dlog("debug", "hf_sync", "HF Dataset log persistence enabled", data={"repo": _HF_DATASET_REPO})
    except Exception as e:
        dlog("debug", "hf_sync", "Failed to enable HF Dataset sync", data={"error": str(e)[:200]}, level="WARNING")


def _hf_sync_loop() -> None:
    """Background thread to periodically flush logs to HF Dataset."""
    interval = int(os.environ.get("DEBUG_HF_SYNC_EVERY", "300"))  # 5 min default
    while _hf_sync_running:
        time.sleep(interval)
        try:
            _flush_to_hf_dataset()
        except Exception as e:
            dlog("debug", "hf_sync", "HF sync error", data={"error": str(e)[:200]}, level="ERROR")


def _flush_to_hf_dataset() -> None:
    """Flush recent logs to HF Dataset."""
    if not _HF_DATASET_ENABLED:
        return
    
    try:
        from huggingface_hub import HfApi
        import tempfile
        from pathlib import Path
        
        api = HfApi(token=_HF_TOKEN)
        
        with _buffer_lock:
            logs = list(_log_buffer)
        
        if not logs:
            return
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / f"logs_{int(time.time())}.jsonl"
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in logs:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            
            api.upload_file(
                path_or_fileobj=str(tmp_path),
                path_in_repo=f"logs/logs_{int(time.time())}.jsonl",
                repo_id=_HF_DATASET_REPO,
                repo_type="dataset",
                token=_HF_TOKEN,
            )
    except Exception as e:
        dlog("debug", "hf_sync", "flush error", data={"error": str(e)[:200]}, level="ERROR")


# Initialize HF sync if configured
if _HF_DATASET_REPO and _HF_TOKEN:
    try:
        _enable_hf_dataset_sync()
    except Exception:
        pass
