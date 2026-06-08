#!/usr/bin/env python3
"""
runner.py - CLI entry for the general autonomous task orchestrator.

# Usage

  python scripts/orchestrator/runner.py                       # run all pending prompts
  python scripts/orchestrator/runner.py --only 03_vector...   # one prompt by stem
  python scripts/orchestrator/runner.py --dry                 # dry plan, no LLM calls
  python scripts/orchestrator/runner.py --watch               # loop, sleep, repeat
  python scripts/orchestrator/runner.py --retry-failed        # re-run shelved prompts
  python scripts/orchestrator/runner.py --status              # show state table
  python scripts/orchestrator/runner.py --committee 3         # bob_the_builders fanout

# What this module does

  1. Discover prompts in vault/research/_prompts/*.md
  2. Load _state.json (per-prompt status: pending|running|done|failed)
  3. For each pending prompt:
       a. Resolve TaskSchematic via planner.resolve_schematic()
       b. Execute via orchestrator.execute()
       c. Write final body to vault/research/<stem>.md (or
          vault/research/_failed/<stem>.attempt<N>.md on failure)
       d. Update _state.json
  4. Write a rolling _summary.md scorecard

# Coexistence with old conductor.py

This is parallel infrastructure. The old `conductor.py` and `replay.py`
are unchanged and still work. Run whichever you prefer per task; new
runner.py is the one we develop further.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# UTF-8 console on Windows so em-dashes etc don't UnicodeEncodeError.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

import httpx

# scripts/ is on PYTHONPATH when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers
from oplog import clear_prompt_stem, log_event, new_run_id, set_prompt_stem
from scheduler import SlotScheduler

from orchestrator import TaskSchematic
from orchestrator.orchestrator import RunResult, execute
from orchestrator.planner import resolve_schematic


ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = ROOT / "vault" / "research" / "_prompts"
OUTPUT_DIR = ROOT / "vault" / "research"
FAILED_DIR = OUTPUT_DIR / "_failed"
STATE_FILE = OUTPUT_DIR / "_state.json"
SUMMARY_FILE = OUTPUT_DIR / "_summary.md"

DEFAULT_PARALLEL = 1            # free-tier doesn't like concurrent prompt work
DEFAULT_WATCH_SLEEP_S = 300
MAX_FAILED_ATTEMPTS = 3


# ---- State ---------------------------------------------------------

@dataclass
class Entry:
    """One prompt's persisted state. Survives kill+restart cycles."""
    stem: str
    status: str = "pending"      # pending | running | done | failed
    attempts: int = 0
    rounds: int = 0
    word_count: int = 0
    decompositions: int = 0
    providers_used: list[str] = field(default_factory=list)
    last_error: str | None = None
    last_updated: str | None = None
    last_schematic_summary: str | None = None  # task_type + stage count


def _load_state() -> dict[str, Entry]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    out: dict[str, Entry] = {}
    for k, v in raw.items():
        # Backward-compat: tolerate old state files missing new fields.
        out[k] = Entry(**{f.name: v.get(f.name, getattr(Entry(stem=k), f.name))
                          for f in Entry.__dataclass_fields__.values()})
    return out


