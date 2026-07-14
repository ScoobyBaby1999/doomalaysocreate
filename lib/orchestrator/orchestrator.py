"""
orchestrator.py - The stage loop driver.

# What this module does

Given a TaskSchematic and a SlotScheduler, executes the schematic's
stages in order, applying:

  - Cross-stage rotation (each stage excludes the previous stage's
    provider via scheduler.pick_slot(role, exclude_providers=...))
  - Fanout (a stage with FanoutSpec runs once per item in `over`,
    in batches of max_parallel; concurrent shards exclude each
    other's providers)
  - Per-role merge logic (roles.merge() routes output into context)
  - Post-stage Judge calls (after generator/transformer stages produce
    a body, run the 3-layer Judge; on hard fails, inject reviewer +
    transformer pair onto the queue)
  - Mid-run decomposition (after 2 consecutive failures with overload
    signals, replace the failing stage with a freshly-planned sub-pipeline)

# What this module does NOT do

- Read prompt files          (runner.py)
- Call the Planner            (planner.py)
- Implement Judge layers       (judge package)
- Manage state across runs    (runner.py)
- Touch the filesystem         (runner.py + planner.py write outputs)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

import judge
from judge.report import JudgeReport
from oplog import log_event
from scheduler import ProviderError, SchedulerError, SlotScheduler, call_slot

from .roles import (
    Role,
    RoleMergeError,
    merge as merge_role_output,
    render_inputs,
    render_output_rules,
    render_skeleton,
)
from .schematic import StageDef, TaskSchematic


import os

# Per-stage call attempts: each retry picks a different slot (the failed provider
# is excluded), so with a pool where several providers are throttled/broken we need
# enough hops to reach a working carrier. Configurable via STAGE_CALL_ATTEMPTS.
STAGE_CALL_ATTEMPTS = int(os.environ.get("STAGE_CALL_ATTEMPTS", "6"))

# How many times a single stage can fail (call-level OR judge-level)
# before we attempt mid-run decomposition. Keep small — repeated failure
# means we're banging on a fundamentally wrong-sized task.
STAGE_FAILURE_THRESHOLD = 2

# Mid-run decomposition: each branch gets at most this much depth. 1 means
# a top-level stage may decompose once into sub-stages, but a sub-stage
# cannot decompose further (would risk infinite tree growth).
MAX_DECOMPOSITION_DEPTH = 1

# Default max_tokens by role when the StageDef doesn't specify. Upper bounds, not
# targets - thinking models spend part of the budget on reasoning traces, so these
# sit at playground-grade levels; the per-provider limits.max_out clamp in call_slot
# protects hosts with real per-request ceilings (e.g. GitHub Models).
_ROLE_DEFAULT_MAX_TOKENS = {
    Role.PLANNER: 16384,
    Role.GENERATOR: 16384,
    Role.REVIEWER: 8192,
    Role.TRANSFORMER: 16384,
    Role.EXTRACTOR: 8192,
    Role.VERIFIER: 1024,
}


# ---------- Result types ----------

@dataclass
class StageRecord:
    """One executed stage's outcome. Goes into RunResult.stages for telemetry."""
    name: str
    role: Role
    slot_id: str | None       # None if scheduling failed entirely
    provider: str | None
    family: str | None
    duration_s: float
    ok: bool
    shard_count: int = 1      # 1 for non-fanout stages
    error: str | None = None


@dataclass
class RunResult:
    """End-to-end run outcome for one TaskSchematic.

    Fields:
      ok            true iff judge passes and a body exists
      body          final content (possibly None if the run failed)
      stages        executed stage records (history; includes retries)
      providers_used  ordered list of providers used (for credits/log)
      rounds        how many judge-driven retry rounds fired
      decompositions how many mid-run decompositions fired
      final_report  last JudgeReport (None if the run never reached judge)
      error         human-readable terminal error, or None on success
    """
    ok: bool
    body: str | None
    stages: list[StageRecord] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)
    rounds: int = 0
    decompositions: int = 0
    final_report: JudgeReport | None = None
    error: str | None = None
    #   multi-file artifacts emitted by the final body-producer (path -> content),
    #   populated when the schematic's output_rules.format == "files".
    artifacts: list[dict] = field(default_factory=list)


# ---------- Public entry point ----------

