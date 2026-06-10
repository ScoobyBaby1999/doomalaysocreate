from __future__ import annotations
import argparse
import asyncio
import io
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# utf-8 console on windows so em-dashes etc. don't UnicodeEncodeError.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

import httpx

#   make backend/ resolvable for top-level modules (oplog, scheduler, providers).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import providers
from oplog import atomic_write_json, log_event, new_run_id
from scheduler import SlotScheduler

from content import checkpoint
from content.main import NetworkPauseError, RunResult, execute
from content.planner import resolve_schematic
from content.schematics import Stage, Task
from content.sources import fetch_sources, parse_frontmatter, render_sources_context
from content.roles import Roles, get_role, make_role, build_output_context

ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"
OUTPUT_DIR = ROOT / "outputs"
FAILED_DIR = OUTPUT_DIR / "_failed"
STATE_FILE = OUTPUT_DIR / "_state" / "state.json"
SUMMARY_FILE = OUTPUT_DIR / "_summary" / "summary.md"

DEFAULT_PARALLEL = 1
DEFAULT_WATCH_SLEEP_S = 300
MAX_FAILED_ATTEMPTS = 3


@dataclass
class Entry:
    stem: str
    status: str = "pending"
    #   pending  -> initial, never run
    #   running  -> in flight (stale-detected and re-picked on next pass)
    #   paused   -> network-class failure mid-run; checkpoint preserved;
    #               attempts NOT incremented; auto-resumes on next pass
    #   done     -> output written
    #   failed   -> hard failure (planner gave up, stage unrecoverable, etc.)
    attempts: int = 0
    word_count: int = 0
    providers_used: list[str] = field(default_factory=list)
    last_error: str | None = None
    last_updated: str | None = None
    last_schematic_summary: str | None = None


def load_state() -> dict[str, Entry]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    out: dict[str, Entry] = {}
    for stem, value in raw.items():
        out[stem] = Entry(**{
            f.name: value.get(f.name, getattr(Entry(stem=stem), f.name))
            for f in Entry.__dataclass_fields__.values()
        })
    return out


def save_state(state: dict[str, Entry]) -> None:
    atomic_write_json(
        STATE_FILE,
        {stem: asdict(entry) for stem, entry in state.items()},
    )


def list_prompts() -> list[Path]:
    if not PROMPTS_DIR.exists():
        return []
    return sorted(PROMPTS_DIR.glob("*.md"))


def pending(state: dict[str, Entry], prompts: list[Path], retry_failed: bool) -> list[Path]:
    out = []
    for prompt_path in prompts:
        entry = state.get(prompt_path.stem)
        if entry is None or entry.status in ("pending", "paused"):
            out.append(prompt_path)
        elif entry.status == "failed" and (retry_failed or entry.attempts < MAX_FAILED_ATTEMPTS):
            out.append(prompt_path)
        elif entry.status == "running":
            out.append(prompt_path)
    return out


def make_frontmatter(stem: str, schematic: Task, result: RunResult) -> str:
    today = time.strftime("%Y-%m-%d")
    credit_lines = []
    seen_credits = set()
    if result.credits:
        for credit in result.credits:
            credit_key = f"{credit.get('role')}:{credit.get('slot')}"
            if credit_key in seen_credits or not credit.get("slot"):
                continue
            seen_credits.add(credit_key)
            credit_lines.append(f"- {credit.get('role')} via {credit.get('slot')}")
    else:
        for rec in result.stages:
            if not rec.ok or not rec.slot_id:
                continue
            credit_key = f"{rec.role.value}:{rec.slot_id}"
            if credit_key in seen_credits:
                continue
            seen_credits.add(credit_key)
            credit_lines.append(f"- {rec.role.value} via {rec.slot_id}")
    if not credit_lines:
        credit_lines = ["- orchestrator pipeline"]

    return (
        "---\n"
        f"keywords:\n- {schematic.task_type}\n- {stem}\n"
        f"task_type: {schematic.task_type}\n"
        f"task: {schematic.task}\n"
        "status: active\n"
        f"created: '{today}'\n"
        f"updated: '{today}'\n"
        "credits:\n"
        + "\n".join(credit_lines) + "\n"
        f"desc: \"Auto-generated {schematic.task_type} from {stem}.\"\n"
        "---\n\n"
    )


