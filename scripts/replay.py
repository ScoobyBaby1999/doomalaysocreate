#!/usr/bin/env python3
"""
replay.py - Hippocampal-replay-style multi-pass research loop.

Stages:
  draft    - write from prompt + exemplar + honesty rules
  validate - structural + epistemic checks (validator.py)
  critique - terse issue list from draft + validator report
  revise   - apply critique, return full revised body
  validate - re-check; loop up to max_rounds

# Phase 0 change (2026-04-21)

Previously this module hardcoded OpenRouter and kept chain-rotation logic
(_try_one_model, _openrouter_call) inline. That made it impossible to add
other providers without rewriting the call path.

The Phase 0 refactor splits responsibilities:
  - providers.py owns the registry (who, what, where)
  - scheduler.py owns the picking logic (which slot next, when to cool down)
  - replay.py owns the *stages* (draft/critique/revise) and the HTTP call
    that turns a slot + messages into content

`_call_slot(slot, messages)` is the single function that actually hits an
HTTP endpoint. It works for any OpenAI-compatible provider (all 8 in our
inventory) because every provider has the same wire shape:
  POST {base_url} with {"model": X, "messages": [...]} and Bearer auth.

`_pick_and_call(scheduler, stage, exclude_provider, messages)` wraps the
scheduler loop: pick a slot, pace by provider rpm, call it, and on failure
classify the error, record it with the scheduler, and pick the next slot.

Phase 0's registry only contains OpenRouter, so in practice the scheduler
picks the same slot every time and behavior is identical to the pre-refactor
pipeline. Phase 1+ adds real rotation by registering more providers.

# Cross-stage rotation

`run_replay` tracks `providers_used` (the provider name per stage) and
passes the previous stage's provider as `exclude_provider` to the next
stage's pick. That's the "1 LLM provider does 1 task only; and it bounces
around" rule, enforced at the scheduler layer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from providers import Slot
from scheduler import SchedulerError, SlotScheduler
from validator import ValidatorReport, validate

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_SYS_DIR = Path(__file__).resolve().parent / "prompts"
EXEMPLAR_PATH = ROOT / "vault" / "research" / "02_brain_memory_parallels.md"
RATE_LOG_PATH = ROOT / "vault" / "research" / "_rate_log.jsonl"


def _rate_log(record: dict[str, Any]) -> None:
    """Append a single call event to _rate_log.jsonl. Fire-and-forget — never
    raise, because we don't want logging to break the pipeline."""
    try:
        RATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with RATE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---- Result structures ----

@dataclass
class StageResult:
    stage: str
    slot_id: str           # e.g. "openrouter/openai/gpt-oss-120b:free"
    provider: str          # just the provider name, for quick rotation checks
    model: str
    duration_s: float
    ok: bool
    content: str
    error: str | None = None


@dataclass
class ReplayResult:
    stem: str
    ok: bool
    rounds: int
    final_body: str | None
    stages: list[StageResult] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)
    final_report: ValidatorReport | None = None
    error: str | None = None


# ---- Call primitives ----

class ProviderError(RuntimeError):
    """Raised by _call_slot on any call-level failure. The message is prefixed
    with a short classifier ('429:', '524:', 'http:', 'json:', 'shape:',
    'empty:') that the scheduler uses to decide cooldown vs blacklist."""


