"""
judge/llm_judge.py - Layer 3: LLM-as-judge.

Each natural-language criterion in `judge_config.llm_judges` becomes
one scheduler call with role=VERIFIER. The verifier skeleton in
prompts/role_verifier.md forces strict {"pass": bool, "reason": str}
JSON output.

LLM verdicts produce SOFT flags only. Fuzzy yes/no judgments shouldn't
single-handedly reject a draft, but accumulated soft flags still
trigger revise rounds via the orchestrator's loop logic.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

import httpx

from orchestrator.roles import Role, render_skeleton
from scheduler import ProviderError, call_slot

from .report import JudgeReport

if TYPE_CHECKING:
    from scheduler import SlotScheduler


# Token budget per verifier call. Verifier outputs are tiny — one JSON
# object — so this stays small. Cost-conscious for runs with many
# llm_judges criteria.
_VERIFIER_MAX_TOKENS = 200


async def run_llm_judges(
    body: str,
    criteria: list[str],
    scheduler: "SlotScheduler",
    http_client: httpx.AsyncClient,
    exclude_provider: str | None = None,
    metrics=None,
    profile: str = "default",
) -> JudgeReport:
    """Run each criterion as one VERIFIER call. Parallel via gather.

    Each verifier call:
      1. scheduler.pick_slot(role=VERIFIER, exclude_provider=...) —
         routes to a small precision model on a different provider
         than the body's author.
      2. Renders the verifier skeleton with the criterion as
         {{INSTRUCTIONS}} and the body as {{INPUTS_RENDERED}}.
      3. Awaits call_slot(); enforces provider pacing.
      4. Parses the JSON {pass, reason}; soft-flag on fail.

    Errors at any step become soft flags — a flaky verifier shouldn't
    cause a generator's output to be rejected.
    """
    report = JudgeReport()
    if not criteria:
        return report

    # Build the exclusion set: don't ask the body's author to judge it.
    exclude: set[str] = {exclude_provider} if exclude_provider else set()

    async def _one(criterion: str) -> tuple[str, dict | None, str | None]:
        """Run one verifier call. Returns (criterion, parsed_or_none, error)."""
        try:
            slot = scheduler.pick_slot(
                role=Role.VERIFIER, exclude_providers=exclude,
            )
        except Exception as e:  # noqa: BLE001 - SchedulerError + others
            return (criterion, None, f"could not pick slot: {e!r}")

        await scheduler.wait_for_provider_pacing(slot)
        import time as _time
        _t0 = _time.monotonic()

        # Build messages. Verifier skeleton expects {{INSTRUCTIONS}} =
        # the criterion, {{INPUTS_RENDERED}} = the body to judge.
        system = render_skeleton(
            Role.VERIFIER,
            instructions=criterion,
            output_rules_rendered="(strict JSON output)",
            inputs_rendered=body,
        )
        # The user message just nudges the model toward the JSON form.
        # The skeleton already includes the strict format spec.
        user = (
            "Judge the criterion against the content above. "
            "Output ONLY the JSON object."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            content, usage = await call_slot(
                http_client, slot, messages,
                max_tokens=_VERIFIER_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            scheduler.record_success(slot)
            _emit_verifier_metric(metrics, profile, slot,
                                  round(_time.monotonic() - _t0, 2), usage, True, "ok")
        except ProviderError as e:
            err = str(e)
            code = err.split(":", 1)[0]
            scheduler.record_failure(slot, code, reason=err[:200])
            _emit_verifier_metric(metrics, profile, slot,
                                  round(_time.monotonic() - _t0, 2), {}, False, code)
            return (criterion, None, f"verifier call failed: {err[:120]}")

        parsed = _parse_verifier_output(content)
        if parsed is None:
            return (criterion, None, f"unparseable: {content[:120]!r}")
        return (criterion, parsed, None)

    # Fan-out: every criterion judged in parallel.
    results = await asyncio.gather(*(_one(c) for c in criteria))
    for criterion, parsed, error in results:
        # Truncated criterion for log messages.
        c_short = criterion if len(criterion) <= 80 else criterion[:77] + "..."

        if error is not None:
            report.soft_flags.append(f"[judge] {c_short}: {error}")
            continue
        assert parsed is not None
        if not parsed.get("pass", False):
            reason = parsed.get("reason", "(no reason)")
            report.soft_flags.append(f"[judge] {c_short}: {reason}")

    return report


def _emit_verifier_metric(metrics, profile, slot, latency_s, usage, ok, code) -> None:
    #   per-profile metric for each LLM-judge verifier call. best-effort.
    if metrics is None:
        return
    usage = usage or {}
    try:
        metrics.record(
            profile=profile, logical="__llm_judge__", provider=slot.provider.name,
            model=slot.model, family=slot.model_family, role="verifier",
            effort="judge", latency_s=latency_s,
            in_tokens=usage.get("prompt_tokens"), out_tokens=usage.get("completion_tokens"),
            ok=ok, code=code, attempts=1, routed_to=slot.who,
        )
    except Exception:  # noqa: BLE001
        pass


def _parse_verifier_output(content: str) -> dict | None:
    """Parse a verifier's output into {pass: bool, reason: str}.

    Tolerates ```json fences and prose-only outputs that mention
    "pass: true/false". Returns None if both strategies fail.
    """
    text = content.strip()

    # Strip fence if present.
    if text.startswith("```"):
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()

    # Strict JSON path.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "pass" in parsed:
            return {
                "pass": bool(parsed["pass"]),
                "reason": str(parsed.get("reason", "")),
            }
    except json.JSONDecodeError:
        pass

    # Lenient prose fallback: look for "pass: true|false|yes|no".
    m = re.search(
        r'pass["\']?\s*[:=]\s*(true|false|yes|no)',
        text, re.IGNORECASE,
    )
    if m is not None:
        return {
            "pass": m.group(1).lower() in ("true", "yes"),
            "reason": text[:200],
        }

    return None