async def _discover_and_fetch_urls(
    schematic: Task,
    scheduler: SlotScheduler,
    client: httpx.AsyncClient,
    prompt_text: str,
    stem: str,
    log,
) -> list[dict]:
    """Auto-discover source URLs via the source_discovery stage, then fetch
    them with trafilatura. Called when frontmatter has no seed_urls but the
    schematic has a source_discovery stage.

    This runs the source_discovery planner LLM call once, extracts URLs from
    its JSON output, fetches them all concurrently, and returns the fetched
    sources dict list (same format as fetch_sources).
    """
    # find the source_discovery stage in the schematic
    sd_stage = None
    for stage in schematic.stages:
        if stage.name == "source_discovery":
            sd_stage = stage
            break
    if sd_stage is None:
        return []

    log(f"[sources] no seed_urls in frontmatter; auto-discovering via source_discovery stage")
    log_event("source_discovery_start", stem=stem)

    # build the system prompt for the source_discovery stage
    # we use "prompt" as the only input since outline hasn't run yet
    output_rules = build_output_context(schematic.output_rules)
    system_prompt = make_role(
        sd_stage.role,
        instructions=sd_stage.instructions,
        output_rules=output_rules,
        inputs=prompt_text,
    )
    max_tokens = sd_stage.max_tokens or 4000

    # call the LLM once (no fanout yet — outline hasn't produced topics)
    from scheduler import call_slot
    picked = scheduler.pick_slot(role=sd_stage.role, exclude_providers=set())
    await scheduler.wait_for_provider_pacing(picked)

    try:
        raw_output = await call_slot(
            client, picked,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt_text},
            ],
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        log(f"[sources] source_discovery LLM call failed: {e}")
        log_event("source_discovery_fail", stem=stem, reason=str(e)[:300])
        return []

    log(f"[sources] source_discovery output via {picked.who} ({len(raw_output)} chars)")

    # parse the LLM output to extract URLs
    # the output is a JSON array of {url, title, relevance} objects
    try:
        # strip markdown fences if present
        stripped = raw_output.strip()
        if stripped.startswith("```"):
            first_nl = stripped.find("\n")
            last_fence = stripped.rfind("```")
            if first_nl != -1 and last_fence > first_nl:
                stripped = stripped[first_nl + 1:last_fence].strip()
        proposed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError) as e:
        log(f"[sources] source_discovery output unparseable: {e}")
        log_event("source_discovery_parse_fail", stem=stem, reason=str(e)[:200])
        return []

    # normalize: could be a list of dicts or a dict with nested lists
    url_entries = []
    if isinstance(proposed, list):
        url_entries = proposed
    elif isinstance(proposed, dict):
        # try common keys
        for key in ("sources", "urls", "urls_list", "discovered", "articles"):
            if key in proposed and isinstance(proposed[key], list):
                url_entries = proposed[key]
                break
        if not url_entries and "url" in proposed:
            url_entries = [proposed]

    # extract unique URLs
    discovered_urls: list[str] = []
    seen: set[str] = set()
    for entry in url_entries:
        if isinstance(entry, dict):
            url = entry.get("url", "")
        elif isinstance(entry, str):
            url = entry
        else:
            continue
        if url and url.startswith("http") and url not in seen:
            seen.add(url)
            discovered_urls.append(url)

    if not discovered_urls:
        log(f"[sources] source_discovery produced no valid URLs")
        log_event("source_discovery_empty", stem=stem)
        return []

    log(f"[sources] discovered {len(discovered_urls)} unique URLs, fetching...")
    log_event("source_discovery_urls", stem=stem, count=len(discovered_urls))

    # fetch them with trafilatura (same pipeline as seed_urls)
    sources_cache = OUTPUT_DIR / "_state" / f"{stem}_discovered_sources.json"
    fetched = await fetch_sources(client, discovered_urls, cache_path=sources_cache)

    log(f"[sources] auto-discovered: fetched {len(fetched)}/{len(discovered_urls)} sources")
    log_event("source_discovery_done", stem=stem, ok=len(fetched), total=len(discovered_urls))

    return fetched