async def _call_slot(
    client: httpx.AsyncClient,
    slot: Slot,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    timeout_s: float,
) -> str:
    """Single HTTP call against one (provider, model) slot.

    Works for any OpenAI-compatible chat-completions endpoint (OpenRouter,
    Cerebras, Groq, Gemini's OpenAI shim, Ollama Cloud, Cloudflare Workers AI,
    Vercel AI Gateway). Returns the assistant message content string.

    Classifies failures by prefixing the raised message. The scheduler parses
    this prefix in `record_failure()`:
      "429:..."  → rate limit, cool 5 min
      "524:..."  → upstream crashed, session blacklist
      "400/401/404:..." → client-side config error, session blacklist
      "5xx:..."  → transient, cool 1 min
      "http:..." → network error (timeout, reset), cool 1 min
      "json:..." → non-JSON body, cool 1 min
      "shape:..."→ JSON shape surprise, cool 1 min
      "empty:...→ 200 with empty content, cool 1 min
    """
    p = slot.provider
    headers = {
        "Authorization": f"Bearer {p.api_key}",
        "Content-Type": "application/json",
    }
    headers.update(p.extra_headers)

    body = {"model": slot.model, "messages": messages, "max_tokens": max_tokens}

    t_call = time.monotonic()
    status: int | None = None
    try:
        r = await client.post(p.base_url, headers=headers, json=body, timeout=timeout_s)
        status = r.status_code
        duration = round(time.monotonic() - t_call, 2)

        # Real HTTP 429 (some providers use this directly).
        if status == 429:
            _rate_log({"provider": p.name, "model": slot.model, "status": 429,
                       "outcome": "rotate", "reason": "HTTP 429", "duration_s": duration})
            raise ProviderError(f"429:HTTP 429 from {slot.id}")

        # Config errors are permanent for this session.
        if status in (400, 401, 403, 404):
            msg = f"HTTP {status}"
            _rate_log({"provider": p.name, "model": slot.model, "status": status,
                       "outcome": "blacklist", "reason": msg, "duration_s": duration})
            raise ProviderError(f"{status}:{msg}")

        if status is not None and status >= 500:
            msg = f"HTTP {status}"
            _rate_log({"provider": p.name, "model": slot.model, "status": status,
                       "outcome": "rotate", "reason": msg, "duration_s": duration})
            # Map any 5xx other than 524 to a transient cooldown; 524 is
            # OpenRouter's "provider upstream crashed" and warrants a blacklist.
            code = "524" if status == 524 else "5xx"
            raise ProviderError(f"{code}:{msg}")

        r.raise_for_status()

        try:
            data = r.json()
        except ValueError as e:
            _rate_log({"provider": p.name, "model": slot.model, "status": status,
                       "outcome": "rotate", "reason": f"non-json: {e!r}",
                       "duration_s": duration})
            raise ProviderError(f"json:{e!r}")

        # OpenRouter quirk: upstream errors come back as HTTP 200 with
        # {"error": {"code": 429|524|..., ...}} in the body. Other providers
        # don't do this but the check is harmless for them.
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            err_code = str(err.get("code", ""))
            msg = err.get("message", "")
            meta = err.get("metadata", {})
            raw = (meta.get("raw", "") if isinstance(meta, dict) else "")[:100]
            full = f"upstream {err_code}: {msg} {raw}".strip()
            _rate_log({
                "provider": p.name, "model": slot.model, "status": status,
                "upstream_code": err_code, "upstream_msg": msg[:200],
                "outcome": "rotate", "reason": full[:300], "duration_s": duration,
            })
            raise ProviderError(f"{err_code}:{full}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            msg = f"bad shape: {e!r}; body={str(data)[:200]}"
            _rate_log({"provider": p.name, "model": slot.model, "status": status,
                       "outcome": "rotate", "reason": msg[:300], "duration_s": duration})
            raise ProviderError(f"shape:{msg}")

        if not content or not content.strip():
            _rate_log({"provider": p.name, "model": slot.model, "status": status,
                       "outcome": "rotate", "reason": "empty content",
                       "duration_s": duration})
            raise ProviderError(f"empty:empty content from {slot.id}")

        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        _rate_log({
            "provider": p.name, "model": slot.model, "status": status,
            "outcome": "ok", "duration_s": duration,
            "out_tokens": usage.get("completion_tokens"),
            "in_tokens": usage.get("prompt_tokens"),
        })
        return content

    except httpx.HTTPError as e:
        duration = round(time.monotonic() - t_call, 2)
        _rate_log({"provider": p.name, "model": slot.model, "status": status,
                   "outcome": "rotate", "reason": repr(e)[:300],
                   "duration_s": duration})
        raise ProviderError(f"http:{e!r}")


async def _pick_and_call(
    client: httpx.AsyncClient,
    scheduler: SlotScheduler,
    stage: str,
    exclude_provider: str | None,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    timeout_s: float = 600.0,
) -> tuple[str, Slot]:
    """Scheduler-driven call loop.

    Repeats:
      pick a slot → pace by provider rpm → call it.
    On ProviderError: classify, tell the scheduler, loop again. The scheduler
    will hide the failed slot (cooldown or blacklist) so the next pick is
    a different one.

    If the scheduler runs out of candidates it raises SchedulerError; we
    propagate it so the stage runner can surface it as a stage failure.

    Returns (content, slot_used) so the caller can record which slot serviced
    this stage (→ StageResult, → providers_used, → cross-stage rotation).
    """
    while True:
        slot = scheduler.pick_slot(stage, exclude_provider=exclude_provider)
        await scheduler.wait_for_provider_pacing(slot)
        try:
            content = await _call_slot(
                client, slot, messages,
                max_tokens=max_tokens, timeout_s=timeout_s,
            )
            scheduler.record_success(slot)
            return content, slot
        except ProviderError as exc:
            err = str(exc)
            code = err.split(":", 1)[0]
            scheduler.record_failure(slot, code, reason=err[:200])
            # Loop: scheduler will now filter this slot out via cooldown/blacklist.


# ---- Helpers ----

def _load_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_prompt_toc(prompt_md: str) -> list[str]:
    """Pull the numbered topic list out of a research prompt."""
    toc = re.findall(r"^\s*\d+\.\s+\*\*([^*]+?)\*\*", prompt_md, re.MULTILINE)
    if toc:
        return [t.strip().rstrip(".") for t in toc]
    return re.findall(r"^###?\s+(.+)$", prompt_md, re.MULTILINE)


def make_frontmatter(stem: str, stages: list[StageResult]) -> str:
    """Build YAML frontmatter. Credits provider/model per stage."""
    today = time.strftime("%Y-%m-%d")
    credit_lines = [
        f"- replay pipeline ({s.stage}: {s.provider}/{s.model})"
        for s in stages if s.ok
    ]
    credits_block = "\n".join(credit_lines) if credit_lines else "- replay pipeline"
    return (
        "---\n"
        f"keywords:\n- research\n- {stem}\n"
        "status: active\n"
        f"created: '{today}'\n"
        f"updated: '{today}'\n"
        "credits:\n"
        f"{credits_block}\n"
        "links:\n  hard: []\n  soft: []\n"
        f"desc: \"Auto-generated research from {stem} (Replay loop).\"\n"
        "---\n\n"
    )


# ---- Stage runners ----
#
# Each stage runner does the same dance:
#   1. Load the system prompt for this stage from scripts/prompts/.
#   2. Build the user message from the caller's inputs.
#   3. Call _pick_and_call with the right (stage, exclude_provider).
#   4. Wrap result or failure in a StageResult.
#
# The `exclude_provider` argument is the *previous* stage's provider. Pass None
# for the draft stage (first call; no prior provider to exclude).

async def _stage_draft(
    client: httpx.AsyncClient,
    scheduler: SlotScheduler,
    prompt_body: str,
    exemplar: str,
) -> StageResult:
    sys_tmpl = _load_text(PROMPTS_SYS_DIR / "system_draft.md")
    system = sys_tmpl.replace("{{EXEMPLAR}}", exemplar)
    t0 = time.monotonic()
    try:
        content, slot = await _pick_and_call(
            client, scheduler, "draft", exclude_provider=None,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt_body},
            ],
            max_tokens=16000,
        )
        return StageResult(
            stage="draft", slot_id=slot.id, provider=slot.provider.name,
            model=slot.model, duration_s=time.monotonic() - t0,
            ok=True, content=content,
        )
    except (SchedulerError, Exception) as e:  # noqa: BLE001
        return StageResult(
            stage="draft", slot_id="?", provider="?", model="?",
            duration_s=time.monotonic() - t0, ok=False, content="", error=repr(e),
        )


