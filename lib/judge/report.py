"""
judge/report.py - The shared JudgeReport dataclass and RunContext.

Lives in its own module to avoid circular imports between engine.py,
plugins.py, llm_judge.py — they all return JudgeReport but none of
them import from each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx


@dataclass
class JudgeReport:
    """Aggregated verdict from one or more Judge layers.

    Two severity buckets:
      hard_fails   blocking issues. If non-empty AND round < max_rounds,
                   the orchestrator injects (reviewer, transformer)
                   onto the queue and re-runs Judge after the retry.
      soft_flags   advisory. Fed into the next reviewer stage's user
                   message so the critique knows what to target, but
                   do not trigger retries on their own.

    Layer 1 (rules) and Layer 2 (plugins) write hard_fails by default.
    Layer 3 (LLM judges) writes soft_flags only — fuzzy verdicts can't
    single-handedly reject a draft.

    `ok` is true iff hard_fails is empty.
    """
    hard_fails: list[str] = field(default_factory=list)
    soft_flags: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard_fails

    def merge(self, other: "JudgeReport") -> None:
        """Append another report's findings to this one. In-place."""
        self.hard_fails.extend(other.hard_fails)
        self.soft_flags.extend(other.soft_flags)

    def to_critique_feed(self) -> str:
        """Render as markdown suitable for pasting into a reviewer prompt.

        Reviewer stages get this as part of their user message so they
        know exactly what to target. Empty findings → "(no issues)".
        """
        lines: list[str] = []
        if self.hard_fails:
            lines.append("## Hard failures (must fix)")
            lines.extend(f"- {h}" for h in self.hard_fails)
        if self.soft_flags:
            if lines:
                lines.append("")
            lines.append("## Soft flags (consider addressing)")
            lines.extend(f"- {s}" for s in self.soft_flags)
        return "\n".join(lines) if lines else "(no issues)"


@dataclass
class RunContext:
    """Per-evaluate() shared resources passed to plugins.

    Plugins need an HTTP client (URL fetches) and the schematic
    (so they can read output_rules.format etc). RunContext bundles
    both so plugin signatures stay short.
    """
    http_client: "httpx.AsyncClient"
    schematic: Any  # avoid importing TaskSchematic to break cycles
