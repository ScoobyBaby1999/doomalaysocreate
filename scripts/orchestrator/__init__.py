"""
orchestrator/ - The general autonomous task orchestrator package.

Layout:
  schematic.py      TaskSchematic, StageDef, FanoutSpec dataclasses + parser.
  roles.py          Role enum, skeleton loader, per-role merge semantics.
  planner.py        plan(), bob_the_builders(), template-hint resolution.
  orchestrator.py   Stage loop with fanout, judge integration, decomposition.
  runner.py         CLI entry; reads prompts, drives the pipeline, writes out.
  prompts/          Role skeleton system prompts (role_*.md, synthesizer.md).
  templates/        Shipped TaskSchematic anchors (research_paper.json, etc).

Top-level dependency direction (one-way):
  runner.py
    ├─ planner.py    -> roles.py, schematic.py, scheduler, providers
    ├─ orchestrator.py -> roles.py, schematic.py, judge, scheduler, providers
    └─ judge          -> scheduler, providers (judge does not import orchestrator)

Re-exports the most commonly used public names so callers can do
`from orchestrator import Role, TaskSchematic, plan` instead of reaching
into individual submodules.
"""

from .roles import Role
from .schematic import (
    FanoutSpec,
    JudgeConfig,
    OutputRules,
    Rule,
    StageDef,
    TaskSchematic,
)

__all__ = [
    "Role",
    "FanoutSpec",
    "StageDef",
    "TaskSchematic",
    "JudgeConfig",
    "OutputRules",
    "Rule",
]
