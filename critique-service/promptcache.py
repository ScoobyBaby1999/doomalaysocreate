from __future__ import annotations
import hashlib
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from oplog import atomic_write_json, log_event

# Per-space prompt cache - the panel's top free-tier utility lever. Each user runs their
# OWN single-tenant space, so caching their own (prompt, budget) -> output is safe and
# saves a real provider call on an exact repeat. In-memory LRU first, with an optional
# disk mirror (atomic writes) so the cache survives a restart/redeploy alongside jobs.
#
# Correctness rules: the key includes every output-affecting field; we ONLY cache
# successful calls; and a job marked no_store is never written. Never raises.

CACHE_DIR = Path(os.environ.get("CACHE_DIR", Path(__file__).resolve().parent / "data" / "cache"))
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "1").strip() not in ("0", "false", "no", "")
CACHE_TTL_S = float(os.environ.get("CACHE_TTL_S", str(24 * 3600)))
CACHE_MAX = int(os.environ.get("CACHE_MAX", "512"))


class PromptCache:
    def __init__(self, *, enabled: bool = CACHE_ENABLED, dirpath: Path = CACHE_DIR,
                 ttl_s: float = CACHE_TTL_S, max_entries: int = CACHE_MAX) -> None:
        self.enabled = enabled
        self.ttl_s = ttl_s
        self.max_entries = max(1, max_entries)
        self.dir = Path(dirpath)
        self._mem: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log_event("cache_dir_error", error=repr(e)[:200])
                self.enabled = False

    @staticmethod
    def key(*, logical: str, system_prompt: str, user_msg: str, max_tokens: int,
            reasoning: bool, research: bool, privacy: str = "off") -> str:
        #   privacy is part of the key: a result produced under privacy=off may have
        #   come from a training/logging host, and must never be served to a strict
        #   request (panel-found bypass, whole-repo review 2026-06).
        h = hashlib.sha256()
        for part in (logical, system_prompt, user_msg, str(int(max_tokens)),
                     "1" if reasoning else "0", "1" if research else "0", privacy):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()

    def _path(self, k: str) -> Path:
        return self.dir / f"{k}.json"

    def _fresh(self, entry: dict) -> bool:
        return (time.time() - entry.get("ts", 0)) <= self.ttl_s

    def get(self, k: str) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._mem.get(k)
            if entry is not None:
                if self._fresh(entry):
                    self._mem.move_to_end(k)
                    self.hits += 1
                    return dict(entry["value"])
                self._mem.pop(k, None)
        #   cold mem (restart) -> try the disk mirror.
        try:
            p = self._path(k)
            if p.exists():
                import json
                entry = json.loads(p.read_text(encoding="utf-8"))
                if self._fresh(entry):
                    with self._lock:
                        self._mem[k] = entry
                        self._mem.move_to_end(k)
                        self.hits += 1
                    return dict(entry["value"])
                p.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        with self._lock:
            self.misses += 1
        return None

    def put(self, k: str, value: dict) -> None:
        if not self.enabled:
            return
        entry = {"ts": time.time(), "value": value}
        with self._lock:
            self._mem[k] = entry
            self._mem.move_to_end(k)
            while len(self._mem) > self.max_entries:
                old_k, _ = self._mem.popitem(last=False)
                try:
                    self._path(old_k).unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            atomic_write_json(self._path(k), entry)
        except Exception as e:  # noqa: BLE001 - cache write must never break a call
            log_event("cache_write_error", error=repr(e)[:200])

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {"enabled": self.enabled, "entries": len(self._mem),
                    "hits": self.hits, "misses": self.misses,
                    "hit_rate": round(self.hits / total, 3) if total else 0.0,
                    "ttl_s": self.ttl_s, "max_entries": self.max_entries}
