from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Callable

import httpx

from oplog import log_event
from scheduler import ProviderError, SchedulerError, SlotScheduler, call_slot

from .roles import Roles, make_role
from .schematics import SchemaError, Task, load_json, task_inspect, remove_header_footer
from .sources import parse_frontmatter

# the planner: turns a raw user prompt into a validated Task schematic.
# resolution order:
#   1. cached schematic next to the prompt file (zero llm calls)
#   2. explicit template from frontmatter (user-selected, bypasses all classification)
#   3. auto-detected template via keyword scan over all registered templates
#   4. blank planner call (worst case)
# on parse failure we retry with the error spliced into the next attempt's prompt
# so the model can correct itself. after max_attempts -> freeform fallback.

# templates dir is one level up from content/ (promoted to backend/templates/)
templates_dir = Path(__file__).resolve().parent.parent / "templates"

# auto-built registry: scan templates/ at import time, map task_type -> Path.
# dropping a new .json into templates/ makes it available immediately on next load.
_template_registry: dict[str, Path] = {}

def _build_template_registry() -> dict[str, Path]:
    registry = {}
    if not templates_dir.exists():
        return registry
    for json_file in templates_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            task_type = data.get("task_type")
            if isinstance(task_type, str) and task_type:
                registry[task_type] = json_file
        except (json.JSONDecodeError, OSError):
            pass
    return registry

def _find_template_by_type(task_type: str) -> Path | None:
    if not _template_registry:
        _template_registry.update(_build_template_registry())
    return _template_registry.get(task_type)

def _find_template_by_filename(name: str) -> Path | None:
    candidate = templates_dir / f"{name}.json"
    return candidate if candidate.exists() else None

# keyword cues extracted from each template's instructions + task field.
# built at import time from the registry; no hardcoded rules.
_classifier_index: list[tuple[str, list[re.Pattern]]] = []

def _build_classifier_index() -> list[tuple[str, list[re.Pattern]]]:
    index = []
    if not _template_registry:
        _template_registry.update(_build_template_registry())
    for task_type, tpl_path in _template_registry.items():
        try:
            data = json.loads(tpl_path.read_text(encoding="utf-8"))
            text = data.get("task", "") + " "
            for stage in data.get("stages", []):
                text += stage.get("instructions", "") + " "
            words = set()
            for word in text.lower().split():
                w = re.sub(r"[^a-z0-9]", "", word)
                if len(w) > 4:
                    words.add(w)
            patterns = [re.compile(r"\b" + re.escape(w) + r"\b", re.IGNORECASE) for w in words]
            index.append((task_type, patterns))
        except (json.JSONDecodeError, OSError):
            pass
    return index

def _classify_from_templates(prompt_text: str) -> str | None:
    if not _classifier_index:
        _classifier_index.extend(_build_classifier_index())
    scores: dict[str, int] = {}
    for task_type, patterns in _classifier_index:
        for pat in patterns:
            if pat.search(prompt_text):
                scores[task_type] = scores.get(task_type, 0) + 1
    if not scores:
        return None
    return max(scores, key=scores.get)

def find_template(task_type: str) -> Path | None:
    result = _find_template_by_type(task_type)
    if result is not None:
        return result
    return _find_template_by_filename(task_type)

def load_template(path: Path) -> Task | None:
    try:
        return load_json(path)
    except (SchemaError, OSError):
        return None

# cache helpers

def find_cached_schematic(prompt_path: Path) -> Task | None:
    cache_path = prompt_path.with_suffix(prompt_path.suffix + ".schematic.json")
    if not cache_path.exists():
        return None
    try:
        return load_json(cache_path)
    except (SchemaError, OSError):
        return None

def save_cached_schematic(prompt_path: Path, schematic: Task) -> None:
    cache_path = prompt_path.with_suffix(prompt_path.suffix + ".schematic.json")
    payload = schematic_to_dict(schematic)
    cache_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

def schematic_to_dict(schema: Task) -> dict:
    return {
        "task_type": schema.task_type,
        "task": schema.task,
        "stages": [
            {
                "name": stage.name,
                "role": stage.role.value,
                "instructions": stage.instructions,
                "inputs": list(stage.inputs),
                **(
                    {"fanout": {"over": stage.fanout.over,
                                "max_parallel": stage.fanout.max_parallel}}
                    if stage.fanout else {}
                ),
                **({"max_tokens": stage.max_tokens} if stage.max_tokens else {}),
            }
            for stage in schema.stages
        ],
        "output_rules": dict(schema.output_rules),
    }

# the planner call

