#!/usr/bin/env python3
"""
scheduler.py - Slot picker / multi-armed bandit for the orchestrator.

# What this module is for

Every LLM call in the pipeline goes through `pick_slot(role, exclude_providers)`.
The scheduler decides:

  1. Stage eligibility   — slots that don't declare this role are filtered out.
  2. Availability        — blacklisted or cooling slots are hidden.
  3. Cross-stage rotation — the previous stage's provider is excluded
                            (set, not single — fanout shards exclude
                            each other's providers too).
  4. Score-driven pick   — among eligible slots, prefer the model with
                            the best track record (model-family bandit
                            score), then the carrier with the longest
                            idle time (provider-spreading).

# Two state tables, distinct concerns

  FamilyState       Per model_family: pooled successes/failures across
                    every carrier serving the same model. Bandit score
                    reads from here. Penalties only on model-quality
                    failures (json/shape/empty/refusal) — provider-side
                    failures (429, 524, 4xx) don't penalize the model.

  SlotState         Per (provider, model) slot: cooldown_until,
                    blacklisted, last_error. Provider-side failures
                    write here. A 429 on Cerebras's Llama doesn't
                    cool Groq's Llama; one's a carrier issue, the
                    other's a different account entirely.

This split is the key correctness invariant of the scheduler: short-term
provider issues don't pollute long-term model-quality learning, and
vice versa.

# Per-provider pacing

Free-tier RPM is per-account. The scheduler tracks `last_call_ts` keyed
by provider name and serializes calls within a provider via an asyncio
lock so concurrent fanout shards space themselves at min_gap = 60/rpm
without bursting.

# State lifetime

All in-memory, lives for one orchestrator run. On restart, scores reset
and the bandit re-learns. Cooldowns reset too (any hot locks forgotten).
This is intentional — scheduler state isn't useful across long gaps.
Durable state (which prompts are done) lives in runner.py's _state.json.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from oplog import log_event
from orchestrator.roles import Role
from providers import Slot


# ---------- Tunable constants ----------

# Cooldown durations.
#   429 → 5 min cooldown (free-tier quotas refill on minute boundaries)
#   transient (5xx other than 524, network errors) → 1 min cooldown
#   permanent (524, 400/401/403/404) → session blacklist (no cooldown)
COOLDOWN_429_S: float = 300.0
COOLDOWN_TRANSIENT_S: float = 60.0

# Recency multiplier on the family score.
#   last_success within RECENCY_FRESH_S → 1.0×
#   last_success older than RECENCY_DECAY_S → RECENCY_FLOOR (0.7×)
#   linear decay between
#   never-succeeded → RECENCY_NEW (0.9×, midway — give it a shot)
RECENCY_FRESH_S: float = 60.0
RECENCY_DECAY_S: float = 1800.0
RECENCY_FLOOR: float = 0.7
RECENCY_NEW: float = 0.9

# Refusal-phrase regex applied to every call's output. If a model returns
# text that starts with any of these, we treat it as a model-quality
# failure (penalize family, retry on a different slot). Captures the
# "I cannot help with that" / "As an AI..." class of refusals that
# would otherwise propagate into the body.
_REFUSAL_RE = re.compile(
    r"^\s*(i\s+(cannot|can'?t|won'?t|am\s+unable|am\s+sorry)|"
    r"as\s+an\s+ai|i\s+do\s+not\s+have\s+access|"
    r"i\s+apologize,?\s+but)\b",
    re.IGNORECASE,
)

# Where to write per-call telemetry. Append-only JSONL.
_RATE_LOG_PATH = (
    Path(__file__).resolve().parent.parent / "vault" / "research" / "_rate_log.jsonl"
)


def _rate_log(record: dict[str, Any]) -> None:
    """Append one event to _rate_log.jsonl. Fire-and-forget — never raise."""
    try:
        _RATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with _RATE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------- State dataclasses ----------

@dataclass
class FamilyState:
    """Bandit state pooled across every carrier of one model_family.

    A success on Cerebras's llama-3.3-70b counts the same as a success
    on Groq's llama-3.3-70b. Both are the same MODEL — only carrier
    differs. Pooling means we learn faster which models are reliable.
    """
    successes: int = 0
    failures: int = 0
    last_success_ts: float = 0.0


@dataclass
class SlotState:
    """Per-(provider, model) cooldown + blacklist state.

    Carrier-specific. A 429 on Groq's Llama cools just that slot; the
    same model at Cerebras keeps serving. Cooldowns expire automatically;
    blacklists last for the whole session.
    """
    cooldown_until: float = 0.0
    blacklisted: bool = False
    last_error: str = ""


class SchedulerError(RuntimeError):
    """Raised by pick_slot when no slot can service a role.

    Caller (orchestrator) treats this as a stage-level failure: shelf
    the task or trigger mid-run decomposition (depending on context).
    """


class ProviderError(RuntimeError):
    """Raised by _call_slot on any LLM-call-level failure.

    Message is prefixed with a short classifier the scheduler reads in
    `record_failure()`:
      "429:..."     real or upstream rate-limit
      "524:..."     upstream provider crashed
      "400/401/403/404:..." misconfig — won't self-heal
      "5xx:..."     transient server hiccup
      "http:..."    network error (timeout, connection reset)
      "json:..."    response wasn't JSON
      "shape:..."   JSON shape was unexpected
      "empty:..."   200 with empty content
      "refusal:..." model refused (penalized at family level)
    """


# ---------- The scheduler ----------

class SlotScheduler:
    """Picks slots for stages using score + rotation + cooldown rules.

    One instance per orchestrator run. Pass it into every stage call so
    cooldowns and family scores stay consistent across the pipeline.

    Thread-safety: not thread-safe, but the pipeline is asyncio
    single-threaded; mutations happen between awaits. The per-provider
    pacing lock is asyncio.Lock (not threading.Lock) — concurrent fanout
    shards targeting the same provider serialize through the rpm gap.
    """

    def __init__(self, slots: list[Slot]) -> None:
        self._slots: list[Slot] = list(slots)

        # Per-slot mutable state: cooldowns, blacklists.
        # Keyed by slot.id (a string like "openrouter/openai/gpt-oss-120b:free")
        # rather than the Slot object itself — Slot contains a Provider with
        # a dict (extra_headers), so Slot is not hashable. The id-string is
        # unique by construction (provider names are unique; model slugs are
        # unique within a provider).
        self._slot_state: dict[str, SlotState] = {s.id: SlotState() for s in slots}

        # Per-family bandit state, keyed by model_family string.
        families = {s.model_family for s in slots}
        self._family_state: dict[str, FamilyState] = {
            f: FamilyState() for f in families
        }

        # Per-provider pacing.
        self._provider_last_call: dict[str, float] = {}
        self._provider_locks: dict[str, asyncio.Lock] = {}

    # ---- score helpers ----

    def _recency_mult(self, family: str) -> float:
        """Recency multiplier for family score.

        Fresh wins (within RECENCY_FRESH_S) → 1.0×.
        Linear decay to RECENCY_FLOOR over RECENCY_DECAY_S.
        Never-succeeded families get RECENCY_NEW (0.9×) so they aren't
        instantly dominated by families with even one lucky win.
        """
        st = self._family_state[family]
        if st.last_success_ts == 0.0:
            return RECENCY_NEW
        age = time.time() - st.last_success_ts
        if age <= RECENCY_FRESH_S:
            return 1.0
        if age >= RECENCY_DECAY_S:
            return RECENCY_FLOOR
        span = RECENCY_DECAY_S - RECENCY_FRESH_S
        frac = (age - RECENCY_FRESH_S) / span
        return 1.0 - frac * (1.0 - RECENCY_FLOOR)

    def _family_score(self, family: str) -> float:
        """Combined family score — Laplace-smoothed × recency.

        Worked example: family with 4 successes, 1 failure, success 30s ago:
          base = (4+1)/(4+1+2) = 5/7 ≈ 0.714
          recency = 1.0
          score ≈ 0.714

        Never-used family:
          base = 1/2 = 0.5, recency = 0.9 → 0.45

        Flaky family (1 success, 4 failures, last success 40 min ago):
          base = 2/7 ≈ 0.286, recency = 0.7 → 0.200
        """
        st = self._family_state[family]
        base = (st.successes + 1) / (st.successes + st.failures + 2)
        return base * self._recency_mult(family)

    # ---- eligibility & picking ----

    def _eligible(
        self, slot: Slot, role: Role, exclude: set[str], now: float,
    ) -> bool:
        """Filter check: slot is currently usable for this role."""
        if role not in slot.roles:
            return False
        st = self._slot_state[slot.id]
        if st.blacklisted:
            return False
        if st.cooldown_until > now:
            return False
        if slot.provider.name in exclude:
            return False
        return True

    def _carriers_for(
        self, family: str, role: Role, exclude: set[str], now: float,
    ) -> list[Slot]:
        """All eligible slots of one family, for this role, right now."""
        return [
            s for s in self._slots
            if s.model_family == family
            and self._eligible(s, role, exclude, now)
        ]

    def _pick_freshest(self, carriers: list[Slot]) -> Slot:
        """Among equally-family-ranked carriers, prefer the longest-idle.

        Implements the "spread across carriers" rule: if Cerebras called
        0.5s ago and Groq called 20s ago, Groq wins. With per-provider
        pacing locks elsewhere, this naturally cycles calls between
        carriers of the same family rather than hammering one.
        """
        return max(
            carriers,
            key=lambda s: time.time() - self._provider_last_call.get(
                s.provider.name, 0.0
            ),
        )

    def pick_slot(
        self,
        role: Role,
        exclude_providers: set[str] | str | None = None,
    ) -> Slot:
        """Pick the best slot for this role.

        `exclude_providers` accepts:
          - None       no exclusion (e.g. first stage of a run)
          - a string   single provider to exclude (cross-stage rotation)
          - a set      multiple providers to exclude (fanout: every
                       currently-running shard's provider)

        Two-pass ranking:
          Pass 1 — Rank model FAMILIES by pooled score. Picks the best
                   model first (we want llama-3.3-70b before gemma-3-27b
                   if Llama has a better track record).
          Pass 2 — For the top family, pick the freshest CARRIER. This
                   is where carrier-spreading happens: among Cerebras /
                   Groq / Cloudflare all serving llama-3.3-70b, the
                   one whose pacing has been idle longest wins.

        Fallbacks:
          - Top family has no eligible carrier → drop to next family.
          - All families exhausted under the exclusion → relax exclusion
            ONCE, retry. Logged as `rotation_relaxed`.
          - Still nothing → raise SchedulerError.
        """
        # Normalize exclude argument.
        if exclude_providers is None:
            exclude: set[str] = set()
        elif isinstance(exclude_providers, str):
            exclude = {exclude_providers}
        else:
            exclude = set(exclude_providers)

        now = time.time()

        # Candidate families: those with at least one role-eligible,
        # non-blacklisted slot. Don't bother ranking dead families.
        candidate_families = {
            s.model_family for s in self._slots
            if role in s.roles and not self._slot_state[s.id].blacklisted
        }
        if not candidate_families:
            raise SchedulerError(
                f"role={role.value}: no non-blacklisted slots declare "
                f"eligibility for this role"
            )

        ranked = sorted(
            candidate_families,
            key=lambda f: self._family_score(f),
            reverse=True,
        )

        # Pass 1: honor exclude_providers.
        for family in ranked:
            carriers = self._carriers_for(family, role, exclude, now)
            if carriers:
                return self._pick_freshest(carriers)

        # Pass 2: relax exclude_providers (as last resort, log it).
        if exclude:
            for family in ranked:
                carriers = self._carriers_for(family, role, set(), now)
                if carriers:
                    log_event("rotation_relaxed",
                              role=role.value, family=family,
                              excluded=sorted(exclude))
                    return self._pick_freshest(carriers)

        # Truly nothing available. Diagnose for the error message.
        cooling = sum(
            1 for s in self._slot_state.values()
            if s.cooldown_until > now
        )
        blacklisted = sum(
            1 for s in self._slot_state.values() if s.blacklisted
        )
        raise SchedulerError(
            f"role={role.value}: no eligible carriers ("
            f"{cooling} cooling, {blacklisted} blacklisted, "
            f"{len(self._slots)} total)"
        )

    # ---- outcome recording ----

    def record_success(self, slot: Slot) -> None:
        """Tell the scheduler a call on this slot returned usable content.

        Updates BOTH state tables: family bandit credit, slot cooldown
        clear. Successes don't reset failure counts (the bandit needs
        long-term ratios), only the most recent transient cooldown.
        """
        fs = self._family_state[slot.model_family]
        fs.successes += 1
        fs.last_success_ts = time.time()

        st = self._slot_state[slot.id]
        st.cooldown_until = 0.0
        st.last_error = ""

    def record_failure(
        self, slot: Slot, code: str, reason: str = "",
    ) -> None:
        """Tell the scheduler a call on this slot failed.

        `code` is the short classifier from ProviderError. The split:
          MODEL-quality codes (json, shape, empty, refusal):
            penalize FAMILY score AND mark slot's last_error.
          CARRIER codes (429, 5xx, http):
            cooldown the slot only; family unchanged.
          PERMANENT codes (524, 400, 401, 403, 404):
            blacklist the slot only; family unchanged.

        This is the key invariant — provider-side failures never
        penalize the model itself.
        """
        st = self._slot_state[slot.id]
        st.last_error = f"{code}:{reason}"[:200]

        if code in ("json", "shape", "empty", "refusal"):
            self._family_state[slot.model_family].failures += 1
            log_event("family_penalty",
                      slot=slot.id, family=slot.model_family,
                      code=code, reason=reason[:160])
        elif code == "429":
            st.cooldown_until = time.time() + COOLDOWN_429_S
            log_event("cooldown_set",
                      slot=slot.id, code=code,
                      cooldown_s=COOLDOWN_429_S, reason=reason[:160])
        elif code in ("524", "400", "401", "403", "404"):
            st.blacklisted = True
            log_event("slot_blacklisted",
                      slot=slot.id, code=code, reason=reason[:160])
        else:
            # 5xx, http, anything else → transient cooldown.
            st.cooldown_until = time.time() + COOLDOWN_TRANSIENT_S
            log_event("cooldown_set",
                      slot=slot.id, code=code,
                      cooldown_s=COOLDOWN_TRANSIENT_S, reason=reason[:160])

    # ---- pacing ----

    async def wait_for_provider_pacing(self, slot: Slot) -> None:
        """Enforce min_gap = 60/rpm between calls to the same provider.

        Per-provider asyncio lock so concurrent stage tasks targeting the
        same provider serialize through the gap rather than firing
        simultaneously. Updates last_call_ts atomically inside the lock.
        """
        name = slot.provider.name
        lock = self._provider_locks.setdefault(name, asyncio.Lock())
        min_gap = 60.0 / max(1, slot.provider.rpm)
        async with lock:
            last = self._provider_last_call.get(name, 0.0)
            now = time.time()
            gap = now - last
            if gap < min_gap:
                sleep_s = min_gap - gap
                log_event("pacing_wait",
                          provider=name, slot=slot.id,
                          sleep_s=round(sleep_s, 2), rpm=slot.provider.rpm)
                await asyncio.sleep(sleep_s)
            self._provider_last_call[name] = time.time()

    # ---- introspection ----

    def earliest_wake_ts(self) -> float:
        """Earliest cooldown_until across non-blacklisted slots, or 0.

        Used by callers that want to sleep until any slot becomes
        eligible (rather than busy-looping on pick_slot).
        """
        cooling = [
            st.cooldown_until for st in self._slot_state.values()
            if not st.blacklisted and st.cooldown_until > 0.0
        ]
        return min(cooling) if cooling else 0.0

    def snapshot(self) -> list[dict]:
        """Status table per slot, sorted by family score descending."""
        now = time.time()
        rows = []
        for slot in self._slots:
            st = self._slot_state[slot.id]
            fs = self._family_state[slot.model_family]
            rows.append({
                "slot": slot.id,
                "family": slot.model_family,
                "roles": [r.value for r in slot.roles],
                "family_successes": fs.successes,
                "family_failures": fs.failures,
                "family_score": round(self._family_score(slot.model_family), 3),
                "cooling_s": max(0.0, round(st.cooldown_until - now, 1)),
                "blacklisted": st.blacklisted,
                "last_error": st.last_error,
            })
        rows.sort(key=lambda r: r["family_score"], reverse=True)
        return rows


# ---------- _call_slot: the single HTTP call helper ----------

async def call_slot(
    client: httpx.AsyncClient,
    slot: Slot,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    timeout_s: float = 600.0,
    response_format: dict | None = None,
) -> str:
    """Make one OpenAI-compat chat-completion call against a slot.

    Works for any provider in providers.py — they all expose the same
    OpenAI-compat shape, only base_url and headers differ.

    Classifies failures via ProviderError prefix so the scheduler's
    record_failure() can route to family vs slot state correctly:
      "429"     real HTTP 429 OR upstream-200-with-error-code-429
      "524"     upstream provider crashed
      "400-404" config errors
      "5xx"     other server errors
      "http"    network error
      "json"    body wasn't JSON
      "shape"   JSON shape unexpected
      "empty"   200 with empty content
      "refusal" model returned a refusal phrase

    `response_format` is forwarded to providers that support it (OpenAI,
    Gemini's compat shim, Groq, Cerebras). Providers that ignore it just
    return regular text and the caller falls back to prompt-only JSON
    coercion. Pass `{"type": "json_object"}` or `{"type": "json_schema",
    "json_schema": {...}}` to enable.
    """
    p = slot.provider
    headers = {
        "Authorization": f"Bearer {p.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(p.extra_headers)

    body: dict[str, Any] = {
        "model": slot.model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        body["response_format"] = response_format

    # Common base fields for every log_event emitted from this call.
    _base = dict(provider=p.name, model=slot.model, family=slot.model_family,
                 slot=slot.id)

    t_call = time.monotonic()
    status: int | None = None
    try:
        r = await client.post(p.base_url, headers=headers, json=body, timeout=timeout_s)
        status = r.status_code
        duration = round(time.monotonic() - t_call, 2)

        if status == 429:
            log_event("call_fail", **_base, http_status=429,
                      fail_code="429", reason="HTTP 429", duration_s=duration)
            raise ProviderError(f"429:HTTP 429 from {slot.id}")

        if status in (400, 401, 403, 404):
            msg = f"HTTP {status}: {r.text[:160]}"
            log_event("call_fail", **_base, http_status=status,
                      fail_code=str(status), reason=msg, duration_s=duration)
            raise ProviderError(f"{status}:{msg}")

        if status is not None and status >= 500:
            msg = f"HTTP {status}: {r.text[:160]}"
            code = "524" if status == 524 else "5xx"
            log_event("call_fail", **_base, http_status=status,
                      fail_code=code, reason=msg, duration_s=duration)
            raise ProviderError(f"{code}:{msg}")

        r.raise_for_status()

        try:
            data = r.json()
        except ValueError as e:
            log_event("call_fail", **_base, http_status=status,
                      fail_code="json", reason=f"non-json: {e!r}", duration_s=duration)
            raise ProviderError(f"json:{e!r}")

        # OpenRouter quirk: upstream errors arrive as HTTP 200 with
        # {"error": {"code": ..., "message": ...}} body.
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            err_code = str(err.get("code", "unknown"))
            msg = err.get("message", "")[:200]
            log_event("call_fail", **_base, http_status=status,
                      fail_code=err_code, upstream_code=err_code,
                      reason=f"upstream {err_code}: {msg}", duration_s=duration)
            raise ProviderError(f"{err_code}:upstream {err_code}: {msg}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            msg = f"bad shape: {e!r}; body={str(data)[:200]}"
            log_event("call_fail", **_base, http_status=status,
                      fail_code="shape", reason=msg[:300], duration_s=duration)
            raise ProviderError(f"shape:{msg}")

        if not content or not content.strip():
            log_event("call_fail", **_base, http_status=status,
                      fail_code="empty", reason="empty content", duration_s=duration)
            raise ProviderError(f"empty:empty content from {slot.id}")

        head = content.strip()[:200]
        if _REFUSAL_RE.match(head):
            log_event("call_fail", **_base, http_status=status,
                      fail_code="refusal", reason=head[:120], duration_s=duration)
            raise ProviderError(f"refusal:{head[:120]!r}")

        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        log_event("call_ok", **_base, http_status=status, duration_s=duration,
                  in_tokens=usage.get("prompt_tokens"),
                  out_tokens=usage.get("completion_tokens"),
                  out_chars=len(content))
        return content

    except httpx.HTTPError as e:
        duration = round(time.monotonic() - t_call, 2)
        log_event("call_fail", **_base, http_status=status,
                  fail_code="http", reason=repr(e)[:300], duration_s=duration)
        raise ProviderError(f"http:{e!r}")