async def execute(
    schematic: TaskSchematic,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    *,
    initial_context: dict[str, Any] | None = None,
    log: Callable[[str], None] = print,
    metrics: Any = None,
    profile: str = "default",
    effort: str = "med",
    nonce: str = "",
) -> RunResult:
    """Run a TaskSchematic to completion.

    `initial_context` seeds the run context with whatever the caller
    wants (typically `{"prompt": <raw user prompt>}`). The Orchestrator
    populates additional keys as stages run: `body` for the latest
    generator/transformer output, `<stage.name>` for extractor/planner
    structured outputs, etc.

    `metrics`/`profile`/`effort` feed the per-profile MetricStore (each stage
    call is one event). `nonce` is the artifact-protocol token spliced into
    file-producing stages. All are stashed under reserved `__*__` context keys
    (never rendered into prompts) so the existing stage signatures stay intact.

    Returns RunResult unconditionally — never raises. Failures are
    captured in result.error and result.ok.
    """
    context: dict[str, Any] = dict(initial_context or {})
    context["__metrics__"] = metrics
    context["__profile__"] = profile
    context["__effort__"] = effort
    context["__nonce__"] = nonce
    result = RunResult(ok=False, body=None)

    # The stage queue. Mutable — judge retries inject (reviewer,
    # transformer) pairs; mid-run decomposition splices sub-stages
    # in place of the failing stage.
    queue: list[StageDef] = list(schematic.stages)

    # Per-stage failure counter (for mid-run decomposition trigger).
    # Keyed by stage.name; reset on success.
    stage_failures: dict[str, int] = {}
    # Per-stage decomposition counter (cap at MAX_DECOMPOSITION_DEPTH).
    decomp_depth: dict[str, int] = {}

    prev_provider: str | None = None
    judge_round = 0
    idx = 0

    while idx < len(queue):
        stage = queue[idx]

        log_event("stage_start",
                  stage=stage.name, role=stage.role.value,
                  fanout=stage.fanout is not None,
                  fanout_over=stage.fanout.over if stage.fanout else None,
                  exclude_provider=prev_provider)
        try:
            stage_records = await _execute_stage(
                stage=stage,
                schematic=schematic,
                scheduler=scheduler,
                http_client=http_client,
                context=context,
                exclude_provider=prev_provider,
                log=log,
            )
        except StageFailure as e:
            stage_failures[stage.name] = stage_failures.get(stage.name, 0) + 1
            log_event("stage_fail",
                      stage=stage.name, role=stage.role.value,
                      failure_count=stage_failures[stage.name],
                      reason=str(e)[:300])

            if (
                decomp_depth.get(stage.name, 0) < MAX_DECOMPOSITION_DEPTH
                and stage_failures[stage.name] >= STAGE_FAILURE_THRESHOLD
            ):
                log(f"[orch] mid-run decomposition triggered for stage {stage.name!r}")
                log_event("decomposition_trigger",
                          stage=stage.name, failures=stage_failures[stage.name])
                sub_stages = await _decompose_stage(
                    stage, scheduler, http_client, log=log,
                )
                if sub_stages:
                    decomp_depth[stage.name] = decomp_depth.get(stage.name, 0) + 1
                    result.decompositions += 1
                    queue = queue[:idx] + list(sub_stages) + queue[idx + 1:]
                    log(f"[orch] decomposed into {len(sub_stages)} sub-stages")
                    log_event("decomposition_complete",
                              stage=stage.name, sub_stage_count=len(sub_stages),
                              sub_stages=[s.name for s in sub_stages])
                    continue
                log_event("decomposition_fail",
                          stage=stage.name, reason="planner returned empty sub-stages")

            result.error = f"stage {stage.name!r} failed: {e!s}"
            result.stages.append(StageRecord(
                name=stage.name, role=stage.role,
                slot_id=None, provider=None, family=None,
                duration_s=0.0, ok=False, error=str(e)[:200],
            ))
            return result

        result.stages.extend(stage_records)
        ok_records = [r for r in stage_records if r.ok]
        for rec in ok_records:
            if rec.provider:
                result.providers_used.append(rec.provider)
        if ok_records:
            log_event("stage_success",
                      stage=stage.name, role=stage.role.value,
                      shard_count=len(stage_records),
                      shards_ok=len(ok_records),
                      providers=[r.provider for r in ok_records if r.provider],
                      families=[r.family for r in ok_records if r.family],
                      total_duration_s=round(
                          sum(r.duration_s for r in ok_records), 2))

        # Determine `prev_provider` for cross-stage rotation. For fanout
        # we use the LAST shard's provider — successive non-fanout stages
        # then exclude that one provider. (Concurrent shards within a
        # fanout already exclude each other via _execute_fanout.)
        if stage_records:
            last_record = stage_records[-1]
            prev_provider = last_record.provider

        # Post-stage Judge: only after stages that produce a full body.
        # Generator/transformer stages with no fanout replace context["body"].
        # Fanout stages don't produce a body — judge runs after the
        # downstream stitcher (which IS a transformer with no fanout).
        is_body_producer = (
            stage.role in (Role.GENERATOR, Role.TRANSFORMER)
            and stage.fanout is None
        )
        if is_body_producer and "body" in context:
            body_words = len(context["body"].split())
            log_event("judge_start",
                      stage=stage.name, body_words=body_words,
                      rule_count=len(schematic.judge_config.get("rules", [])),
                      plugin_count=len(schematic.judge_config.get("plugins", [])),
                      llm_judge_count=len(schematic.judge_config.get("llm_judges", [])))
            judge_report = await judge.evaluate(
                body=context["body"],
                schematic=schematic,
                scheduler=scheduler,
                http_client=http_client,
                exclude_provider=prev_provider,
                metrics=context.get("__metrics__"),
                profile=context.get("__profile__", "default"),
            )
            result.final_report = judge_report
            log_event("judge_done",
                      stage=stage.name, ok=judge_report.ok,
                      hard_fails=len(judge_report.hard_fails),
                      soft_flags=len(judge_report.soft_flags),
                      hard_details=judge_report.hard_fails[:5],
                      soft_details=judge_report.soft_flags[:5])

            if not judge_report.ok and judge_round < schematic.max_rounds:
                judge_round += 1
                result.rounds = judge_round
                log(f"[orch] judge round {judge_round}: {len(judge_report.hard_fails)} "
                    f"hard fails, {len(judge_report.soft_flags)} soft. Injecting retry.")
                log_event("judge_retry_inject",
                          round=judge_round, hard_fails=len(judge_report.hard_fails),
                          soft_flags=len(judge_report.soft_flags))
                retry_pair = _build_retry_pair(stage.name, judge_report, judge_round)
                queue = queue[:idx + 1] + list(retry_pair) + queue[idx + 1:]
            elif not judge_report.ok:
                log(f"[orch] judge still rejecting after {judge_round} rounds; "
                    f"continuing to next stage anyway")
                log_event("judge_max_rounds",
                          rounds=judge_round, hard_fails=len(judge_report.hard_fails))

        # Reset failure counter for this stage on any success.
        stage_failures.pop(stage.name, None)
        idx += 1

    # End of queue. Determine overall success.
    body = context.get("body")
    result.body = body
    result.artifacts = context.get("artifacts", [])
    if body is None:
        result.error = result.error or "no generator/transformer stage produced a body"
        return result

    if result.final_report is not None and not result.final_report.ok:
        result.error = (
            f"judge still has {len(result.final_report.hard_fails)} hard fails "
            f"after {judge_round} rounds: "
            + "; ".join(result.final_report.hard_fails[:3])
        )
        return result

    result.ok = True
    return result


