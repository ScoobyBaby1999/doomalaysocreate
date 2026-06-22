from __future__ import annotations
import atexit
import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oplog import log_event

# Per-profile metrics substrate.
#
# The point of this service is no longer "merge some critiques" - it is to
# accumulate REAL-WORLD operational data so that, over time, we can predict how
# to stage a fan-out across many providers WITHOUT burning quota, judge which
# model is best at which role, and attribute behavioural quirks to models.
#
# Every judge call (real or mock) emits one event. Events are namespaced by
# PROFILE (a user has many profiles); only published provider limits are global.
# We do not care about response *content* here - just cost/throttle/latency/
# success. Content sampling is opt-in (METRICS_SAMPLE_OUTPUTS=1) for later
# qualitative review by Opus.
#
# Persistence: a Hugging Face Space filesystem is ephemeral (reset on sleep), so
# durable storage is an append-only JSONL per profile in a PUBLIC HF Dataset
# repo (optional). If configured, ALL Spaces write to the SAME dataset so metrics
# are shared and aggregated across users. Without this, the store degrades
# gracefully to in-memory + a local jsonl cache - still fully queryable within a
# session. Flushes are batched + best-effort and never break a request.

EVENT_FIELDS = (
    "ts", "profile", "logical", "provider", "model", "family", "role", "effort",
    "latency_s", "in_tokens", "out_tokens", "ok", "code", "attempts",
    "routed_to", "candidates_tried", "mock",
    "steps", "searches", "tool_calls", "reasoning_chars",
)

# keep at most this many recent events per profile in memory for live aggregation.
# the durable JSONL keeps everything; aggregates over a recent window are what
# inform routing decisions.
HOT_WINDOW = int(os.environ.get("METRICS_HOT_WINDOW", "5000"))
FLUSH_EVERY = int(os.environ.get("METRICS_FLUSH_EVERY", "25"))
SAMPLE_OUTPUTS = os.environ.get("METRICS_SAMPLE_OUTPUTS", "0").strip() in ("1", "true", "yes")

DATA_DIR = Path(os.environ.get("METRICS_DATA_DIR", Path(__file__).resolve().parent / "data" / "metrics"))


@dataclass
class _Roll:
    #   a single aggregation bucket (per provider, or per model+role).
    calls: int = 0
    ok: int = 0
    fail: int = 0
    throttle_429: int = 0
    latency_sum: float = 0.0
    in_tokens: int = 0
    out_tokens: int = 0
    last_code: str = ""
    codes: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, ev: dict) -> None:
        self.calls += 1
        if ev.get("ok"):
            self.ok += 1
        else:
            self.fail += 1
        code = ev.get("code") or ""
        if code:
            self.codes[code] += 1
            self.last_code = code
        if code == "429":
            self.throttle_429 += 1
        self.latency_sum += float(ev.get("latency_s") or 0.0)
        self.in_tokens += int(ev.get("in_tokens") or 0)
        self.out_tokens += int(ev.get("out_tokens") or 0)

    def view(self) -> dict:
        avg_latency = round(self.latency_sum / self.calls, 2) if self.calls else 0.0
        return {
            "calls": self.calls, "ok": self.ok, "fail": self.fail,
            "success_rate": round(self.ok / self.calls, 3) if self.calls else None,
            "throttle_429": self.throttle_429,
            "throttle_rate": round(self.throttle_429 / self.calls, 3) if self.calls else None,
            "avg_latency_s": avg_latency,
            "in_tokens": self.in_tokens, "out_tokens": self.out_tokens,
            "last_code": self.last_code,
            "codes": dict(self.codes),
        }


