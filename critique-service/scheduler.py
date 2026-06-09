from __future__ import annotations
import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from content.roles import Roles
from providers import slot
from oplog import log_event

# Stream responses by default. Long-reasoning models (DeepSeek V4 Pro / R1, etc.)
# emit large outputs that, non-streamed, make the provider hold the connection
# until done - tripping gateway 504s / read timeouts even though the model streams
# instantly in the vendor playground. STREAM=0 to disable.
STREAM_DEFAULT = os.environ.get("STREAM", "1").strip() not in ("0", "false", "no", "")


# scheduler: picks slots by score + rotation + cooldown, paces calls per provider,
# and translates provider responses into ProviderError codes the orchestrator can route.

# cooldowns - 429 means rate limit, give that slot a long break.
# everything else short-cools by a minute. keep blacklist for hard 4xx.
cooldown_rate_limit: float = 300.0
cooldown_minute: float = 60.0

# family score recency multiplier - fresh wins (within 60s) count fully,
# old wins (>30min) decay to 0.7x.
rank_success_recently: float = 60.0
rank_success_window: float = 1800.0
rank_low_multiplier: float = 0.7
rank_default_multiplier: float = 0.9


# stat buckets - one per family, one per slot.

@dataclass
class FamilyState:                # tracks how a model family (across providers) is doing
    successes: int = 0
    failures: int = 0
    last_success_ts: float = 0.0


@dataclass
class SlotState:                  # tracks one (provider, model) slot's health
    cooldown_until: float = 0.0
    blacklisted: bool = False
    last_error: str = ""


class SchedulerError(RuntimeError):
    """raised when no slot can serve the requested role."""


class ProviderError(RuntimeError):
    """llm error - 4xx / 5xx / http: / json: / shape: / empty: / refusal:"""


# error code classes - the orchestrator uses these to decide whether to
# retry, blacklist, or pause the whole run.
network_codes = {"http", "5xx", "524"}      # transient network / server problems
access_codes = {"400", "401", "403", "404"} # slot is permanently bad


def is_network_code(code: str) -> bool:
    #   used by the orchestrator to detect "all-attempts-failed-the-network".
    #   getaddrinfo / connection-reset / read timeouts all flow into 'http:'.
    return code in network_codes


