#!/usr/bin/env python3
"""
conductor.py - Orchestrates the Replay research loop across many prompts.

- Reads prompts from vault/research/_prompts/*.md
- Runs the Replay pipeline on each (draft -> critique -> revise)
- Persists state in vault/research/_state.json for safe resume
- Writes successful output to vault/research/<stem>.md
- Shelves failed drafts to vault/research/_failed/<stem>.md
- Writes a rolling _summary.md scorecard

Resume model: kill and restart at will. Entries marked `done` are skipped.
Failed entries can be retried by deleting them from the state file, or using
`--retry-failed`.

Modes:
  python scripts/conductor.py                      # one pass, exit
  python scripts/conductor.py --watch              # run once, sleep, repeat
  python scripts/conductor.py --only 01_transformer_internals
  python scripts/conductor.py --dry                # no network calls
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time

# Force UTF-8 on stdout/stderr so em-dashes and other non-cp1252 chars in
# validator messages don't UnicodeEncodeError on Windows consoles.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from providers import build_slots, load_registry
from replay import (
    ReplayResult,
    make_frontmatter,
    run_replay,
)
from scheduler import SlotScheduler

ROOT = Path(__file__).resolve().parent.parent

# Load .env if present — same shape as the original research_runner loader.
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

PROMPTS_DIR = ROOT / "vault" / "research" / "_prompts"
OUTPUT_DIR = ROOT / "vault" / "research"
FAILED_DIR = OUTPUT_DIR / "_failed"
STATE_FILE = OUTPUT_DIR / "_state.json"
SUMMARY_FILE = OUTPUT_DIR / "_summary.md"

DEFAULT_PARALLEL = 1          # free tier hates bursts; 1 = sequential
DEFAULT_MAX_ROUNDS = 3
DEFAULT_WATCH_SLEEP_S = 300   # between passes in --watch mode
MAX_FAILED_ATTEMPTS = 3       # give up after this many full-pipeline failures


# ---- State ----

@dataclass
class Entry:
    stem: str
    status: str = "pending"         # pending | running | done | failed
    attempts: int = 0
    rounds: int = 0
    word_count: int = 0
    last_error: str | None = None
    last_updated: str | None = None


def _load_state() -> dict[str, Entry]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {k: Entry(**v) for k, v in raw.items()}


def _save_state(state: dict[str, Entry]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({k: asdict(v) for k, v in state.items()}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


# ---- Work selection ----

def _list_prompts() -> list[Path]:
    return sorted(PROMPTS_DIR.glob("*.md"))


def _pending(state: dict[str, Entry], prompts: list[Path], retry_failed: bool) -> list[Path]:
    out = []
    for p in prompts:
        entry = state.get(p.stem)
        if entry is None or entry.status == "pending":
            out.append(p)
        elif entry.status == "failed" and (retry_failed or entry.attempts < MAX_FAILED_ATTEMPTS):
            out.append(p)
        elif entry.status == "running":
            # Stale running marker from a killed conductor — retry.
            out.append(p)
    return out


# ---- Processing ----

async def _process_one(
    client: httpx.AsyncClient,
    prompt_path: Path,
    state: dict[str, Entry],
    *,
    models: StageModels,
    max_rounds: int,
    dry: bool,
    check_urls: bool,
) -> None:
    stem = prompt_path.stem
    entry = state.setdefault(stem, Entry(stem=stem))
    if not dry:
        entry.status = "running"
        entry.attempts += 1
        entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_state(state)

    result: ReplayResult = await run_replay(
        client, prompt_path,
        models=models,
        max_rounds=max_rounds,
        check_urls=check_urls,
        dry=dry,
        log=lambda m: print(f"  {m}", flush=True),
    )
    entry.rounds = result.rounds

    if dry:
        entry.status = "pending"  # don't mark done from a dry run
        entry.last_error = None
        _save_state(state)
        return

    if result.ok and result.final_body:
        out_path = OUTPUT_DIR / f"{stem}.md"
        out_path.write_text(
            make_frontmatter(stem, result.stages) + result.final_body,
            encoding="utf-8",
        )
        entry.status = "done"
        entry.word_count = len(result.final_body.split())
        entry.last_error = None
        print(f"[OK ] {stem}: wrote {out_path.name} "
              f"({entry.word_count} wc, {entry.rounds} rounds)", flush=True)
    else:
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        if result.final_body:
            shelf = FAILED_DIR / f"{stem}.attempt{entry.attempts}.md"
            shelf.write_text(result.final_body, encoding="utf-8")
        entry.status = "failed"
        entry.last_error = result.error or "unknown failure"
        if result.final_body:
            entry.word_count = len(result.final_body.split())
        print(f"[ERR] {stem}: {entry.last_error}", flush=True)

    entry.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_state(state)


# ---- Status / summary ----

def _print_status(state: dict[str, Entry]) -> None:
    if not state:
        print("(no entries)")
        return
    print(f"{'stem':40} {'status':10} {'att':>3} {'rnd':>3} {'wc':>6}  last_err")
    print("-" * 100)
    for stem in sorted(state):
        e = state[stem]
        err = (e.last_error or "")[:40]
        print(f"{stem:40} {e.status:10} {e.attempts:>3} {e.rounds:>3} {e.word_count:>6}  {err}")


def _write_summary(state: dict[str, Entry]) -> None:
    lines = ["# Replay run summary", "",
             f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}", "",
             "| stem | status | attempts | rounds | word count | last error |",
             "|------|--------|----------|--------|------------|------------|"]
    for stem in sorted(state):
        e = state[stem]
        err = (e.last_error or "").replace("|", "\\|")[:80]
        lines.append(f"| {stem} | {e.status} | {e.attempts} | {e.rounds} | {e.word_count} | {err} |")
    SUMMARY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- Main loop ----

async def run_once(args: argparse.Namespace) -> int:
    if not PROMPTS_DIR.exists():
        print(f"no prompts dir at {PROMPTS_DIR}", file=sys.stderr)
        return 2

    state = _load_state()
    prompts = _list_prompts()
    if args.only:
        prompts = [p for p in prompts if p.stem == args.only]
        if not prompts:
            print(f"no prompt matches --only={args.only}", file=sys.stderr)
            return 2
    else:
        prompts = _pending(state, prompts, args.retry_failed)

    if not prompts:
        print("nothing to do — all entries done or failed past retry cap")
        _print_status(state)
        return 0

    print(f"processing {len(prompts)} prompt(s) with parallel={args.parallel}")
    models = StageModels()
    sem = asyncio.Semaphore(args.parallel)

    async with httpx.AsyncClient() as client:
        async def guarded(p: Path) -> None:
            async with sem:
                try:
                    await _process_one(
                        client, p, state,
                        models=models,
                        max_rounds=args.max_rounds,
                        dry=args.dry,
                        check_urls=not args.no_url_check,
                    )
                except Exception as e:  # noqa: BLE001 - top-level guard
                    entry = state.setdefault(p.stem, Entry(stem=p.stem))
                    entry.status = "failed"
                    entry.last_error = f"conductor exception: {e!r}"
                    _save_state(state)
                    print(f"[ERR] {p.stem}: {e!r}", flush=True)

        await asyncio.gather(*[guarded(p) for p in prompts])

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
            if all(e.status == "done" or (e.status == "failed"
                                          and e.attempts >= MAX_FAILED_ATTEMPTS)
                   for e in state.values()):
                print("all prompts settled; exiting watch loop")
                return 0
            print(f"sleeping {args.watch_sleep}s before next pass")
            await asyncio.sleep(args.watch_sleep)
    return await run_once(args)


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay research conductor")
    ap.add_argument("--only", default=None, help="Run a single prompt by stem")
    ap.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL)
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                    help="Max critique/revise rounds per prompt")
    ap.add_argument("--dry", action="store_true", help="No network calls; print plan only")
    ap.add_argument("--watch", action="store_true",
                    help="Loop: run a pass, sleep, run again until all settled")
    ap.add_argument("--watch-sleep", type=int, default=DEFAULT_WATCH_SLEEP_S)
    ap.add_argument("--no-url-check", action="store_true",
                    help="Skip live HEAD checks on URLs (useful offline / for speed)")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Retry failed entries even past MAX_FAILED_ATTEMPTS")
    ap.add_argument("--status", action="store_true",
                    help="Print state table and exit")
    args = ap.parse_args()

    if args.status:
        _print_status(_load_state())
        return 0

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
