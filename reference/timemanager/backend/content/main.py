from __future__ import annotations
import asyncio
import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from oplog import log_event
from scheduler import (
    ProviderError, SchedulerError, SlotScheduler,
    call_slot, is_network_code,
)

from . import checkpoint
from .roles import (
    Roles,
    merge as merge_role_output,
    build_input_context,
    build_output_context,
    make_role,
    resolve_path,
)
from .schematics import Stage, Task

output_dir = Path(__file__).resolve().parent.parent / "outputs"
raw_dir = output_dir / "raw"

# the orchestrator: runs a Task end to end.
# stages execute in order. cross-stage rotation excludes the previous stage's
# provider so back-to-back calls hit different carriers.
# fanout stages run once per item in `over`, in batches of max_parallel.

role_default_max_tokens = {
    Roles.planner: 4000,
    Roles.generator: 8000,
    Roles.critiquer: 4000,
    Roles.transformer: 8000,
    Roles.parser: 2000,
    Roles.verifier: 200,
}


@dataclass
class StageRecord:
    name: str
    role: Roles
    slot_id: str | None
    provider: str | None
    family: str | None
    duration_s: float
    ok: bool
    fanouts: int = 1
    error: str | None = None


@dataclass
class RunResult:
    ok: bool
    body: str | None
    stages: list[StageRecord] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)
    credits: list[dict] = field(default_factory=list)
    error: str | None = None


class StageFailure(RuntimeError):
    """raised internally when a stage exhausts retries; caught by the main loop."""


class NetworkPauseError(StageFailure):
    """all attempts of a stage hit network-class errors. this is treated as a
    pause not a failure - the runner keeps the checkpoint, doesn't bump the
    attempts counter, and resumes on the next pass when the network is back."""


def schematic_hash(schematic: Task) -> str:
    parts = []
    for s in schematic.stages:
        fanout_info = f"fanout={s.fanout.over}" if s.fanout else ""
        parts.append(f"{s.name}|{s.role.value}|{fanout_info}|inputs={','.join(s.inputs)}")
    body = "|".join(parts)
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


