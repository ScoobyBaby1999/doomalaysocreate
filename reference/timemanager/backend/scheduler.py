from __future__ import annotations
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from content.roles import Roles
from providers import slot
from oplog import log_event


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
        #       per-provider locks let us serialize calls to the same provider so
        #       rpm pacing actually works under concurrency.
        self.provider_last_call: dict[str, float] = {}
        self.provider_locks: dict[str, asyncio.Lock] = {}
        #       per-provider call counts drive the rotation - the slot whose
        #       provider has the fewest successful calls wins. seeded to 0
        #       per process; pure runtime state, no disk.
        self.provider_calls: dict[str, int] = {}
        for s in slots:
            self.provider_calls.setdefault(s.provider.name, 0)

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
        #     1. provider_calls asc - rotate; fewest-used provider wins
        #     2. family_score desc  - prefer healthier model families
        #     3. last_call asc      - freshest within a tie
        provider_calls = self.provider_calls.get(candidate.provider.name, 0)
        family_score = self.family_score(candidate.model_family)
        last_call = self.provider_last_call.get(candidate.provider.name, 0.0)
        return (provider_calls, -family_score, last_call)

    def pick_slot(self, role: Roles, exclude_providers: set[str] | str | None = None) -> slot:
        #   resolve exclude param into a set.
        if exclude_providers is None:
            exclude: set[str] = set()
        elif isinstance(exclude_providers, str):
            exclude = {exclude_providers}
        else:
            exclude = set(exclude_providers)

        now = time.time()
        eligible = [s for s in self.slots if self.is_eligible(s, role, exclude, now)]

        #       fallback: relax the exclude (rotation rule) - better to repeat a provider
        #       than to fail the call entirely.
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
        return eligible[0]

    def record_success(self, picked: slot) -> None:
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

    def record_failure(self, picked: slot, code: str, reason: str = "") -> None:
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

    async def wait_for_provider_pacing(self, picked: slot) -> None:
        #   serialize calls per provider, sleep just enough to stay under rpm.
        name = picked.provider.name
        lock = self.provider_locks.setdefault(name, asyncio.Lock())
        min_gap = 60.0 / max(1, picked.provider.rpm)
        async with lock:
            last = self.provider_last_call.get(name, 0.0)
            gap = time.time() - last
            if gap < min_gap:
                sleep_s = min_gap - gap
                log_event("pacing_wait",
                          provider=name, slot=picked.who,
                          sleep_s=round(sleep_s, 2), rpm=picked.provider.rpm)
                await asyncio.sleep(sleep_s)
            self.provider_last_call[name] = time.time()

    def earliest_wake_ts(self) -> float:
        #   for the runner: when is the soonest non-blacklisted slot off cooldown?
        cooling = [
            state.cooldown_until for state in self.slot_state.values()
            if not state.blacklisted and state.cooldown_until > 0.0
        ]
        return min(cooling) if cooling else 0.0

    def snapshot(self) -> list[dict]:
        #   debug/telemetry view - one row per slot, sorted best-family-first.
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


# the http call. all retries / rotation / cooldowns happen one level up; here we
# just translate one http exchange into either a string body or a ProviderError.

async def call_slot(client: httpx.AsyncClient, picked: slot, messages: list[dict[str, str]],
                    *, max_tokens: int, timeout_s: float = 600.0,
                    response_format: dict | None = None) -> str:
    p = picked.provider
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

    log_base = dict(provider=p.name, model=picked.model, family=picked.model_family,
                    slot=picked.who)

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
        return content

    except httpx.HTTPError as e:
        duration = round(time.monotonic() - t_call, 2)
        log_event("call_fail", **log_base, http_status=status,
                  fail_code="http", reason=repr(e)[:300], duration_s=duration)
        raise ProviderError(f"http:{e!r}")