async def process_one(client: httpx.AsyncClient, scheduler: SlotScheduler,
                      prompt_path: Path, state: dict[str, Entry], *, dry: bool) -> None:

    stem = prompt_path.stem
    entry = state.setdefault(stem, Entry(stem=stem))
    if not dry:
        entry.status = "running"
        entry.attempts += 1
        entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)

    log_prefix = f"[{stem}]"

    def log(msg: str) -> None:
        print(f"{log_prefix} {msg}", flush=True)

    log_event("prompt_start", stem=stem, attempt=entry.attempts, dry=dry)

    schematic = await resolve_schematic(
        prompt_path, scheduler, client,
        save_cache=not dry,
        log=log,
    )
    entry.last_schematic_summary = f"{schematic.task_type} ({len(schematic.stages)} stages)"

    if dry:
        log(f"DRY: would execute {entry.last_schematic_summary}; exiting per --dry")
        for s in schematic.stages:
            fanout = f" fanout={s.fanout.over}" if s.fanout else ""
            log(f"  - {s.name} (role={s.role.value}){fanout}")
        log_event("prompt_dry_end", stem=stem,
                  schematic_summary=entry.last_schematic_summary)
        entry.status = "pending"
        save_state(state)
        return

    prompt_text = prompt_path.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(prompt_text)
    seed_urls: list[str] = frontmatter.get("seed_urls") or []
    initial_context: dict = {"prompt": prompt_text}

    requires_sources = any(
        "fetched_sources" in stage.inputs
        for stage in schematic.stages
    )

    fetched: list[dict] = []

    if seed_urls:
        # user provided seed_urls in frontmatter — fetch them directly
        sources_cache = OUTPUT_DIR / "_state" / f"{stem}_sources.json"
        fetched = await fetch_sources(client, seed_urls, cache_path=sources_cache)
        if not fetched and requires_sources:
            entry.status = "failed"
            entry.last_error = (
                f"{schematic.task_type} requires seed_urls but 0 of {len(seed_urls)} "
                f"url(s) were accessible. add valid seed_urls to the prompt frontmatter."
            )
            entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            save_state(state)
            log(f"FAIL: {entry.last_error}")
            log_event("prompt_fail", stem=stem, error=entry.last_error)
            return
        log(f"[sources] fetched {len(fetched)}/{len(seed_urls)} sources")
        log_event("sources_ready", stem=stem, ok=len(fetched), total=len(seed_urls))

    elif requires_sources:
        # no seed_urls in frontmatter, but the template needs sources.
        # check if the schematic has a source_discovery stage we can use.
        has_discovery = any(s.name == "source_discovery" for s in schematic.stages)
        if has_discovery:
            fetched = await _discover_and_fetch_urls(
                schematic, scheduler, client, prompt_text, stem, log,
            )
            if not fetched:
                log(f"[sources] warning: source_discovery found but produced no accessible URLs")
                log_event("sources_empty_discovery", stem=stem)
                # don't fail — stages will write without citations
            else:
                log_event("sources_ready_discovered", stem=stem, ok=len(fetched))
        else:
            log(f"[sources] warning: template requires sources but no seed_urls or source_discovery stage")
            log_event("sources_no_path", stem=stem)

    # inject fetched sources into the initial context for the orchestrator
    if fetched:
        initial_context["fetched_sources"] = render_sources_context(fetched)
        initial_context["discovered_urls_count"] = len(fetched)

    try:
        result: RunResult = await execute(
            schematic, scheduler, client,
            stem=stem,
            initial_context=initial_context,
            log=log,
        )
    except NetworkPauseError as pause_error:
        entry.attempts = max(0, entry.attempts - 1)
        entry.status = "paused"
        entry.last_error = f"paused: {pause_error!s}"[:500]
        entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_state(state)
        log(f"PAUSED: {entry.last_error}")
        log_event("prompt_paused", stem=stem, reason=str(pause_error)[:500])
        return

    entry.providers_used = result.providers_used

    if result.ok and result.body is not None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / f"{stem}.md"
        out_path.write_text(
            make_frontmatter(stem, schematic, result) + result.body,
            encoding="utf-8",
        )
        entry.status = "done"
        entry.word_count = len(result.body.split())
        entry.last_error = None
        log(f"OK wrote {out_path.name} ({entry.word_count} wc)")
        log_event("prompt_done",
                  stem=stem, word_count=entry.word_count,
                  providers_used=entry.providers_used,
                  stage_count=len(result.stages),
                  task_type=schematic.task_type)
    else:
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        if result.body is not None:
            shelf = FAILED_DIR / f"{stem}.attempt{entry.attempts}.md"
            shelf.write_text(result.body, encoding="utf-8")
            entry.word_count = len(result.body.split())
        entry.status = "failed"
        entry.last_error = result.error or "unknown failure"
        log(f"FAIL: {entry.last_error}")
        log_event("prompt_fail",
                  stem=stem, error=entry.last_error[:300],
                  partial_words=entry.word_count)

    entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_state(state)