async def execute(schematic: Task, scheduler: SlotScheduler, http_client: httpx.AsyncClient,
                  *, stem: str, initial_context: dict[str, Any] | None = None,
                  log: Callable[[str], None] = print) -> RunResult:

    context: dict[str, Any] = dict(initial_context or {})
    result = RunResult(ok=False, body=None)
    completed_stages: list[str] = []
    credits: list[dict] = []

    current_hash = schematic_hash(schematic)
    saved = checkpoint.load(stem)
    if saved is not None and saved.schematic_hash and saved.schematic_hash != current_hash:
        log(f"[main] checkpoint schematic mismatch (saved={saved.schematic_hash}, current={current_hash}) - discarding")
        log_event("checkpoint_discarded", stem=stem, reason="schematic_changed")
        checkpoint.clear(stem)
        saved = None

    if saved is not None:
        merged_context = dict(saved.context)
        merged_context.update(context)
        context = merged_context
        completed_stages = list(saved.completed_stages)
        credits = list(saved.credits)
        log(f"[main] resuming: {len(completed_stages)} stages already complete "
            f"({', '.join(completed_stages[:5]) + ('...' if len(completed_stages) > 5 else '')})")
        log_event("execute_resume",
                  stem=stem, completed=completed_stages,
                  credits_count=len(credits))

    raw_stem_dir = raw_dir / stem
    if saved is None:
        shutil.rmtree(raw_stem_dir, ignore_errors=True)
    raw_stem_dir.mkdir(parents=True, exist_ok=True)

    raw_seq: list[int] = [0]

    queue: list[Stage] = list(schematic.stages)
    prev_provider: str | None = None
    idx = 0

    while idx < len(queue):
        stage = queue[idx]

        if stage.name in completed_stages:
            log(f"[main] resumed: skip {stage.name}")
            log_event("stage_skipped_resumed", stage=stage.name, role=stage.role.value)
            idx += 1
            continue

        log_event("stage_start",
                  stage=stage.name, role=stage.role.value,
                  fanout=stage.fanout is not None,
                  fanout_over=stage.fanout.over if stage.fanout else None,
                  exclude_provider=prev_provider)

        try:
            stage_records = await execute_stage(
                stage=stage,
                schematic=schematic,
                scheduler=scheduler,
                http_client=http_client,
                context=context,
                exclude_provider=prev_provider,
                log=log,
            )
        except NetworkPauseError as error:
            log(f"[main] network pause at stage {stage.name!r}: {error!s}")
            log_event("stage_network_pause",
                      stage=stage.name, role=stage.role.value,
                      reason=str(error)[:300])
            raise
        except StageFailure as error:
            log_event("stage_fail",
                      stage=stage.name, role=stage.role.value,
                      reason=str(error)[:500])

            result.error = f"stage {stage.name!r} failed: {error!s}"
            result.stages.append(StageRecord(
                name=stage.name, role=stage.role, slot_id=None,
                provider=None, family=None, duration_s=0.0,
                ok=False, error=str(error)[:200],
            ))
            return result

        result.stages.extend(stage_records)
        ok_records = [r for r in stage_records if r.ok]
        for rec in ok_records:
            if rec.provider:
                result.providers_used.append(rec.provider)
            if rec.ok and rec.slot_id:
                credits.append({
                    "stage": rec.name, "role": rec.role.value, "slot": rec.slot_id,
                })
        if ok_records:
            log_event("stage_success",
                      stage=stage.name,
                      role=stage.role.value,
                      fanouts=len(stage_records),
                      ok=len(ok_records),
                      providers=[r.provider for r in ok_records if r.provider],
                      families=[r.family for r in ok_records if r.family],
                      total_duration_s=round(sum(r.duration_s for r in ok_records), 2))

        if stage_records:
            prev_provider = stage_records[-1].provider

        if stage.fanout is not None:
            shards = context.get(stage.name, [])
            for shard_idx, shard_content in enumerate(shards):
                if shard_content is not None:
                    raw_seq[0] += 1
                    text = (shard_content if isinstance(shard_content, str)
                            else json.dumps(shard_content, indent=2, ensure_ascii=False))
                    write_raw_stage(raw_stem_dir, raw_seq[0],
                                    f"{stage.name}_{shard_idx}", text)
        elif "body" in context:
            raw_seq[0] += 1
            write_raw_stage(raw_stem_dir, raw_seq[0], stage.name, context["body"])

        completed_stages.append(stage.name)
        checkpoint.save(stem, context, completed_stages, credits, current_hash)

        idx += 1

    body = context.get("body")
    result.body = body
    if body is None:
        result.error = result.error or "no generator/transformer stage produced a body"
        return result

    result.ok = True
    result.credits = credits
    checkpoint.clear(stem)
    return result


async def execute_stage(stage: Stage, schematic: Task, scheduler: SlotScheduler,
                        http_client: httpx.AsyncClient, context: dict[str, Any],
                        exclude_provider: str | None,
                        log: Callable[[str], None]) -> list[StageRecord]:

    if stage.fanout is None:
        record = await execute_one_call(
            stage, schematic, scheduler, http_client, context,
            exclude_providers={exclude_provider} if exclude_provider else set(),
            fanout_index=None, log=log,
        )
        return [record]

    items = resolve_fanout_over(context, stage.fanout.over)
    if not items:
        raise StageFailure(f"fanout.over={stage.fanout.over!r} resolved to empty list")

    log(f"[main] fanout {stage.name}: {len(items)} shards, "
        f"max_parallel={stage.fanout.max_parallel}")

    active_providers: set[str] = ({exclude_provider} if exclude_provider else set())

    context.setdefault(stage.name, [None] * len(items))

    sem = asyncio.Semaphore(stage.fanout.max_parallel)
    inflight_lock = asyncio.Lock()

    async def shard(i: int) -> StageRecord:
        async with sem:
            async with inflight_lock:
                exclude_now = set(active_providers)
            try:
                rec = await execute_one_call(
                    stage, schematic, scheduler, http_client, context,
                    exclude_providers=exclude_now,
                    fanout_index=i, log=log,
                )
                async with inflight_lock:
                    if rec.provider:
                        active_providers.discard(rec.provider)
                return rec
            except StageFailure as e:
                return StageRecord(
                    name=f"{stage.name}[{i}]",
                    role=stage.role,
                    slot_id=None, provider=None, family=None,
                    duration_s=0.0, ok=False, error=str(e)[:200],
                )

    records = await asyncio.gather(*(shard(i) for i in range(len(items))))

    failed = sum(1 for r in records if not r.ok)
    if failed > len(records) // 2:
        raise StageFailure(f"fanout {stage.name}: {failed}/{len(records)} shards failed")
    if failed > 0:
        log(f"[main] fanout {stage.name}: {failed}/{len(records)} shards failed "
            f"(continuing with surviving shards)")
    return records