class _ProfileMetrics:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.by_provider: dict[str, _Roll] = defaultdict(_Roll)
        self.by_model_role: dict[str, _Roll] = defaultdict(_Roll)
        #       sliding rolling-day token/call counts per provider for budget hints.
        self.day_calls: dict[str, list[float]] = defaultdict(list)   # provider -> call timestamps
        self.day_tokens: dict[str, list[tuple[float, int]]] = defaultdict(list)

    def add(self, ev: dict) -> None:
        self.events.append(ev)
        if len(self.events) > HOT_WINDOW:
            self.events = self.events[-HOT_WINDOW:]
        self.by_provider[ev["provider"]].add(ev)
        self.by_model_role[f"{ev.get('model')}|{ev.get('role')}"].add(ev)
        now = ev.get("_now") or time.time()
        prov = ev["provider"]
        self.day_calls[prov].append(now)
        tok = int(ev.get("in_tokens") or 0) + int(ev.get("out_tokens") or 0)
        if tok:
            self.day_tokens[prov].append((now, tok))
        #   prune >24h entries on WRITE too (not just on the budget-read path) so a
        #   long-running instance can't grow these lists without bound.
        cutoff = now - 24 * 3600.0
        if self.day_calls[prov] and self.day_calls[prov][0] < cutoff:
            self.day_calls[prov] = [t for t in self.day_calls[prov] if t >= cutoff]
        if self.day_tokens[prov] and self.day_tokens[prov][0][0] < cutoff:
            self.day_tokens[prov] = [(t, n) for (t, n) in self.day_tokens[prov] if t >= cutoff]