# ---------- Stage execution ----------

class StageFailure(RuntimeError):
    """Raised internally by _execute_stage when retries are exhausted.

    Caught by the main loop to decide between decomposition and shelf.
    """


async def _execute_stage(
    stage: StageDef,
    schematic: TaskSchematic,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    context: dict[str, Any],
    exclude_provider: str | None,
    log: Callable[[str], None],
) -> list[StageRecord]:
    """Run one stage. Returns one StageRecord per shard (or just one for non-fanout)."""
    if stage.fanout is None:
        # Single call.
        record = await _execute_one_call(
            stage, schematic, scheduler, http_client, context,
            exclude_providers={exclude_provider} if exclude_provider else set(),
            shard_index=None,
            log=log,
        )
        return [record]

    # Fanout: resolve `over` to a list, run shards in batches.
    items = _resolve_fanout_over(context, stage.fanout.over)
    if not items:
        raise StageFailure(
            f"fanout.over={stage.fanout.over!r} resolved to empty list"
        )

    log(f"[orch] fanout {stage.name}: {len(items)} shards, "
        f"max_parallel={stage.fanout.max_parallel}")

    records: list[StageRecord] = []
    inflight_providers: set[str] = (
        {exclude_provider} if exclude_provider else set()
    )

    # Pre-allocate so context[stage.name][i] = result lands at the right index
    # even if shards finish out of order (handled by merge in roles.py).
    context.setdefault(stage.name, [None] * len(items))

    sem = asyncio.Semaphore(stage.fanout.max_parallel)
    inflight_lock = asyncio.Lock()

    async def _shard(i: int) -> StageRecord:
        async with sem:
            # Snapshot the providers sibling shards are CURRENTLY holding so this
            # shard starts on a different one. _execute_one_call keeps the shared
            # set live as it picks/retries/releases (registration happens there).
            async with inflight_lock:
                exclude_now = set(inflight_providers)
            try:
                return await _execute_one_call(
                    stage, schematic, scheduler, http_client, context,
                    exclude_providers=exclude_now,
                    shard_index=i,
                    log=log,
                    inflight=inflight_providers,
                    inflight_lock=inflight_lock,
                )
            except StageFailure as e:
                return StageRecord(
                    name=f"{stage.name}[{i}]",
                    role=stage.role,
                    slot_id=None, provider=None, family=None,
                    duration_s=0.0, ok=False, error=str(e)[:200],
                )

    # _execute_one_call registers each shard's picked provider in inflight_providers
    # right after picking and releases it in a finally; we just gather here.
    records = await asyncio.gather(*(_shard(i) for i in range(len(items))))

    # If too many shards failed, the whole stage is a failure.
    failed = sum(1 for r in records if not r.ok)
    if failed > len(records) // 2:  # majority failed
        raise StageFailure(
            f"fanout {stage.name}: {failed}/{len(records)} shards failed"
        )
    if failed > 0:
        log(f"[orch] fanout {stage.name}: {failed}/{len(records)} shards failed "
            f"(continuing with surviving shards)")
    return records