class SlotScheduler:
    """pick slots using provider rotation + family score + cooldown."""

    def __init__(self, slots: list[slot]) -> None:
        self.slots: list[slot] = list(slots)
        self.slot_state: dict[str, SlotState] = {s.who: SlotState() for s in slots}
        families = {s.model_family for s in slots}
        self.family_state: dict[str, FamilyState] = {f: FamilyState() for f in families}
        #       this scheduler is now shared across the request-handler threads
        #       (each runs its own asyncio loop via asyncio.run) AND the JobRunner's
        #       background loop, so all mutable state is guarded by a threading.Lock.
        #       pacing reserves the next call slot under the lock and sleeps OUTSIDE
        #       it (you can't hold a threading.Lock across await).
        self._lock = threading.Lock()
        self.provider_last_call: dict[str, float] = {}
        #       per-provider success counts (telemetry) and ATTEMPT counts. rotation
        #       ranks by attempts (every pick), not successes, so a chronically
        #       failing provider accrues attempts and rotates to the back instead of
        #       staying at 0 successes and hogging first pick. seeded to 0 per
        #       process; pure runtime state, no disk.
        self.provider_calls: dict[str, int] = {}
        self.provider_attempts: dict[str, int] = {}
        for s in slots:
            self.provider_calls.setdefault(s.provider.name, 0)
            self.provider_attempts.setdefault(s.provider.name, 0)

    def add_slots(self, slots: list[slot]) -> None:
        #   register slots discovered after construction (logical-model candidates
        #   / panel-named physical slots) so they join rotation. idempotent.
        with self._lock:
            for s in slots:
                if s.who in self.slot_state:
                    continue
                self.slots.append(s)
                self.slot_state[s.who] = SlotState()
                self.family_state.setdefault(s.model_family, FamilyState())
                self.provider_calls.setdefault(s.provider.name, 0)
                self.provider_attempts.setdefault(s.provider.name, 0)

    def recency_mult(self, family: str) -> float:
        #   recent win -> 1x. nothing recent -> default 0.9. old win decays to 0.7.
        family_stats = self.family_state[family]
        if family_stats.last_success_ts == 0.0:
            return rank_default_multiplier
        age = time.time() - family_stats.last_success_ts
        if age <= rank_success_recently:
            return 1.0
        if age >= rank_success_window:
            return rank_low_multiplier
        span = rank_success_window - rank_success_recently
        frac = (age - rank_success_recently) / span
        return 1.0 - frac * (1.0 - rank_low_multiplier)

    def family_score(self, family: str) -> float:
        #   laplace-smoothed success rate, scaled by recency. flaky models drop fast.
        family_stats = self.family_state[family]
        base = (family_stats.successes + 1) / (family_stats.successes + family_stats.failures + 2)
        return base * self.recency_mult(family)

    def is_eligible(self, candidate: slot, role: Roles, exclude: set[str], now: float) -> bool:
        if role not in candidate.roles:
            return False
        state = self.slot_state[candidate.who]
        if state.blacklisted:
            return False
        if state.cooldown_until > now:
            return False
        if candidate.provider.name in exclude:
            return False
        return True

    def rank_key(self, candidate: slot) -> tuple:
        #   sort key for slot picking. lowest tuple wins.
        #     1. provider_attempts asc - rotate; fewest-ATTEMPTED provider wins so a
        #        repeatedly-failing provider rotates to the back (not stuck at first).
        #     2. family_score desc     - prefer healthier model families
        #     3. last_call asc         - freshest within a tie
        provider_attempts = self.provider_attempts.get(candidate.provider.name, 0)
        family_score = self.family_score(candidate.model_family)
        last_call = self.provider_last_call.get(candidate.provider.name, 0.0)
        return (provider_attempts, -family_score, last_call)

    def _mark_attempt(self, picked: slot) -> None:
        #   called under the lock when a slot is selected. drives attempt-based rotation.
        self.provider_attempts[picked.provider.name] = (
            self.provider_attempts.get(picked.provider.name, 0) + 1
        )

    @staticmethod
    def _as_exclude(exclude_providers: set[str] | str | None) -> set[str]:
        if exclude_providers is None:
            return set()
        if isinstance(exclude_providers, str):
            return {exclude_providers}
        return set(exclude_providers)

    def pick_slot(self, role: Roles, exclude_providers: set[str] | str | None = None) -> slot:
        exclude = self._as_exclude(exclude_providers)
        with self._lock:
            now = time.time()
            eligible = [s for s in self.slots if self.is_eligible(s, role, exclude, now)]

            #       fallback: relax the exclude (rotation rule) - better to repeat a
            #       provider than to fail the call entirely.
            if not eligible and exclude:
                relaxed = [s for s in self.slots if self.is_eligible(s, role, set(), now)]
                if relaxed:
                    log_event("rotation_relaxed",
                              role=role.value, excluded=sorted(exclude),
                              relaxed_count=len(relaxed))
                    eligible = relaxed

            if not eligible:
                cooling = sum(1 for s in self.slot_state.values() if s.cooldown_until > now)
                blacklisted = sum(1 for s in self.slot_state.values() if s.blacklisted)
                raise SchedulerError(
                    f"role={role.value}: no eligible carriers ("
                    f"{cooling} cooling, {blacklisted} blacklisted, "
                    f"{len(self.slots)} total)"
                )
            eligible.sort(key=self.rank_key)
            chosen = eligible[0]
            self._mark_attempt(chosen)
            return chosen

    def pick_slot_from(self, candidates: list[slot], role: Roles,
                       exclude_providers: set[str] | str | None = None) -> slot | None:
        #   like pick_slot but restricted to a candidate subset (one logical model's
        #   hosts). returns the best eligible candidate, or None if all are excluded /
        #   cooling / blacklisted. used by route_judge to bounce across providers.
        exclude = self._as_exclude(exclude_providers)
        with self._lock:
            now = time.time()
            eligible = [s for s in candidates if self.is_eligible(s, role, exclude, now)]
            if not eligible:
                return None
            eligible.sort(key=self.rank_key)
            chosen = eligible[0]
            self._mark_attempt(chosen)
            return chosen

    def record_success(self, picked: slot) -> None:
        with self._lock:
            family = self.family_state[picked.model_family]
            family.successes += 1
            family.last_success_ts = time.time()
            state = self.slot_state[picked.who]
            state.cooldown_until = 0.0
            state.last_error = ""
            #       provider rotation - count successful calls per provider so the
            #       next pick rotates to whichever has been used least.
            self.provider_calls[picked.provider.name] = (
                self.provider_calls.get(picked.provider.name, 0) + 1
            )

    def set_budget_cooldown(self, provider_name: str, until_ts: float, reason: str = "") -> None:
        #   cool every slot of a provider until until_ts - used when per-profile
        #   metrics show the provider is at/over its published daily budget, so we
        #   stop routing to it without a real 429. reuses the is_eligible cooldown path.
        with self._lock:
            n = 0
            for s in self.slots:
                if s.provider.name == provider_name:
                    st = self.slot_state[s.who]
                    if until_ts > st.cooldown_until:
                        st.cooldown_until = until_ts
                        n += 1
        if n:
            log_event("budget_cooldown", provider=provider_name,
                      slots=n, until_s=round(until_ts - time.time(), 1), reason=reason[:160])

    def _record_failure_locked(self, picked: slot, code: str, reason: str = "") -> None:
        #   different fail codes route to different penalties:
        #     json/shape/empty/refusal -> family-wide demerit
        #     429                      -> long slot cooldown
        #     400/401/403/404/524      -> blacklist the slot for the run
        #     other (5xx, http, ...)   -> short slot cooldown
        state = self.slot_state[picked.who]
        state.last_error = f"{code}:{reason}"[:200]

        if code in ("json", "shape", "empty", "refusal"):
            self.family_state[picked.model_family].failures += 1
            log_event("family_penalty",
                      slot=picked.who, family=picked.model_family,
                      code=code, reason=reason[:160])
        elif code == "429":
            #       proportional cooldown - low-rpm providers come back fast,
            #       high-rpm ones wait longer. floor at 15s so we don't hammer
            #       a freshly-rate-limited slot, ceiling at the legacy 5min.
            cooldown_s = min(cooldown_rate_limit, max(15.0, 120.0 / picked.provider.rpm * 2))
            state.cooldown_until = time.time() + cooldown_s
            log_event("cooldown_set",
                      slot=picked.who, code=code, rpm=picked.provider.rpm,
                      cooldown_s=round(cooldown_s, 1), reason=reason[:160])
        elif code in ("400", "401", "403", "404"):
            #       hard access errors - the slot is broken for this account.
            state.blacklisted = True
            log_event("slot_blacklisted",
                      slot=picked.who, code=code, reason=reason[:160])
        elif code in ("413", "422"):
            #       payload-size / shape rejection - this call won't fit,
            #       but smaller calls might. cool the slot briefly so we rotate.
            state.cooldown_until = time.time() + cooldown_minute
            log_event("cooldown_set",
                      slot=picked.who, code=code,
                      cooldown_s=cooldown_minute, reason=reason[:160])
        else:
            #       transient network / 5xx / unknown - short cooldown.
            state.cooldown_until = time.time() + cooldown_minute
            log_event("cooldown_set",
                      slot=picked.who, code=code,
                      cooldown_s=cooldown_minute, reason=reason[:160])

    def record_failure(self, picked: slot, code: str, reason: str = "") -> None:
        with self._lock:
            self._record_failure_locked(picked, code, reason)

    async def wait_for_provider_pacing(self, picked: slot) -> None:
        #   pace calls per provider to stay under rpm. RESERVE the next allowed
        #   call time under the threading lock (so concurrent callers across loops
        #   stack their waits), then sleep outside the lock. min_gap=0 disables.
        name = picked.provider.name
        min_gap = 60.0 / max(1, picked.provider.rpm)
        with self._lock:
            now = time.time()
            last = self.provider_last_call.get(name, 0.0)
            earliest = max(now, last + min_gap)
            self.provider_last_call[name] = earliest
            sleep_s = earliest - now
        if sleep_s > 0:
            log_event("pacing_wait",
                      provider=name, slot=picked.who,
                      sleep_s=round(sleep_s, 2), rpm=picked.provider.rpm)
            await asyncio.sleep(sleep_s)

    def earliest_wake_ts(self) -> float:
        #   for the runner: when is the soonest non-blacklisted slot off cooldown?
        with self._lock:
            cooling = [
                state.cooldown_until for state in self.slot_state.values()
                if not state.blacklisted and state.cooldown_until > 0.0
            ]
        return min(cooling) if cooling else 0.0

    def snapshot(self) -> list[dict]:
        #   debug/telemetry view - one row per slot, sorted best-family-first.
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> list[dict]:
        now = time.time()
        rows = []
        for s in self.slots:
            state = self.slot_state[s.who]
            family = self.family_state[s.model_family]
            rows.append({
                "slot": s.who,
                "family": s.model_family,
                "roles": [r.value for r in s.roles],
                "family_successes": family.successes,
                "family_failures": family.failures,
                "family_score": round(self.family_score(s.model_family), 3),
                "cooling_s": max(0.0, round(state.cooldown_until - now, 1)),
                "blacklisted": state.blacklisted,
                "last_error": state.last_error,
            })
        rows.sort(key=lambda r: r["family_score"], reverse=True)
        return rows

    def provider_rollup(self) -> dict[str, dict]:
        #   per-provider rotation + health summary for /api/stats.
        with self._lock:
            now = time.time()
            out: dict[str, dict] = {}
            for s in self.slots:
                st = self.slot_state[s.who]
                row = out.setdefault(s.provider.name, {
                    "calls": self.provider_calls.get(s.provider.name, 0),
                    "slots": 0, "cooling_slots": 0, "blacklisted_slots": 0,
                    "pool": s.provider.pool, "rpm": s.provider.rpm,
                    "rpd": s.provider.rpd, "tpd": s.provider.tpd,
                })
                row["slots"] += 1
                if st.cooldown_until > now:
                    row["cooling_slots"] += 1
                if st.blacklisted:
                    row["blacklisted_slots"] += 1
            return out