def _save_state(state: dict[str, Entry]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({k: asdict(v) for k, v in state.items()}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


# ---- Work selection ------------------------------------------------

def _list_prompts() -> list[Path]:
    if not PROMPTS_DIR.exists():
        return []
    return sorted(PROMPTS_DIR.glob("*.md"))


def _pending(state: dict[str, Entry], prompts: list[Path], retry_failed: bool) -> list[Path]:
    """Filter prompts to those needing processing."""
    out = []
    for p in prompts:
        entry = state.get(p.stem)
        if entry is None or entry.status == "pending":
            out.append(p)
        elif entry.status == "failed" and (
            retry_failed or entry.attempts < MAX_FAILED_ATTEMPTS
        ):
            out.append(p)
        elif entry.status == "running":
            # Stale running marker from a killed run — retry.
            out.append(p)
    return out


# ---- Output writing ------------------------------------------------

def _make_frontmatter(stem: str, schematic: TaskSchematic, result: RunResult) -> str:
    """Build YAML frontmatter for the output file.

    Credits each stage's (provider/model). One bullet per stage record so
    a human can audit which carrier wrote each piece.
    """
    today = time.strftime("%Y-%m-%d")
    credit_lines = []
    seen_credits = set()
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
        f"rounds: {result.rounds}\n"
        f"decompositions: {result.decompositions}\n"
        "links:\n  hard: []\n  soft: []\n"
        f"desc: \"Auto-generated {schematic.task_type} from {stem}.\"\n"
        "---\n\n"
    )


# ---- Per-prompt processing -----------------------------------------

async def _process_one(
    client: httpx.AsyncClient,
    scheduler: SlotScheduler,
    prompt_path: Path,
    state: dict[str, Entry],
    *,
    committee_size: int,
    dry: bool,
) -> None:
    """Plan + execute one prompt. Persists state after every milestone."""
    stem = prompt_path.stem
    set_prompt_stem(stem)
    entry = state.setdefault(stem, Entry(stem=stem))
    if not dry:
        entry.status = "running"
        entry.attempts += 1
        entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_state(state)

    log_prefix = f"[{stem}]"

    def log(msg: str) -> None:
        print(f"{log_prefix} {msg}", flush=True)

    log_event("prompt_start", stem=stem, attempt=entry.attempts, dry=dry)

    # 1. Resolve schematic. Don't write cache during --dry runs (the
    # plan we get is for inspection only; we'd be polluting the cache
    # with whatever the planner happened to emit one time).
    schematic = await resolve_schematic(
        prompt_path, scheduler, client,
        committee_size=committee_size,
        save_cache=not dry,
        log=log,
    )
    entry.last_schematic_summary = (
        f"{schematic.task_type} ({len(schematic.stages)} stages)"
    )
    if dry:
        log(f"DRY: would execute {entry.last_schematic_summary}; exiting per --dry")
        for s in schematic.stages:
            fanout = f" fanout={s.fanout.over}" if s.fanout else ""
            log(f"  - {s.name} (role={s.role.value}){fanout}")
        log_event("prompt_dry_end", stem=stem,
                  schematic_summary=entry.last_schematic_summary)
        entry.status = "pending"
        _save_state(state)
        clear_prompt_stem()
        return

    # 2. Execute.
    initial_context = {
        "prompt": prompt_path.read_text(encoding="utf-8"),
    }
    result: RunResult = await execute(
        schematic, scheduler, client,
        initial_context=initial_context,
        log=log,
    )

    entry.rounds = result.rounds
    entry.decompositions = result.decompositions
    entry.providers_used = result.providers_used

    # 3. Write output.
    if result.ok and result.body is not None:
        out_path = OUTPUT_DIR / f"{stem}.md"
        out_path.write_text(
            _make_frontmatter(stem, schematic, result) + result.body,
            encoding="utf-8",
        )
        entry.status = "done"
        entry.word_count = len(result.body.split())
        entry.last_error = None
        log(f"OK wrote {out_path.name} ({entry.word_count} wc, "
            f"{entry.rounds} rounds, {entry.decompositions} decompositions)")
        log_event("prompt_done",
                  stem=stem, word_count=entry.word_count,
                  rounds=entry.rounds, decompositions=entry.decompositions,
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
                  rounds=entry.rounds, decompositions=entry.decompositions,
                  partial_words=entry.word_count)

    entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_state(state)
    clear_prompt_stem()


# ---- Status / summary ---------------------------------------------

def _print_status(state: dict[str, Entry]) -> None:
    if not state:
        print("(no entries)")
        return
    print(f"{'stem':40} {'status':10} {'att':>3} {'rnd':>3} {'wc':>6}  last_err")
    print("-" * 100)
    for stem in sorted(state):
        e = state[stem]
        err = (e.last_error or "")[:40]
        print(f"{stem:40} {e.status:10} {e.attempts:>3} {e.rounds:>3} "
              f"{e.word_count:>6}  {err}")


def _write_summary(state: dict[str, Entry]) -> None:
    lines = [
        "# Orchestrator run summary",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
        "| stem | status | attempts | rounds | decomp | word count | task_type | last error |",
        "|------|--------|----------|--------|--------|------------|-----------|------------|",
    ]
    for stem in sorted(state):
        e = state[stem]
        err = (e.last_error or "").replace("|", "\\|")[:80]
        task_summary = (e.last_schematic_summary or "?").split(" ", 1)[0]
        lines.append(
            f"| {stem} | {e.status} | {e.attempts} | {e.rounds} | "
            f"{e.decompositions} | {e.word_count} | {task_summary} | {err} |"
        )
    SUMMARY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- Main loop ----------------------------------------------------

async def run_once(args: argparse.Namespace) -> int:
    """One pass through pending prompts. Returns shell exit code."""
    if not PROMPTS_DIR.exists():
        print(f"no prompts dir at {PROMPTS_DIR}", file=sys.stderr)
        return 2

    state = _load_state()
    prompts = _list_prompts()
    if args.only:
        prompts = [p for p in prompts if p.stem == args.only or p.stem.startswith(args.only)]
        if not prompts:
            print(f"no prompt matches --only={args.only}", file=sys.stderr)
            return 2
    else:
        prompts = _pending(state, prompts, args.retry_failed)

    if not prompts:
        print("nothing to do — all entries done or failed past retry cap")
        _print_status(state)
        return 0

    run_id = new_run_id()
    print(f"processing {len(prompts)} prompt(s); committee={args.committee}; "
          f"dry={args.dry}; run_id={run_id}")

    # Build the scheduler ONCE for the whole pass — cooldowns and family
    # scores accumulate across prompts within a session.
    provs = providers.load_registry()
    slots = providers.build_slots(provs)
    if not slots:
        print("no slots! check .env for at least OPENROUTER_API_KEY", file=sys.stderr)
        return 2
    print(f"loaded {len(provs)} providers, {len(slots)} slots")
    log_event("run_start",
              provider_count=len(provs),
              slot_count=len(slots),
              providers=[p.name for p in provs],
              prompt_count=len(prompts),
              committee=args.committee,
              dry=args.dry,
              parallel=args.parallel)
    scheduler = SlotScheduler(slots)

    sem = asyncio.Semaphore(args.parallel)

    async with httpx.AsyncClient(timeout=600.0) as client:
        async def guarded(p: Path) -> None:
            async with sem:
                try:
                    await _process_one(
                        client, scheduler, p, state,
                        committee_size=args.committee,
                        dry=args.dry,
                    )
                except Exception as e:  # noqa: BLE001 - top-level guard
                    entry = state.setdefault(p.stem, Entry(stem=p.stem))
                    entry.status = "failed"
                    entry.last_error = f"runner exception: {e!r}"
                    _save_state(state)
                    print(f"[ERR] {p.stem}: {e!r}", flush=True)

        await asyncio.gather(*[guarded(p) for p in prompts])

    if not args.dry:
        _write_summary(state)
    _print_status(state)
    return 0


async def main_async(args: argparse.Namespace) -> int:
    if args.watch:
        while True:
            code = await run_once(args)
            if code != 0:
                return code
            state = _load_state()
            if all(
                e.status == "done"
                or (e.status == "failed" and e.attempts >= MAX_FAILED_ATTEMPTS)
                for e in state.values()
            ):
                print("all prompts settled; exiting watch loop")
                return 0
            print(f"sleeping {args.watch_sleep}s before next pass")
            await asyncio.sleep(args.watch_sleep)
    return await run_once(args)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="General autonomous task orchestrator (replaces conductor.py)"
    )
    ap.add_argument("--only", default=None,
                    help="Run a single prompt by stem (or prefix)")
    ap.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL,
                    help="Concurrent prompts (free tier doesn't like >1)")
    ap.add_argument("--committee", type=int, default=1,
                    help="bob_the_builders committee size for the Planner (default 1)")
    ap.add_argument("--dry", action="store_true",
                    help="Plan only; no execution. Schematic NOT cached.")
    ap.add_argument("--watch", action="store_true",
                    help="Loop: run a pass, sleep, run again until all settled")
    ap.add_argument("--watch-sleep", type=int, default=DEFAULT_WATCH_SLEEP_S)
    ap.add_argument("--retry-failed", action="store_true",
                    help="Retry shelved prompts even past MAX_FAILED_ATTEMPTS")
    ap.add_argument("--status", action="store_true",
                    help="Print state table and exit")
    args = ap.parse_args()

    if args.status:
        _print_status(_load_state())
        return 0

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