async def _execute_one_call(
    stage: StageDef,
    schematic: TaskSchematic,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    context: dict[str, Any],
    exclude_providers: set[str],
    shard_index: int | None,
    log: Callable[[str], None],
    inflight: set[str] | None = None,
    inflight_lock: "asyncio.Lock | None" = None,
) -> StageRecord:
    """Execute one LLM call for a stage. Handles retries on call-level failure.

    When `inflight`/`inflight_lock` are supplied (fanout), this call REGISTERS its
    currently-picked provider into the shared set so sibling shards exclude it, swaps
    the registration on each retry, and discards it in a finally. (Previously the
    fanout passed only a snapshot copy and nothing was ever added, so concurrent
    shards did not actually avoid each other - whole-repo review finding, 2026-06.)
    """
    t0 = time.monotonic()

    # Build prompts.
    inputs_to_render = list(stage.inputs)
    if not inputs_to_render:
        # Default inputs by role.
        inputs_to_render = _default_inputs_for_role(stage.role)

    # Substitute {i} placeholders for fanout shards. e.g. "outline.{i}"
    # becomes "outline.0" for shard 0.
    if shard_index is not None:
        inputs_to_render = [
            p.replace("{i}", str(shard_index)) for p in inputs_to_render
        ]

    inputs_rendered = render_inputs(context, inputs_to_render)
    output_rules_rendered = render_output_rules(schematic.output_rules)

    #   file-output mode: splice the marker-block protocol into body-producing
    #   stages so the model emits multi-file artifacts we can reconstruct.
    instructions = stage.instructions
    if (schematic.output_rules.get("format") == "files"
            and stage.role in (Role.GENERATOR, Role.TRANSFORMER)):
        import artifacts as _artifacts
        instructions = instructions + _artifacts.artifact_instructions(
            context.get("__nonce__", ""))

    system_prompt = render_skeleton(
        stage.role,
        instructions=instructions,
        output_rules_rendered=output_rules_rendered,
        inputs_rendered=inputs_rendered,
    )

    max_tokens = stage.max_tokens or _ROLE_DEFAULT_MAX_TOKENS.get(stage.role, 8192)

    #   ctx-aware routing: estimate how much context this call needs (prompt in + output
    #   reserve) and only route to a host whose window can hold it. ~chars/3 for code-ish
    #   prompts; +512 overhead for the chat envelope. Prevents 413s from small-ctx hosts
    #   when a stage carries a big input (e.g. a whole-repo blob in repo_audit).
    _user_msg = "Produce the requested output now."
    min_ctx = (len(system_prompt) + len(_user_msg)) // 3 + max_tokens + 512

    # Up to 3 retries at the call layer (different slot each time via
    # scheduler bandit). Each retry adds the failed slot's provider to
    # the exclude set so we don't keep hitting the same one.
    excluded_so_far: set[str] = set(exclude_providers)
    last_error: str | None = None
    registered: str | None = None  # provider currently held in the shared inflight set

    async def _register(provider_name: str | None) -> None:
        #   swap this shard's reservation in the shared fanout set (under its lock).
        nonlocal registered
        if inflight is None or inflight_lock is None:
            return
        async with inflight_lock:
            if registered is not None:
                inflight.discard(registered)
            registered = provider_name
            if provider_name is not None:
                inflight.add(provider_name)

    shard_label = f"{stage.name}[{shard_index}]" if shard_index is not None else stage.name

    try:
      for attempt in range(STAGE_CALL_ATTEMPTS):
        try:
            slot = scheduler.pick_slot(
                role=stage.role, exclude_providers=excluded_so_far, min_ctx=min_ctx,
            )
        except SchedulerError as e:
            last_error = f"no slot available: {e!s}"
            log(f"[orch] {stage.name}: {last_error}")
            log_event("stage_attempt_fail",
                      stage=shard_label, role=stage.role.value,
                      attempt=attempt + 1, fail_code="no_slot",
                      reason=last_error[:200])
            break

        #   reserve this provider so sibling shards in the fanout avoid it.
        await _register(slot.provider.name)

        log_event("stage_attempt",
                  stage=shard_label, role=stage.role.value,
                  slot=slot.who, provider=slot.provider.name,
                  family=slot.model_family, attempt=attempt + 1,
                  exclude_providers=sorted(excluded_so_far),
                  shard_index=shard_index)

        await scheduler.wait_for_provider_pacing(slot)

        response_format = None
        if stage.role in (Role.PLANNER, Role.EXTRACTOR, Role.VERIFIER):
            response_format = {"type": "json_object"}

        try:
            content, usage = await call_slot(
                http_client, slot,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "Produce the requested output now."},
                ],
                max_tokens=max_tokens,
                response_format=response_format,
            )
        except ProviderError as e:
            err = str(e)
            code = err.split(":", 1)[0]
            scheduler.record_failure(slot, code, reason=err[:200])
            _emit_stage_metric(context, slot, stage, round(time.monotonic() - t0, 2),
                               {}, ok=False, code=code)
            excluded_so_far.add(slot.provider.name)
            last_error = err[:200]
            log(f"[orch] {shard_label} attempt {attempt + 1} on {slot.who}: "
                f"{code} ({err[:80]})")
            log_event("stage_attempt_fail",
                      stage=shard_label, role=stage.role.value,
                      slot=slot.who, provider=slot.provider.name,
                      attempt=attempt + 1, fail_code=code, reason=err[:200])
            continue

        try:
            merge_role_output(
                stage.role, stage.name, context, content,
                shard_index=shard_index,
                nonce=context.get("__nonce__", ""),
                output_format=schematic.output_rules.get("format"),
            )
        except RoleMergeError as e:
            scheduler.record_failure(slot, "shape", reason=str(e)[:200])
            _emit_stage_metric(context, slot, stage, round(time.monotonic() - t0, 2),
                               usage, ok=False, code="shape")
            excluded_so_far.add(slot.provider.name)
            last_error = str(e)[:200]
            log(f"[orch] {shard_label} merge failed on {slot.who}: {last_error[:80]}")
            log_event("stage_attempt_fail",
                      stage=shard_label, role=stage.role.value,
                      slot=slot.who, provider=slot.provider.name,
                      attempt=attempt + 1, fail_code="merge", reason=last_error[:200])
            continue

        scheduler.record_success(slot)
        _emit_stage_metric(context, slot, stage, round(time.monotonic() - t0, 2),
                           usage, ok=True, code="ok")
        duration = round(time.monotonic() - t0, 2)
        log(f"[orch] {shard_label} OK via {slot.who} ({duration}s)")
        log_event("stage_attempt_ok",
                  stage=shard_label, role=stage.role.value,
                  slot=slot.who, provider=slot.provider.name,
                  family=slot.model_family, attempt=attempt + 1,
                  duration_s=duration,
                  out_chars=len(content))
        return StageRecord(
            name=shard_label,
            role=stage.role,
            slot_id=slot.who,
            provider=slot.provider.name,
            family=slot.model_family,
            duration_s=duration,
            ok=True,
        )
    finally:
        #   release this shard's provider reservation on ANY exit (success, failure,
        #   exception) so the shared inflight set never leaks a stale entry.
        await _register(None)

    raise StageFailure(
        f"all {STAGE_CALL_ATTEMPTS} attempts failed for stage {stage.name!r}; last: {last_error}"
    )