class MetricStore:
    """thread-safe, per-profile metrics capture with optional HF-Dataset persistence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profiles: dict[str, _ProfileMetrics] = defaultdict(_ProfileMetrics)
        self._unflushed: dict[str, list[dict]] = defaultdict(list)
        self._since_flush = 0
        self._hf = _HFSink()
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.load()
        atexit.register(self.flush)

    def record(self, *, profile: str, logical: str, provider: str, model: str,
               family: str, role: str, effort: str, latency_s: float,
               in_tokens: int | None, out_tokens: int | None, ok: bool, code: str,
               attempts: int, routed_to: str | None,
               candidates_tried: list[str] | None = None, mock: bool = False,
               output: str | None = None, steps: int | None = None,
               searches: int | None = None, tool_calls: int | None = None,
               reasoning_chars: int | None = None) -> None:
        now = time.time()
        ev: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "profile": profile, "logical": logical, "provider": provider,
            "model": model, "family": family, "role": role, "effort": effort,
            "latency_s": round(float(latency_s or 0.0), 2),
            "in_tokens": in_tokens, "out_tokens": out_tokens,
            "ok": bool(ok), "code": code, "attempts": attempts,
            "routed_to": routed_to, "candidates_tried": candidates_tried or [],
            "mock": bool(mock),
            "steps": steps, "searches": searches, "tool_calls": tool_calls,
            "reasoning_chars": reasoning_chars,
        }
        if SAMPLE_OUTPUTS and output:
            ev["output_sample"] = output[:500]
        store_ev = dict(ev, _now=now)
        with self._lock:
            self._profiles[profile].add(store_ev)
            self._unflushed[profile].append(ev)
            self._since_flush += 1
            due = self._since_flush >= FLUSH_EVERY
        if due:
            self.flush()

    def provider_day_usage(self, profile: str, provider: str) -> tuple[int, int]:
        #   (calls, tokens) for this provider in the trailing 24h for this profile.
        cutoff = time.time() - 86400.0
        with self._lock:
            pm = self._profiles.get(profile)
            if pm is None:
                return (0, 0)
            calls = [t for t in pm.day_calls.get(provider, []) if t >= cutoff]
            pm.day_calls[provider] = calls
            toks = [(t, n) for (t, n) in pm.day_tokens.get(provider, []) if t >= cutoff]
            pm.day_tokens[provider] = toks
            return (len(calls), sum(n for _, n in toks))

    def aggregates(self, profile: str) -> dict:
        with self._lock:
            pm = self._profiles.get(profile)
            if pm is None:
                return {"profile": profile, "events": 0, "by_provider": {}, "by_model_role": {}}
            by_provider = {k: v.view() for k, v in sorted(pm.by_provider.items())}
            by_model_role = {k: v.view() for k, v in sorted(pm.by_model_role.items())}
            return {
                "profile": profile,
                "events": len(pm.events),
                "by_provider": by_provider,
                "by_model_role": by_model_role,
            }

    def profiles(self) -> list[str]:
        with self._lock:
            return sorted(self._profiles.keys())

    # --- persistence --------------------------------------------------------
    def _local_path(self, profile: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in profile) or "default"
        return DATA_DIR / f"{safe}.jsonl"

    def load(self) -> None:
        #   on boot: pull durable JSONL from HF (if configured) then replay local
        #   cache into the in-memory aggregates so /api/metrics shows history.
        self._hf.download_all(DATA_DIR)
        try:
            files = list(DATA_DIR.glob("*.jsonl"))
        except OSError:
            files = []
        loaded = 0
        for f in files:
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    prof = ev.get("profile", "default")
                    self._profiles[prof].add(dict(ev, _now=time.time()))
                    loaded += 1
            except (OSError, ValueError):
                continue
        if loaded:
            log_event("metrics_loaded", events=loaded, files=len(files))

    def flush(self) -> None:
        #   append unflushed events to local jsonl, then best-effort push to HF.
        with self._lock:
            pending = {p: evs[:] for p, evs in self._unflushed.items() if evs}
            for p in pending:
                self._unflushed[p].clear()
            self._since_flush = 0
        if not pending:
            return
        touched: list[Path] = []
        for profile, evs in pending.items():
            path = self._local_path(profile)
            try:
                with path.open("a", encoding="utf-8") as fh:
                    for ev in evs:
                        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                touched.append(path)
            except OSError as e:
                log_event("metrics_flush_error", profile=profile, error=repr(e)[:200])
        for path in touched:
            self._hf.upload(path)


class _HFSink:
    """optional durable sink: mirrors per-profile JSONL to a PUBLIC HF Dataset."""

    def __init__(self) -> None:
        self.repo_id = os.environ.get("METRICS_PUBLIC_HF_REPO", "").strip()
        self.token = (os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_TOKEN", "")).strip()
        self._api = None
        self.enabled = False
        if not (self.repo_id and self.token):
            return
        try:
            from huggingface_hub import HfApi
            self._api = HfApi(token=self.token)
            self._api.create_repo(self.repo_id, repo_type="dataset", private=False, exist_ok=True)
            self.enabled = True
            log_event("metrics_hf_enabled", repo=self.repo_id)
        except Exception as e:  # noqa: BLE001 - persistence must never break boot
            log_event("metrics_hf_disabled", reason=repr(e)[:200])
            self.enabled = False
        if not (self.repo_id and self.token):
            return
        try:
            from huggingface_hub import HfApi
            self._api = HfApi(token=self.token)
            # PUBLIC dataset - anyone can read, authenticated users with write token can write
            self._api.create_repo(self.repo_id, repo_type="dataset", private=False, exist_ok=True)
            self.enabled = True
            log_event("metrics_hf_enabled", repo=self.repo_id, public=True)
        except Exception as e:  # noqa: BLE001 - persistence must never break boot
            log_event("metrics_hf_disabled", reason=repr(e)[:200])
            self.enabled = False

    def upload(self, path: Path) -> None:
        if not self.enabled or self._api is None:
            return
        for attempt in range(3):
            try:
                self._api.upload_file(
                    path_or_fileobj=str(path),
                    path_in_repo=f"metrics/{path.name}",
                    repo_id=self.repo_id,
                    repo_type="dataset",
                )
                return
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    log_event("metrics_hf_upload_error", file=path.name, error=repr(e)[:200])
                    return
                time.sleep(2 ** attempt)

    def download_all(self, dest: Path) -> None:
        if not self.enabled or self._api is None:
            return
        try:
            from huggingface_hub import snapshot_download
            # Public dataset - no token needed for download, but use if available
            snap = snapshot_download(self.repo_id, repo_type="dataset", token=self.token,
                                     allow_patterns="metrics/*.jsonl")
            src = Path(snap) / "metrics"
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.jsonl"):
                    (dest / f.name).write_bytes(f.read_bytes())
        except Exception as e:  # noqa: BLE001
            log_event("metrics_hf_download_error", reason=repr(e)[:200])