async def plan(prompt_text: str, scheduler: SlotScheduler,
               http_client: httpx.AsyncClient, *,
               template: Task | None = None,
               max_attempts: int = 3,
               log: Callable[[str], None] = print) -> Task:

    template_block = ""
    if template is not None:
        template_block = (
            "Use this as a starting point. Modify the `task` field and each "
            "stage's `instructions` to suit the user's specific prompt. Keep "
            "the overall structure unless the prompt asks for something "
            "fundamentally different.\n\n"
            f"```json\n{json.dumps(schematic_to_dict(template), indent=2)}\n```"
        )

    system_prompt = make_role(
        Roles.planner,
        instructions=prompt_text,
        output_rules="(emit ONLY JSON; no fences; no prose)",
        template=template_block,
    )

    last_error: str | None = None

    for attempt in range(max_attempts):
        user_msg = "Emit the Task JSON template/schematic now."
        if last_error is not None:
            user_msg = (
                f"Your previous attempt failed validation:\n{last_error}\n\n"
                f"Re-emit the corrected Task JSON template/schematic now."
            )

        try:
            picked = scheduler.pick_slot(role=Roles.planner)
        except SchedulerError as e:
            log(f"[planner] no slot available: {e!r}")
            log_event("planner_fail", attempt=attempt, reason=f"no_slot: {e!s}"[:200])
            break

        log(f"[planner] attempt {attempt + 1}/{max_attempts} via {picked.who}")
        log_event("planner_attempt",
                  slot=picked.who, provider=picked.provider.name,
                  family=picked.model_family, attempt=attempt + 1,
                  max_attempts=max_attempts,
                  has_template_hint=template is not None)

        await scheduler.wait_for_provider_pacing(picked)

        try:
            content = await call_slot(
                http_client, picked,
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
            scheduler.record_failure(picked, code, reason=err[:250])
            log(f"[planner] call failed ({code}): {err[:250]}")
            log_event("planner_call_fail",
                      slot=picked.who, provider=picked.provider.name,
                      fail_code=code, reason=err[:250])
            last_error = f"call failed: {err[:250]}"
            continue

        try:
            schematic = remove_header_footer(content)
            scheduler.record_success(picked)
            log(f"[planner] schematic accepted: "
                f"task_type={schematic.task_type}, {len(schematic.stages)} stages")
            log_event("planner_success",
                      slot=picked.who, provider=picked.provider.name,
                      family=picked.model_family,
                      task_type=schematic.task_type,
                      stage_count=len(schematic.stages),
                      fanout_stages=sum(1 for s in schematic.stages if s.fanout),
                      attempt=attempt + 1)
            return schematic
        except SchemaError as e:
            scheduler.record_failure(picked, "json", reason=str(e)[:200])
            last_error = str(e)
            log(f"[planner] schema invalid: {last_error[:200]}")
            log_event("planner_schema_fail",
                      slot=picked.who, provider=picked.provider.name,
                      reason=last_error[:300], attempt=attempt + 1)
            continue

    log(f"[planner] - {max_attempts} attempts reached. using freeform fallback")
    log_event("planner_fallback", attempts_used=max_attempts, last_error=last_error)
    return freeform_fallback(prompt_text)

def freeform_fallback(prompt_text: str) -> Task:
    return task_inspect({
        "task_type": "freeform",
        "task": prompt_text[:200],
        "stages": [{
            "name": "freeform_write",
            "role": "generator",
            "instructions": prompt_text,
        }],
        "output_rules": {"format": "markdown"},
    })

# the resolver runner.py calls per prompt

async def resolve_schematic(prompt_path: Path, scheduler: SlotScheduler,
                            http_client: httpx.AsyncClient, *,
                            save_cache: bool = True,
                            log: Callable[[str], None] = print) -> Task:

    # 1. cache
    cached = find_cached_schematic(prompt_path)
    if cached is not None:
        log(f"[resolve] using cached schematic at {prompt_path.name}.schematic.json")
        log_event("cache_hit", prompt=prompt_path.name,
                  cached_task_type=cached.task_type)
        return cached
    log_event("cache_miss", prompt=prompt_path.name)

    # read prompt + frontmatter
    prompt_text = prompt_path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(prompt_text)
    explicit_template_name = frontmatter.get("template")

    template_hint: Task | None = None

    # 2a. explicit template from frontmatter (user-selected via UI dropdown)
    if explicit_template_name and isinstance(explicit_template_name, str):
        tpl_path = _find_template_by_filename(explicit_template_name)
        if tpl_path is None:
            tpl_path = _find_template_by_type(explicit_template_name)
        if tpl_path is not None:
            template_hint = load_template(tpl_path)
            if template_hint is not None:
                log(f"[resolve] explicit template from frontmatter: {explicit_template_name}")
                log_event("template_explicit", task_type=explicit_template_name,
                          template_file=tpl_path.name)
            else:
                log(f"[resolve] explicit template {explicit_template_name} invalid, ignoring")
                log_event("template_explicit_invalid", task_type=explicit_template_name)

    # 2b. auto-classify from template keyword index (no hardcoded rules)
    if template_hint is None:
        classified = _classify_from_templates(prompt_text)
        if classified:
            tpl_path = find_template(classified)
            if tpl_path is not None:
                template_hint = load_template(tpl_path)
                if template_hint is not None:
                    log(f"[resolve] auto-classified template: {classified}")
                    log_event("template_auto_classified", task_type=classified,
                              template_file=tpl_path.name)

    # 3. plan
    schematic = await plan(
        prompt_text, scheduler, http_client,
        template=template_hint, log=log,
    )

    # 4. cache for next time. don't cache the freeform fallback.
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