# ---------- Helpers ----------

def _emit_stage_metric(context: dict, slot, stage, latency_s: float,
                       usage: dict, *, ok: bool, code: str) -> None:
    #   one per-profile metric event per stage call (success or failure). best-effort.
    metrics = context.get("__metrics__")
    if metrics is None:
        return
    usage = usage or {}
    try:
        metrics.record(
            profile=context.get("__profile__", "default"),
            logical=stage.name, provider=slot.provider.name, model=slot.model,
            family=slot.model_family, role=stage.role.value,
            effort=context.get("__effort__", "med"), latency_s=latency_s,
            in_tokens=usage.get("prompt_tokens"), out_tokens=usage.get("completion_tokens"),
            ok=ok, code=code, attempts=1, routed_to=slot.who,
        )
    except Exception as e:  # noqa: BLE001 - metrics must never break a stage
        log_event("metrics_record_error", where="orchestrator", error=repr(e)[:200])


def _default_inputs_for_role(role: Role) -> list[str]:
    """Inputs to render when StageDef.inputs is empty.

    Maps each role to the context key it most likely wants:
      generator/transformer  → ["prompt", "body"] (latest body if any)
      reviewer               → ["body", "prompt"]
      extractor              → ["prompt"]
      verifier               → ["body"]
      planner (sub-stage)    → ["prompt"]
    """
    if role in (Role.GENERATOR, Role.TRANSFORMER):
        return ["prompt", "body"]
    if role is Role.REVIEWER:
        return ["body", "prompt"]
    if role is Role.VERIFIER:
        return ["body"]
    return ["prompt"]


