"""
judge/plugins.py - Layer 2: code-backed plugin registry.

Plugins handle validation that can't be expressed declaratively in
engine.py — URL fetches, parser-based checks, code-execution sandboxes,
etc. Each plugin is a class with a `tag` and an async `run()` method.

# How a plugin gets installed

  1. Write a class implementing the JudgePlugin protocol.
  2. At the bottom of the module, call `register(MyPlugin())`.
  3. Import the module from judge/plugins_impl/__init__.py.

The schematic asks for plugins by tag string in `judge_config.plugins`.
Unknown tags become soft flags (a missing/uninstalled plugin shouldn't
break otherwise-valid schematics).
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from .report import JudgeReport, RunContext


class JudgePlugin(Protocol):
    """The contract every plugin must satisfy.

    `tag` is the identifier the schematic uses to request this plugin.
    Must be unique across the registry.

    `run()` does the actual validation. Returns a JudgeReport with any
    findings. May raise on internal errors — `run_plugins()` catches
    exceptions and degrades to soft flags so one buggy plugin can't
    break the pipeline.
    """
    tag: str

    async def run(self, body: str, context: RunContext) -> JudgeReport: ...


# Module-level registry. Populated by side-effect imports from
# plugins_impl/. Keys are tag strings; values are plugin instances
# (not classes — instances may carry config like timeouts).
_REGISTRY: dict[str, JudgePlugin] = {}


def register(plugin: JudgePlugin) -> None:
    """Register a plugin by its tag. Raises on tag conflicts."""
    if plugin.tag in _REGISTRY:
        raise ValueError(
            f"judge plugin tag conflict: {plugin.tag} already registered"
        )
    _REGISTRY[plugin.tag] = plugin


def installed_tags() -> list[str]:
    """Return all currently-registered plugin tags. Used by --list."""
    return sorted(_REGISTRY)


async def run_plugins(
    body: str, wanted_tags: list[str], context: RunContext,
) -> JudgeReport:
    """Run every requested plugin in parallel; merge reports.

    Unknown tags produce a soft flag, never a crash. Plugin exceptions
    also become soft flags — Judge degrades gracefully when individual
    layers misbehave.
    """
    report = JudgeReport()
    selected: list[JudgePlugin] = []

    for tag in wanted_tags:
        plugin = _REGISTRY.get(tag)
        if plugin is None:
            report.soft_flags.append(f"judge plugin not installed: {tag!r}")
            continue
        selected.append(plugin)

    if not selected:
        return report

    # asyncio.gather with return_exceptions=True so one plugin's crash
    # doesn't cancel the others.
    results = await asyncio.gather(
        *(p.run(body, context) for p in selected),
        return_exceptions=True,
    )
    for plugin, result in zip(selected, results):
        if isinstance(result, BaseException):
            report.soft_flags.append(
                f"plugin {plugin.tag!r} crashed: {result!r}"
            )
        else:
            report.merge(result)
    return report