async def _stage_critique(
    client: httpx.AsyncClient,
    scheduler: SlotScheduler,
    draft: str,
    validator_feed: str,
    exclude_provider: str | None,
) -> StageResult:
    system = _load_text(PROMPTS_SYS_DIR / "system_critique.md")
    user = (
        "## Validator report\n\n"
        f"{validator_feed}\n\n"
        "## Draft\n\n"
        f"{draft}"
    )
    t0 = time.monotonic()
    try:
        content, slot = await _pick_and_call(
            client, scheduler, "critique", exclude_provider=exclude_provider,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=4000,
        )
        return StageResult(
            stage="critique", slot_id=slot.id, provider=slot.provider.name,
            model=slot.model, duration_s=time.monotonic() - t0,
            ok=True, content=content,
        )
    except (SchedulerError, Exception) as e:  # noqa: BLE001
        return StageResult(
            stage="critique", slot_id="?", provider="?", model="?",
            duration_s=time.monotonic() - t0, ok=False, content="", error=repr(e),
        )


async def _stage_revise(
    client: httpx.AsyncClient,
    scheduler: SlotScheduler,
    prompt_body: str,
    draft: str,
    critique: str,
    exclude_provider: str | None,
) -> StageResult:
    system = _load_text(PROMPTS_SYS_DIR / "system_revise.md")
    user = (
        "## Original research prompt\n\n"
        f"{prompt_body}\n\n"
        "## Current draft\n\n"
        f"{draft}\n\n"
        "## Critique to apply\n\n"
        f"{critique}"
    )
    t0 = time.monotonic()
    try:
        content, slot = await _pick_and_call(
            client, scheduler, "revise", exclude_provider=exclude_provider,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=16000,
        )
        return StageResult(
            stage="revise", slot_id=slot.id, provider=slot.provider.name,
            model=slot.model, duration_s=time.monotonic() - t0,
            ok=True, content=content,
        )
    except (SchedulerError, Exception) as e:  # noqa: BLE001
        return StageResult(
            stage="revise", slot_id="?", provider="?", model="?",
            duration_s=time.monotonic() - t0, ok=False, content="", error=repr(e),
        )