def _resolve_fanout_over(context: dict[str, Any], path: str) -> list[Any]:
    """Resolve a fanout 'over' path to a concrete list of items.

    Path examples:
      "outline.topics"    → context["outline"]["topics"] (a list)
      "extracted.steps"   → context["extracted"]["steps"]
      "candidates"        → context["candidates"] (must be a list)
    """
    parts = path.split(".")
    value: Any = context
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part, [])
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                value = []
        else:
            value = []
            break
    if not isinstance(value, list):
        return []
    return value


def _build_retry_pair(
    failed_stage_name: str, report: JudgeReport, round_idx: int,
) -> list[StageDef]:
    """Build a (reviewer, transformer) pair to inject after a judge fail.

    The reviewer reads the body + the judge report; the transformer
    applies the critique to the body. Same role-skeleton machinery as
    any other stage.
    """
    feed = report.to_critique_feed()
    return [
        StageDef(
            name=f"_retry{round_idx}_critique",
            role=Role.REVIEWER,
            instructions=(
                f"The previous draft was rejected by the judge. "
                f"Address every hard failure and as many soft flags as possible.\n\n"
                f"## Judge findings\n{feed}\n\n"
                f"Identify each issue with a specific actionable fix."
            ),
            inputs=("body",),
            max_tokens=4000,
        ),
        StageDef(
            name=f"_retry{round_idx}_revise",
            role=Role.TRANSFORMER,
            instructions=(
                f"Apply the critique to the body. Drop unsupportable claims "
                f"rather than fabricating sources. Return the FULL revised content."
            ),
            inputs=("body", f"_retry{round_idx}_critique"),
            max_tokens=8000,
        ),
    ]


async def _decompose_stage(
    stage: StageDef,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    log: Callable[[str], None],
) -> list[StageDef]:
    """Plan a sub-pipeline replacing a single failing stage.

    Calls the Planner with a special directive: "break this stage into
    3-5 smaller stages ending with a transformer stitcher". The Planner
    returns a sub-schematic; we extract its stages and return them as
    the replacement.

    On planning failure, returns empty list — caller treats this as
    an unrecoverable stage failure and shelves the task.
    """
    from .planner import plan

    decomp_prompt = (
        f"The stage '{stage.name}' (role={stage.role.value}) keeps failing. "
        f"Original instructions: {stage.instructions!r}. "
        f"Break this work into 3-5 smaller stages — each small enough for "
        f"a 30B-parameter model to complete reliably — and end with a "
        f"transformer stage that stitches the smaller outputs into the "
        f"shape the parent pipeline expects. "
        f"Emit a complete TaskSchematic JSON with task_type=\"decomposition\"."
    )

    try:
        sub_schematic = await plan(
            prompt_text=decomp_prompt,
            scheduler=scheduler,
            http_client=http_client,
            log=log,
        )
        return list(sub_schematic.stages)
    except Exception as e:  # noqa: BLE001
        log(f"[orch] decomposition planning failed: {e!r}")
        return []
