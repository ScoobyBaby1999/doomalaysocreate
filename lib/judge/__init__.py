"""
judge/ - Pluggable validation pipeline for orchestrator outputs.

Three-layer architecture (run in order, reports merged):

  Layer 1: engine.py     declarative rule checks (pure Python, fast)
  Layer 2: plugins.py    code-backed plugins (URL fetches, etc.)
  Layer 3: llm_judge.py  LLM-as-judge calls for fuzzy criteria

Public surface:
  evaluate(body, schematic, scheduler, http_client, exclude_provider)
    The single entry point. Composes all three layers and returns a
    JudgeReport.

  JudgeReport
    Aggregated verdict. Has hard_fails, soft_flags, ok property,
    merge() method, and to_critique_feed() for downstream stages.
"""

import time

from oplog import log_event

from .report import JudgeReport, RunContext
from .engine import run_rules
from .plugins import register, run_plugins
from .llm_judge import run_llm_judges

# Side-effect import so plugins register themselves on `import judge`.
# Without this, schematics that reference plugin tags would soft-flag
# every plugin as "not installed" until something else imports plugins_impl.
from . import plugins_impl  # noqa: F401


async def evaluate(
    body: str,
    schematic,
    scheduler,
    http_client,
    exclude_provider: str | None = None,
    metrics=None,
    profile: str = "default",
) -> JudgeReport:
    """Full Judge pipeline. Composes all three layers.

    Layer 1 + Layer 2 run unconditionally on whatever the schematic
    declares. Layer 3 only runs if `judge_config.llm_judges` is non-empty
    (LLM-as-judge calls cost real LLM quota — we don't fire them when
    no fuzzy criteria are declared).

    Layers do NOT short-circuit on each other's hard fails. Even if
    Layer 1 says "word count too low", we still run Layers 2 and 3
    so the next reviewer stage gets the full issue list to address
    in one pass instead of bouncing back and forth.

    `exclude_provider` is the provider that produced the body being
    judged. LLM-as-judge calls in Layer 3 honor this so a verifier
    judging the body doesn't run on the same carrier that wrote it.
    """
    cfg = schematic.judge_config
    report = JudgeReport()

    # Layer 1 — declarative rules (pure Python, fast).
    t0 = time.monotonic()
    layer1 = run_rules(body, cfg.get("rules", []), schematic.output_rules)
    report.merge(layer1)
    log_event("judge_layer_done",
              layer="rules",
              rule_count=len(cfg.get("rules", [])),
              hard=len(layer1.hard_fails),
              soft=len(layer1.soft_flags),
              duration_s=round(time.monotonic() - t0, 3),
              hard_details=layer1.hard_fails[:5])

    # Layer 2 — code-backed plugins (I/O-bound).
    t0 = time.monotonic()
    ctx = RunContext(http_client=http_client, schematic=schematic)
    layer2 = await run_plugins(body, cfg.get("plugins", []), ctx)
    report.merge(layer2)
    log_event("judge_layer_done",
              layer="plugins",
              plugins=cfg.get("plugins", []),
              hard=len(layer2.hard_fails),
              soft=len(layer2.soft_flags),
              duration_s=round(time.monotonic() - t0, 3),
              hard_details=layer2.hard_fails[:5])

    # Layer 3 — LLM-as-judge (only if criteria declared).
    if cfg.get("llm_judges"):
        t0 = time.monotonic()
        layer3 = await run_llm_judges(
            body=body,
            criteria=cfg["llm_judges"],
            scheduler=scheduler,
            http_client=http_client,
            exclude_provider=exclude_provider,
            metrics=metrics,
            profile=profile,
        )
        report.merge(layer3)
        log_event("judge_layer_done",
                  layer="llm_judges",
                  criteria_count=len(cfg["llm_judges"]),
                  hard=len(layer3.hard_fails),
                  soft=len(layer3.soft_flags),
                  duration_s=round(time.monotonic() - t0, 3),
                  soft_details=layer3.soft_flags[:5])

    return report


__all__ = [
    "evaluate",
    "JudgeReport",
    "RunContext",
    "run_rules",
    "register",
    "run_plugins",
    "run_llm_judges",
]