def print_status(state: dict[str, Entry]) -> None:
    if not state:
        print("(no entries)")
        return
    print(f"{'stem':40} {'status':10} {'att':>3} {'wc':>6}  last_err")
    print("-" * 80)
    for stem in sorted(state):
        entry = state[stem]
        err = (entry.last_error or "")[:40]
        print(f"{stem:40} {entry.status:10} {entry.attempts:>3} "
              f"{entry.word_count:>6}  {err}")


def write_summary(state: dict[str, Entry]) -> None:
    SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# orchestrator run summary",
        "",
        f"generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
        "| stem | status | attempts | word count | task_type | last error |",
        "|------|--------|----------|------------|-----------|------------|",
    ]
    for stem in sorted(state):
        entry = state[stem]
        err = (entry.last_error or "").replace("|", "\\|")[:80]
        task_summary = (entry.last_schematic_summary or "?").split(" ", 1)[0]
        lines.append(
            f"| {stem} | {entry.status} | {entry.attempts} | "
            f"{entry.word_count} | {task_summary} | {err} |"
        )
    SUMMARY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_once(args: argparse.Namespace) -> int:
    if not PROMPTS_DIR.exists():
        print(f"no prompts dir at {PROMPTS_DIR}", file=sys.stderr)
        return 2

    state = load_state()
    prompts = list_prompts()
    if args.only:
        prompts = [p for p in prompts if p.stem == args.only or p.stem.startswith(args.only)]
        if not prompts:
            print(f"no prompt matches --only={args.only}", file=sys.stderr)
            return 2
    else:
        prompts = pending(state, prompts, args.retry_failed)

    if not prompts:
        print("nothing to do - all entries done or failed past retry cap")
        print_status(state)
        return 0

    run_id = new_run_id()
    print(f"processing {len(prompts)} prompt(s); dry={args.dry}; run_id={run_id}")

    provs = providers.make_provider_registry()
    slots = providers.make_slot_registry(provs)
    if not slots:
        print("no slots! check .env for at least OPENROUTER_API_KEY", file=sys.stderr)
        return 2
    print(f"loaded {len(provs)} providers, {len(slots)} slots")
    log_event("run_start",
              provider_count=len(provs), slot_count=len(slots),
              providers=[p.name for p in provs],
              prompt_count=len(prompts), dry=args.dry, parallel=args.parallel)
    scheduler = SlotScheduler(slots)

    sem = asyncio.Semaphore(args.parallel)

    async with httpx.AsyncClient(timeout=600.0) as client:

        async def guarded(prompt_path: Path) -> None:
            async with sem:
                try:
                    await process_one(client, scheduler, prompt_path, state, dry=args.dry)
                except Exception as e:
                    entry = state.setdefault(prompt_path.stem, Entry(stem=prompt_path.stem))
                    entry.status = "failed"
                    entry.last_error = f"runner exception: {e!r}"
                    save_state(state)
                    print(f"[ERR] {prompt_path.stem}: {e!r}", flush=True)

        await asyncio.gather(*[guarded(p) for p in prompts])

    if not args.dry:
        write_summary(state)
    print_status(state)
    return 0


async def main_async(args: argparse.Namespace) -> int:
    if args.watch:
        while True:
            code = await run_once(args)
            if code != 0:
                return code
            state = load_state()
            if all(
                entry.status == "done"
                or (entry.status == "failed" and entry.attempts >= MAX_FAILED_ATTEMPTS)
                for entry in state.values()
            ):
                print("all prompts settled; exiting watch loop")
                return 0
            print(f"sleeping {args.watch_sleep}s before next pass")
            await asyncio.sleep(args.watch_sleep)
    return await run_once(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="autonomous task orchestrator")
    parser.add_argument("--only", default=None,
                        help="run a single prompt by stem (or prefix)")
    parser.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL,
                        help="concurrent prompts (free tier doesn't like >1)")
    parser.add_argument("--dry", action="store_true",
                        help="plan only; no execution. schematic NOT cached.")
    parser.add_argument("--watch", action="store_true",
                        help="loop: run a pass, sleep, run again until all settled")
    parser.add_argument("--watch-sleep", type=int, default=DEFAULT_WATCH_SLEEP_S)
    parser.add_argument("--retry-failed", action="store_true",
                        help="retry shelved prompts even past MAX_FAILED_ATTEMPTS")
    parser.add_argument("--status", action="store_true",
                        help="print state table and exit")
    args = parser.parse_args()

    if args.status:
        print_status(load_state())
        return 0

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