# the http call. all retries / rotation / cooldowns happen one level up; here we
# just translate one http exchange into either a (content, usage) pair or a
# ProviderError. when MOCK_MODE is on it routes to the synthetic mock provider so
# the whole stack can run with zero real API calls.

async def call_slot(client: httpx.AsyncClient, picked: slot, messages: list[dict[str, str]],
                    *, max_tokens: int, timeout_s: float = 600.0,
                    response_format: dict | None = None,
                    extra_body: dict | None = None) -> tuple[str, dict]:
    #   extra_body carries per-model reasoning/thinking fields (from reasoning_catalog)
    #   that are shallow-merged into the request. On success, usage may include a
    #   "reasoning_content" key when a thinking model streamed its trace.
    import mock_provider
    if mock_provider.ENABLED:
        return await mock_provider.mock_call_slot(
            client, picked, messages, max_tokens=max_tokens,
            timeout_s=timeout_s, response_format=response_format)

    p = picked.provider
    #   clamp to the provider's published per-request output ceiling (e.g. GitHub
    #   Models free tier rejects ~4k+). everywhere else stays uncapped so frontier
    #   models can use their full budget.
    if p.max_out:
        max_tokens = min(max_tokens, p.max_out)
    headers = {
        "Authorization": f"Bearer {p.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(p.extra_headers)

    body: dict[str, Any] = {
        "model": picked.model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        body["response_format"] = response_format
    if extra_body:
        body.update(extra_body)

    log_base = dict(provider=p.name, model=picked.model, family=picked.model_family,
                    slot=picked.who)

    if STREAM_DEFAULT:
        return await _call_slot_stream(client, picked, headers, body, timeout_s, log_base)

    t_call = time.monotonic()
    status: int | None = None
    try:
        response = await client.post(p.url, headers=headers, json=body, timeout=timeout_s)
        status = response.status_code
        duration = round(time.monotonic() - t_call, 2)

        # http status routing - 429 cools, 4xx blacklists, 5xx cools.
        if status == 429:
            log_event("call_fail", **log_base, http_status=429,
                      fail_code="429", reason="HTTP 429", duration_s=duration)
            raise ProviderError(f"429:HTTP 429 from {picked.who}")
        if status in (400, 401, 403, 404, 413, 422):
            #       413 = payload too large (provider request size limit hit).
            #       422 = unprocessable entity (model rejects message shape).
            #       both are slot/provider-specific - rotate away within the stage,
            #       but they're NOT network-class so the pause logic won't trigger.
            msg = f"HTTP {status}: {response.text[:160]}"
            log_event("call_fail", **log_base, http_status=status,
                      fail_code=str(status), reason=msg, duration_s=duration)
            raise ProviderError(f"{status}:{msg}")
        if status is not None and status >= 500:
            msg = f"HTTP {status}: {response.text[:160]}"
            code = "524" if status == 524 else "5xx"
            log_event("call_fail", **log_base, http_status=status,
                      fail_code=code, reason=msg, duration_s=duration)
            raise ProviderError(f"{code}:{msg}")
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError as e:
            log_event("call_fail", **log_base, http_status=status,
                      fail_code="json", reason=f"non-json: {e!r}", duration_s=duration)
            raise ProviderError(f"json:{e!r}")

        # openrouter quirk: upstream errors arrive as http 200 with a body
        # like {"error": {"code": ..., "message": ...}}. catch that.
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            err_code = str(err.get("code", "unknown"))
            msg = err.get("message", "")[:200]
            log_event("call_fail", **log_base, http_status=status,
                      fail_code=err_code, upstream_code=err_code,
                      reason=f"upstream {err_code}: {msg}", duration_s=duration)
            raise ProviderError(f"{err_code}:upstream {err_code}: {msg}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            msg = f"bad shape: {e!r}; body={str(data)[:200]}"
            log_event("call_fail", **log_base, http_status=status,
                      fail_code="shape", reason=msg[:300], duration_s=duration)
            raise ProviderError(f"shape:{msg}")

        if not content or not content.strip():
            log_event("call_fail", **log_base, http_status=status,
                      fail_code="empty", reason="empty content", duration_s=duration)
            raise ProviderError(f"empty:empty content from {picked.who}")

        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        log_event("call_ok", **log_base, http_status=status,
                  duration_s=duration,
                  in_tokens=usage.get("prompt_tokens"),
                  out_tokens=usage.get("completion_tokens"),
                  out_chars=len(content))
        return content, (usage if isinstance(usage, dict) else {})

    except httpx.HTTPError as e:
        duration = round(time.monotonic() - t_call, 2)
        log_event("call_fail", **log_base, http_status=status,
                  fail_code="http", reason=repr(e)[:300], duration_s=duration)
        raise ProviderError(f"http:{e!r}")



async def _call_slot_stream(client: httpx.AsyncClient, picked: slot, headers: dict,
                            body: dict, timeout_s: float, log_base: dict) -> tuple[str, dict]:
    #   streaming variant: keeps the connection alive across long generations and
    #   surfaces reasoning_content for thinking models. accumulates SSE deltas into
    #   one (content, usage) result, mapping the same error codes as the sync path.
    p = picked.provider
    sbody = dict(body, stream=True, stream_options={"include_usage": True})
    t_call = time.monotonic()
    status: int | None = None
    try:
        async with client.stream("POST", p.url, headers=headers, json=sbody, timeout=timeout_s) as response:
            status = response.status_code
            if status != 200:
                text = (await response.aread()).decode("utf-8", "replace")[:200]
                duration = round(time.monotonic() - t_call, 2)
                if status == 429:
                    log_event("call_fail", **log_base, http_status=429, fail_code="429",
                              reason="HTTP 429", duration_s=duration)
                    raise ProviderError(f"429:HTTP 429 from {picked.who}")
                if status in (400, 401, 403, 404, 413, 422):
                    log_event("call_fail", **log_base, http_status=status, fail_code=str(status),
                              reason=f"HTTP {status}: {text}", duration_s=duration)
                    raise ProviderError(f"{status}:HTTP {status}: {text}")
                code = "524" if status == 524 else ("5xx" if status >= 500 else "http")
                log_event("call_fail", **log_base, http_status=status, fail_code=code,
                          reason=f"HTTP {status}: {text}", duration_s=duration)
                raise ProviderError(f"{code}:HTTP {status}: {text}")

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: dict = {}
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    err = chunk["error"]
                    ecode = str(err.get("code", "unknown"))
                    raise ProviderError(f"{ecode}:upstream {ecode}: {str(err.get('message',''))[:200]}")
                for ch in (chunk.get("choices") or []):
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning_parts.append(delta["reasoning_content"])
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        if not content.strip():
            content = reasoning
        duration = round(time.monotonic() - t_call, 2)
        if not content or not content.strip():
            log_event("call_fail", **log_base, http_status=status, fail_code="empty",
                      reason="empty content (stream)", duration_s=duration)
            raise ProviderError(f"empty:empty content from {picked.who}")
        if reasoning.strip():
            usage = dict(usage or {}, reasoning_content=reasoning)
        log_event("call_ok", **log_base, http_status=status, duration_s=duration,
                  in_tokens=usage.get("prompt_tokens"), out_tokens=usage.get("completion_tokens"),
                  out_chars=len(content), reasoning_chars=len(reasoning), streamed=True)
        return content, usage
    except httpx.HTTPError as e:
        duration = round(time.monotonic() - t_call, 2)
        log_event("call_fail", **log_base, http_status=status,
                  fail_code="http", reason=repr(e)[:300], duration_s=duration)
        raise ProviderError(f"http:{e!r}")
