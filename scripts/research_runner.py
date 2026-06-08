#!/usr/bin/env python3
"""
research_runner.py - Thin CLI shim over the Replay conductor.

The old single-shot runner has been replaced by Replay, a multi-stage loop
(draft -> validate -> critique -> revise) that pushes free models toward the
quality of an Opus draft. Preserves the original flags for muscle memory.

Entry point of record: scripts/conductor.py.
This shim just forwards arguments.

Usage:
    python scripts/research_runner.py                     # run once
    python scripts/research_runner.py --watch             # run-sleep-loop
    python scripts/research_runner.py --only 07_markdown_frontmatter_alternatives
    python scripts/research_runner.py --dry               # plan only, no net
"""

from __future__ import annotations

import sys

import conductor


def main() -> int:
    # Translate legacy flags:
    # --provider ollama|openrouter was from the old runner; the new pipeline is
    # OpenRouter-only at the moment (free models there cover the three stages).
    # Drop silently so old muscle memory doesn't error.
    filtered = []
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == "--provider":
            skip_next = True
            continue
        if arg.startswith("--provider="):
            continue
        if arg == "--model":
            skip_next = True
            continue
        if arg.startswith("--model="):
            continue
        filtered.append(arg)
    sys.argv = [sys.argv[0]] + filtered
    return conductor.main()


if __name__ == "__main__":
    sys.exit(main())
