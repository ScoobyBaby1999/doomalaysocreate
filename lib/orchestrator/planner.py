"""
planner.py - The Planner LLM call that emits TaskSchematic JSON.

# What this module does

Given a raw user prompt, produces a validated TaskSchematic the
Orchestrator can execute. Three resolution paths, in priority order:

  1. Cached schematic next to the prompt file
     If <prompt_path>.schematic.json exists, load and return it. Zero
     LLM calls. This is the "we've planned this exact prompt before"
     fast path.

  2. Template hint
     Classify the prompt by keyword/header regex against shipped
     templates (research_paper, lesson_plan, freeform, ...). The
     matched template is inlined as a few-shot example in the Planner
     LLM prompt — the model adapts the template instead of writing
     from scratch.

  3. Blank Planner call
     No template match → ask the Planner to invent a schematic from
     scratch using only the role enum and rule types as constraints.

# Retry on parse failure

The Planner's output is the JSON contract for the entire run. If it
emits invalid JSON or fails schema validation, we retry with the
specific error spliced into the next attempt's prompt. The scheduler
naturally picks a different slot per retry (because the previous slot
got `record_failure(json)`'d, lowering its family score). After
max_attempts we give up and return a freeform single-stage fallback
schematic.

# bob_the_builders (committee planning)

Optional. When committee_size > 1 we fan out N parallel Planner calls
(scheduler picks distinct families/carriers per shard) and a synthesis
call merges the N proposals into one. Costs N+1 calls instead of 1.
Default committee_size = 1. Use 3-5 when you don't trust the planner
on this task type.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from oplog import log_event
from scheduler import ProviderError, SchedulerError, SlotScheduler, call_slot

from .roles import Role, render_skeleton
from .schematic import (
    SchemaValidationError,
    TaskSchematic,
    from_json_text,
    parse_and_validate,
)


# Where shipped templates live. Each is a JSON file matching TaskSchematic
# shape. Filename stem is matched against the user prompt by classify().
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


# ---------- Template classification ----------

# Keyword cues for each task_type. First match wins. Order matters:
# more specific types come first, freeform is the catch-all default.
#
# These are intentionally broad — the Planner can override the inferred
# task_type if the prompt explicitly says otherwise. Misclassification
# costs one extra retry round, not the whole run.
_CLASSIFIER_FLAGS = re.MULTILINE | re.IGNORECASE

_CLASSIFIER_RULES: list[tuple[str, list[str]]] = [
    ("lesson_plan", [
        r"\blesson\s*plan\b", r"\bcurriculum\b", r"\bteach\b",
        r"\beducational\s+content\b", r"\b\d+[\-\s]minute\s+lesson\b",
        r"\blearning\s+objectives?\b",
    ]),
    ("research_paper", [
        r"\bresearch\s+paper\b", r"\bdeep[\-\s]dive\b",
        r"\b(write|draft)\s+(an?\s+)?(in[\-\s]?depth|comprehensive)\b",
        r"\bliterature\s+review\b", r"\bscholarly\s+article\b",
        r"\bscope:\s*\d+[\-\s]?\d*\s+words?\b",
        # Strong signal: a prompt file (.md) with these structural sections
        # is one of our existing research-paper prompts.
        r"^\s*##\s+project\s+context\b",
        r"^\s*##\s+target\s+output\b",
        r"^\s*##\s+(research|writing)\s+(scope|requirements?)\b",
        r"^\s*##\s+sources\s+to\s+(cover|consider|include)\b",
        # Word-count target lines that appear in the existing prompts.
        r"\b\d{4,5}[\-\s]?\d{0,5}\s+words?\b",
    ]),
    ("code_spec", [
        r"\b(api|technical)\s+(spec|design|documentation)\b",
        r"\barchitecture\s+(decision|document)\b",
        r"\bdesign\s+document\b",
    ]),
    ("creative_writing", [
        r"\b(story|narrative|fiction|poem|character\s+profile)\b",
        r"\bworld[\-\s]building\b", r"\bplot\s+outline\b",
    ]),
    ("summary", [
        r"\bsummari[sz]e\b", r"\btl;?dr\b", r"\bbrief\s+overview\b",
    ]),
    ("translation", [
        r"\btranslat(e|ion|ed|ing)\b",
    ]),
]


def classify_prompt(prompt_text: str) -> str:
    """Classify a prompt into one of the known task_types.

    Returns the matched task_type string, or "freeform" if no rule fires.
    Does NOT validate that a corresponding template exists — caller
    handles missing templates by falling through to blank planning.
    """
    # Don't lowercase — multiline regexes use ^/$ on the original text;
    # case insensitivity is per-pattern via _CLASSIFIER_FLAGS.
    for task_type, patterns in _CLASSIFIER_RULES:
        for pat in patterns:
            if re.search(pat, prompt_text, _CLASSIFIER_FLAGS):
                return task_type
    return "freeform"


def find_template(task_type: str) -> Path | None:
    """Return the template file path for a task_type, or None if missing."""
    candidate = TEMPLATES_DIR / f"{task_type}.json"
    return candidate if candidate.exists() else None


# ---------- Cache lookup ----------

def find_cached_schematic(prompt_path: Path) -> TaskSchematic | None:
    """Load <prompt_path>.schematic.json if it exists.

    Cached schematics are written by save_cached_schematic() after a
    successful Planner run. The next time the same prompt is processed,
    the cached schematic is loaded directly and the Planner is skipped.
    """
    cache_path = prompt_path.with_suffix(prompt_path.suffix + ".schematic.json")
    if not cache_path.exists():
        return None
    try:
        from .schematic import from_json_file
        return from_json_file(cache_path)
    except (SchemaValidationError, OSError):
        # Corrupted cache → ignore, re-plan from scratch.
        return None


def save_cached_schematic(prompt_path: Path, schematic: TaskSchematic) -> None:
    """Persist a successfully-planned schematic next to its prompt."""
    cache_path = prompt_path.with_suffix(prompt_path.suffix + ".schematic.json")
    payload = _schematic_to_dict(schematic)
    cache_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _schematic_to_dict(s: TaskSchematic) -> dict:
    """Convert a TaskSchematic back to a plain dict for caching."""
    return {
        "task_type": s.task_type,
        "task": s.task,
        "stages": [
            {
                "name": st.name,
                "role": st.role.value,
                "instructions": st.instructions,
                "inputs": list(st.inputs),
                **({"fanout": {"over": st.fanout.over,
                               "max_parallel": st.fanout.max_parallel}}
                   if st.fanout else {}),
                **({"max_tokens": st.max_tokens} if st.max_tokens else {}),
                "on_judge_fail": st.on_judge_fail,
            }
            for st in s.stages
        ],
        "output_rules": dict(s.output_rules),
        "judge_config": dict(s.judge_config),
        "max_rounds": s.max_rounds,
        "committee_size": s.committee_size,
    }


# ---------- The single Planner call ----------

async def plan(
    prompt_text: str,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    *,
    template_hint: TaskSchematic | None = None,
    max_attempts: int = 3,
    log: callable = print,
) -> TaskSchematic:
    """Produce a validated TaskSchematic for one user prompt.

    Resolution:
      1. If template_hint is provided, inline it as a few-shot example
         in the Planner prompt (the model adapts the example).
      2. Make Planner call with role=PLANNER, response_format=json_object.
      3. Parse + validate. On failure, retry with error feedback.
      4. After max_attempts, fall back to a freeform single-stage schematic.

    Returns a validated TaskSchematic. Never raises (worst case: returns
    the freeform fallback).
    """
    # Build the Planner system prompt (skeleton + optional template).
    template_block = ""
    if template_hint is not None:
        template_block = (
            "Use this as a starting point — modify the `task` field and "
            "each stage's `instructions` to suit the user's specific "
            "prompt. Keep the overall structure unless the prompt asks "
            "for something fundamentally different.\n\n"
            f"```json\n{json.dumps(_schematic_to_dict(template_hint), indent=2)}\n```"
        )

    system_prompt = render_skeleton(
        Role.PLANNER,
        instructions=prompt_text,
        output_rules_rendered="(emit ONLY JSON; no fences; no prose)",
        template_hint=template_block,
    )

    last_error: str | None = None

    for attempt in range(max_attempts):
        user_msg = "Emit the TaskSchematic JSON now."
        if last_error is not None:
            user_msg = (
                f"Your previous attempt failed validation:\n  {last_error}\n\n"
                f"Re-emit the corrected TaskSchematic JSON now."
            )

        try:
            slot = scheduler.pick_slot(role=Role.PLANNER)
        except SchedulerError as e:
            log(f"[planner] no slot available: {e!r}")
            log_event("planner_fail", attempt=attempt, reason=f"no_slot: {e!s}"[:200])
            break

        log(f"[planner] attempt {attempt + 1}/{max_attempts} via {slot.who}")
        log_event("planner_attempt",
                  slot=slot.who, provider=slot.provider.name,
                  family=slot.model_family, attempt=attempt + 1,
                  max_attempts=max_attempts,
                  has_template_hint=template_hint is not None)
        await scheduler.wait_for_provider_pacing(slot)

        try:
            content, _usage = await call_slot(
                http_client, slot,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4000,
                response_format={"type": "json_object"},
            )
        except ProviderError as e:
            err = str(e)
            code = err.split(":", 1)[0]
            scheduler.record_failure(slot, code, reason=err[:200])
            log(f"[planner] call failed ({code}): {err[:120]}")
            log_event("planner_call_fail",
                      slot=slot.who, provider=slot.provider.name,
                      fail_code=code, reason=err[:200])
            last_error = f"call failed: {err[:120]}"
            continue

        try:
            schematic = from_json_text(content)
            scheduler.record_success(slot)
            log(f"[planner] schematic accepted: "
                f"task_type={schematic.task_type}, {len(schematic.stages)} stages")
            log_event("planner_success",
                      slot=slot.who, provider=slot.provider.name,
                      family=slot.model_family,
                      task_type=schematic.task_type,
                      stage_count=len(schematic.stages),
                      fanout_stages=sum(1 for s in schematic.stages if s.fanout),
                      attempt=attempt + 1)
            return schematic
        except SchemaValidationError as e:
            scheduler.record_failure(slot, "json", reason=str(e)[:200])
            last_error = str(e)
            log(f"[planner] schema invalid: {last_error[:200]}")
            log_event("planner_schema_fail",
                      slot=slot.who, provider=slot.provider.name,
                      reason=last_error[:300], attempt=attempt + 1)
            continue

    log(f"[planner] all {max_attempts} attempts exhausted, using freeform fallback")
    log_event("planner_fallback", attempts_used=max_attempts, last_error=last_error)
    return _freeform_fallback(prompt_text)


# ---------- Committee planning (bob_the_builders) ----------

async def bob_the_builders(
    prompt_text: str,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    *,
    committee_size: int = 1,
    template_hint: TaskSchematic | None = None,
    log: callable = print,
) -> TaskSchematic:
    """Plan-with-committee. Default 1 (no committee, just plan()).

    For weak-model setups, set committee_size to 3-5. We fan out N
    parallel Planner calls — the scheduler naturally picks distinct
    (provider, model) combos per shard via family ranking — then run
    a synthesizer call that merges the N proposals into one schematic.

    Why it helps: any single weak-model Planner output is unreliable.
    N proposals + synthesis is robust because errors don't correlate
    across the committee. Same trick that helped Opus synthesize a
    better plan from the Nemotron/Gemma/GLM proposals during design.

    Cost: N + 1 LLM calls instead of 1. Worth it because plan errors
    compound — one bad plan ruins the whole pipeline downstream.
    """
    if committee_size <= 1:
        return await plan(
            prompt_text, scheduler, http_client,
            template_hint=template_hint, log=log,
        )

    log(f"[bob_the_builders] planning with committee of {committee_size}")

    proposals = await asyncio.gather(*[
        plan(prompt_text, scheduler, http_client,
             template_hint=template_hint, log=log)
        for _ in range(committee_size)
    ])

    log(f"[bob_the_builders] synthesizing {len(proposals)} proposals")
    return await _synthesize_proposals(
        proposals, prompt_text, scheduler, http_client, log=log,
    )


async def _synthesize_proposals(
    proposals: list[TaskSchematic],
    prompt_text: str,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    log: callable = print,
) -> TaskSchematic:
    """Merge N TaskSchematic proposals into one via a synthesizer LLM call.

    Reads scripts/orchestrator/prompts/synthesizer.md as the system
    prompt; the user message embeds all N proposals as JSON. Output is
    one TaskSchematic JSON, parsed and validated like a regular plan.

    On synthesis failure, falls back to the first proposal (best-effort
    rather than failing the whole task).
    """
    synth_prompt_path = (
        Path(__file__).resolve().parent / "prompts" / "synthesizer.md"
    )
    synth_skeleton = synth_prompt_path.read_text(encoding="utf-8")

    proposals_json = "\n\n---\n\n".join(
        f"### Proposal {i + 1}\n```json\n"
        f"{json.dumps(_schematic_to_dict(p), indent=2)}\n```"
        for i, p in enumerate(proposals)
    )

    user_msg = (
        f"## User prompt being planned for\n{prompt_text}\n\n"
        f"## Proposed TaskSchematics from the committee\n\n{proposals_json}\n\n"
        f"Merge into one TaskSchematic taking the best of each. "
        f"Emit ONLY the JSON."
    )

    try:
        slot = scheduler.pick_slot(role=Role.PLANNER)
    except SchedulerError:
        log("[synthesize] no slot; falling back to first proposal")
        return proposals[0]

    await scheduler.wait_for_provider_pacing(slot)
    try:
        content, _usage = await call_slot(
            http_client, slot,
            messages=[
                {"role": "system", "content": synth_skeleton},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=6000,
            response_format={"type": "json_object"},
        )
    except ProviderError as e:
        scheduler.record_failure(slot, str(e).split(":", 1)[0], reason=str(e)[:200])
        log(f"[synthesize] call failed: {e!r}; falling back to first proposal")
        return proposals[0]

    try:
        synth = from_json_text(content)
        scheduler.record_success(slot)
        return synth
    except SchemaValidationError as e:
        scheduler.record_failure(slot, "json", reason=str(e)[:200])
        log(f"[synthesize] schema invalid: {e!s}; falling back to first proposal")
        return proposals[0]


# ---------- Fallback ----------

def _freeform_fallback(prompt_text: str) -> TaskSchematic:
    """Single-stage freeform schematic. Used when the Planner exhausts retries.

    Better than failing the task entirely — at least the user gets
    *something* back, even if it's just a one-shot draft. The orchestrator
    runs this through a single GENERATOR call with no judge loop.
    """
    return parse_and_validate({
        "task_type": "freeform",
        "task": prompt_text[:80],
        "stages": [{
            "name": "freeform_write",
            "role": "generator",
            "instructions": prompt_text,
        }],
        "output_rules": {"format": "markdown"},
        "judge_config": {"rules": [], "plugins": [], "llm_judges": []},
        "max_rounds": 1,
        "committee_size": 1,
    })


# ---------- High-level resolver used by runner.py ----------

async def resolve_schematic(
    prompt_path: Path,
    scheduler: SlotScheduler,
    http_client: httpx.AsyncClient,
    *,
    committee_size: int = 1,
    save_cache: bool = True,
    log: callable = print,
) -> TaskSchematic:
    """Top-level resolver. Tries cache → template hint → plan/committee.

    This is what runner.py calls per prompt. Composes all three paths:

      Cache hit          → return immediately (zero LLM calls).
      Template match     → call plan() with template_hint inlined.
      No match           → call plan() with no hint, blank schema only.

    On success (any path), saves the schematic next to the prompt as
    cache for next time.
    """
    # 1. Cache.
    cached = find_cached_schematic(prompt_path)
    if cached is not None:
        log(f"[resolve] using cached schematic at {prompt_path.name}.schematic.json")
        log_event("cache_hit", prompt=prompt_path.name,
                  cached_task_type=cached.task_type)
        return cached
    log_event("cache_miss", prompt=prompt_path.name)

    # 2. Template hint.
    prompt_text = prompt_path.read_text(encoding="utf-8")
    classified = classify_prompt(prompt_text)
    log_event("template_classified",
              prompt=prompt_path.name, task_type=classified,
              prompt_preview=prompt_text[:120].replace("\n", " "))
    template_hint: TaskSchematic | None = None
    template_path = find_template(classified)
    if template_path is not None:
        try:
            from .schematic import from_json_file
            template_hint = from_json_file(template_path)
            log(f"[resolve] template hint: {classified}.json")
            log_event("template_loaded", task_type=classified,
                      template_file=template_path.name)
        except SchemaValidationError as e:
            log(f"[resolve] template {classified}.json invalid, ignoring: {e!s}")
            log_event("template_invalid", task_type=classified, reason=str(e)[:200])
    else:
        log_event("template_missing", task_type=classified)

    # 3. Plan (with optional committee).
    schematic = await bob_the_builders(
        prompt_text, scheduler, http_client,
        committee_size=committee_size,
        template_hint=template_hint,
        log=log,
    )

    # 4. Cache for next time (best effort; never fail run on cache write).
    # Skip caching when:
    #   - save_cache=False (e.g. --dry runs; the user is just inspecting)
    #   - the schematic is the freeform fallback (planning failed; we
    #     don't want to permanently route this prompt through fallback).
    is_fallback = (
        schematic.task_type == "freeform"
        and len(schematic.stages) == 1
        and schematic.stages[0].name == "freeform_write"
    )
    if save_cache and not is_fallback:
        try:
            save_cached_schematic(prompt_path, schematic)
            log(f"[resolve] cached schematic to {prompt_path.name}.schematic.json")
        except OSError as e:
            log(f"[resolve] cache write failed: {e!r}")
    elif is_fallback:
        log("[resolve] not caching freeform fallback (planning failed)")

    return schematic