# ---- Public entry point ----

async def run_replay(
    client: httpx.AsyncClient,
    prompt_path: Path,
    scheduler: SlotScheduler,
    *,
    max_rounds: int = 3,
    check_urls: bool = True,
    dry: bool = False,
    log: Callable[[str], None] = print,
) -> ReplayResult:
    """Run draft→critique→revise for one prompt.

    `scheduler` is shared across all calls in this run (so cooldowns and
    scores are consistent). The conductor builds one SlotScheduler at
    startup and passes it into every run_replay() call.
    """
    stem = prompt_path.stem
    prompt_body = _load_text(prompt_path)
    toc = _extract_prompt_toc(prompt_body)
    exemplar = _load_text(EXEMPLAR_PATH)

    result = ReplayResult(stem=stem, ok=False, rounds=0, final_body=None)

    if dry:
        log(f"[dry] {stem}: would pick slots from scheduler for draft/"
            f"critique/revise; prompt={len(prompt_body)} chars, "
            f"exemplar={len(exemplar)} chars, toc={len(toc)} topics")
        result.ok = True
        return result

    # --- Round 0: initial draft ---
    log(f"[{stem}] drafting")
    draft_stage = await _stage_draft(client, scheduler, prompt_body, exemplar)
    result.stages.append(draft_stage)
    if not draft_stage.ok:
        result.error = f"draft failed: {draft_stage.error}"
        return result
    result.providers_used.append(draft_stage.provider)
    log(f"[{stem}] drafted via {draft_stage.slot_id}")
    current = draft_stage.content
    prev_provider = draft_stage.provider

    # --- Validate + critique + revise loop ---
    for round_idx in range(1, max_rounds + 1):
        result.rounds = round_idx
        log(f"[{stem}] validating (round {round_idx})")
        report = await validate(current, toc, check_urls=check_urls)
        result.final_report = report
        if report.ok and not report.soft_flags:
            log(f"[{stem}] validator clean on round {round_idx}")
            break
        if report.ok and round_idx > 1:
            log(f"[{stem}] no hard fails; stopping refinement")
            break

        log(f"[{stem}] critiquing "
            f"(hard={len(report.hard_fails)} soft={len(report.soft_flags)}, "
            f"excluding provider={prev_provider})")
        crit_stage = await _stage_critique(
            client, scheduler, current, report.to_critique_feed(),
            exclude_provider=prev_provider,
        )
        result.stages.append(crit_stage)
        if not crit_stage.ok:
            result.error = f"critique failed round {round_idx}: {crit_stage.error}"
            return result
        result.providers_used.append(crit_stage.provider)
        log(f"[{stem}] critiqued via {crit_stage.slot_id}")
        prev_provider = crit_stage.provider

        log(f"[{stem}] revising (excluding provider={prev_provider})")
        rev_stage = await _stage_revise(
            client, scheduler, prompt_body, current, crit_stage.content,
            exclude_provider=prev_provider,
        )
        result.stages.append(rev_stage)
        if not rev_stage.ok:
            result.error = f"revise failed round {round_idx}: {rev_stage.error}"
            return result
        result.providers_used.append(rev_stage.provider)
        log(f"[{stem}] revised via {rev_stage.slot_id}")
        prev_provider = rev_stage.provider
        current = rev_stage.content

    # Final validation pass.
    final_report = await validate(current, toc, check_urls=check_urls)
    result.final_report = final_report
    result.final_body = current
    result.ok = final_report.ok
    if not result.ok:
        result.error = (
            f"still has hard fails after {result.rounds} rounds: "
            + "; ".join(final_report.hard_fails[:3])
        )
    return result