async def execute_one_call(stage: Stage, schematic: Task, scheduler: SlotScheduler,
                           http_client: httpx.AsyncClient, context: dict[str, Any],
                           exclude_providers: set[str], fanout_index: int | None,
                           log: Callable[[str], None]) -> StageRecord:

    t0 = time.monotonic()

    if stage.role is Roles.assembler:
        return assemble_stage(stage, context, fanout_index, log, t0)

    inputs_to_render = list(stage.inputs)
    if not inputs_to_render:
        inputs_to_render = default_inputs_for_role(stage.role)

    if fanout_index is not None:
        inputs_to_render = [p.replace("{i}", str(fanout_index)) for p in inputs_to_render]

    inputs_rendered = build_input_context(context, inputs_to_render)
    output_rules_rendered = build_output_context(schematic.output_rules)
    system_prompt = make_role(
        stage.role,
        instructions=stage.instructions,
        output_rules=output_rules_rendered,
        inputs=inputs_rendered,
    )
    max_tokens = stage.max_tokens or role_default_max_tokens.get(stage.role, 4000)

    max_attempts = 5
    excluded_so_far: set[str] = set(exclude_providers)
    last_error: str | None = None
    attempt_codes: list[str] = []

    label = f"{stage.name}[{fanout_index}]" if fanout_index is not None else stage.name

    for attempt in range(max_attempts):
        try:
            picked = scheduler.pick_slot(role=stage.role, exclude_providers=excluded_so_far)
        except SchedulerError as e:
            last_error = f"no slot available: {e!s}"
            log(f"[main] {stage.name}: {last_error}")
            log_event("stage_attempt_fail",
                      stage=label, role=stage.role.value,
                      attempt=attempt + 1, fail_code="no_slot",
                      reason=last_error[:200])
            attempt_codes.append("no_slot")
            break

        log_event("stage_attempt",
                  stage=label, role=stage.role.value,
                  slot=picked.who, provider=picked.provider.name,
                  family=picked.model_family, attempt=attempt + 1,
                  exclude_providers=sorted(excluded_so_far),
                  fanout_index=fanout_index)

        await scheduler.wait_for_provider_pacing(picked)

        response_format = None
        if stage.role in (Roles.planner, Roles.parser, Roles.verifier):
            response_format = {"type": "json_object"}

        try:
            content = await call_slot(
                http_client, picked,
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
            scheduler.record_failure(picked, code, reason=err[:400])
            excluded_so_far.add(picked.provider.name)
            last_error = err[:200]
            attempt_codes.append(code)
            log(f"[main] {label} attempt {attempt + 1} on {picked.who}: {code} ({err[:80]})")
            log_event("stage_attempt_fail",
                      stage=label, role=stage.role.value,
                      slot=picked.who, provider=picked.provider.name,
                      attempt=attempt + 1, fail_code=code, reason=err[:400])
            continue

        try:
            merge_role_output(stage.role, stage.name, context, content, index=fanout_index)
        except ValueError as e:
            scheduler.record_failure(picked, "shape", reason=str(e)[:200])
            excluded_so_far.add(picked.provider.name)
            last_error = str(e)[:200]
            attempt_codes.append("merge")
            log(f"[main] {label} merge failed on {picked.who}: {last_error[:80]}")
            log_event("stage_attempt_fail",
                      stage=label, role=stage.role.value,
                      slot=picked.who, provider=picked.provider.name,
                      attempt=attempt + 1, fail_code="merge", reason=last_error[:200])
            continue

        scheduler.record_success(picked)
        duration = round(time.monotonic() - t0, 2)
        log(f"[main] {label} OK via {picked.who} ({duration}s)")
        log_event("stage_attempt_ok",
                  stage=label, role=stage.role.value,
                  slot=picked.who, provider=picked.provider.name,
                  family=picked.model_family, attempt=attempt + 1,
                  duration_s=duration, out_chars=len(content))
        return StageRecord(
            name=label,
            role=stage.role,
            slot_id=picked.who,
            provider=picked.provider.name,
            family=picked.model_family,
            duration_s=duration,
            ok=True,
        )

    if attempt_codes and all(is_network_code(c) for c in attempt_codes):
        log(f"[main] {label}: all {len(attempt_codes)} attempts hit network-class "
            f"errors ({sorted(set(attempt_codes))}) - pausing instead of failing")
        log_event("stage_network_pause_decision",
                  stage=label, role=stage.role.value,
                  attempt_codes=attempt_codes, last_error=last_error)
        raise NetworkPauseError(
            f"network down at stage {stage.name!r}: {last_error}"
        )

    raise StageFailure(f"all attempts failed for stage {stage.name!r}; last: {last_error}")


assembler_placeholder = re.compile(r"\{([\w\.\*]+)\}")


def assemble_stage(stage: Stage, context: dict[str, Any],
                   fanout_index: int | None, log: Callable[[str], None],
                   t0: float) -> StageRecord:
    template = stage.instructions
    if fanout_index is not None:
        template = template.replace("{i}", str(fanout_index))

    rendered = assembler_placeholder.sub(
        lambda match: resolve_path(context, match.group(1)),
        template,
    )

    label = f"{stage.name}[{fanout_index}]" if fanout_index is not None else stage.name
    duration = round(time.monotonic() - t0, 4)

    if not rendered.strip():
        raise StageFailure(
            f"assembler {stage.name!r} produced empty output - "
            f"template referenced unresolved keys?"
        )

    if fanout_index is not None:
        bucket = context.setdefault(stage.name, [])
        while len(bucket) <= fanout_index:
            bucket.append(None)
        bucket[fanout_index] = rendered
    else:
        context[stage.name] = rendered
        context["body"] = rendered

    log(f"[main] {label} assembled ({len(rendered)} chars, "
        f"{len(rendered.split())} words, {duration}s)")
    log_event("stage_attempt_ok",
              stage=label, role=stage.role.value,
              slot="<assembler>", provider="<code>", family="<assembler>",
              attempt=1, duration_s=duration, out_chars=len(rendered))

    return StageRecord(
        name=label,
        role=stage.role,
        slot_id="<assembler>",
        provider="<code>",
        family="<assembler>",
        duration_s=duration,
        ok=True,
    )


def write_raw_stage(stem_dir: Path, seq: int, label: str, text: str) -> None:
    try:
        stem_dir.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^\w\-]", "_", label)[:60]
        path = stem_dir / f"{seq:02d}_{safe_label}.md"
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log_event("raw_stage_write_fail", label=label, reason=repr(e)[:200])


def default_inputs_for_role(role: Roles) -> list[str]:
    if role in (Roles.generator, Roles.transformer):
        return ["prompt", "body"]
    if role is Roles.critiquer:
        return ["body", "prompt"]
    if role is Roles.verifier:
        return ["body"]
    return ["prompt"]


def resolve_fanout_over(context: dict[str, Any], path: str) -> list[Any]:
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
    if isinstance(value, list):
        return value
    # LLMs often return JSON arrays as raw strings in the context.
    # Try to parse them so fanout stages can iterate.
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        # Not a parseable array — treat the whole string as a single item.
        if stripped:
            return [stripped]
    return []
