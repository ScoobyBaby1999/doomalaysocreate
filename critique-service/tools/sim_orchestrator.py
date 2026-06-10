from __future__ import annotations
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Quota-free end-to-end test of the ported orchestrator + judge + artifacts.
#   MOCK_MODE=1 python tools/sim_orchestrator.py
# Exits non-zero on any failed assertion.

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("LOOM_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ.pop("METRICS_HF_REPO", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID",
            "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"
for p in ("NVIDIA", "CLOUDFLARE", "OPENROUTER", "GITHUB_MODELS"):
    os.environ[f"MOCK_RPD_{p}"] = "1000"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import metrics as metrics_mod  # noqa: E402
import mock_provider  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="sim-orch-"))
metrics_mod.DATA_DIR = _TMP
metrics_mod.FLUSH_EVERY = 1

import artifacts  # noqa: E402
import critique_service as cs  # noqa: E402
from orchestrator.orchestrator import execute  # noqa: E402
from orchestrator.schematic import from_json_text, from_json_file  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def fresh():
    for f in _TMP.glob("*.jsonl"):
        f.unlink()
    mock_provider.reset(None)
    return cs.Panel()


async def run(schematic, prompt, profile, *, nonce=""):
    panel = fresh()
    async with httpx.AsyncClient() as client:
        return await execute(
            schematic, panel.scheduler, client,
            initial_context={"prompt": prompt},
            log=lambda *_: None, metrics=panel.metrics,
            profile=profile, effort="med", nonce=nonce,
        ), panel


async def scenario_freeform():
    print("scenario: freeform template runs end-to-end")
    schem = from_json_file(HERE / "orchestrator" / "templates" / "freeform.json")
    res, panel = await run(schem, "Write a haiku about caching.", "p_free")
    check("freeform ok", res.ok, f"err={res.error}")
    check("freeform produced a body", bool(res.body))
    agg = panel.metrics.aggregates("p_free")
    check("freeform emitted metrics", agg["events"] >= 1, f"events={agg['events']}")


async def scenario_artifacts():
    print("scenario: file-output stage yields multi-file artifacts")
    schem = from_json_text(json.dumps({
        "task_type": "code_spec", "task": "mock build",
        "stages": [{"name": "build", "role": "generator",
                    "instructions": "Build the requested project.", "inputs": ["prompt"],
                    "max_tokens": 2000}],
        "output_rules": {"format": "files"},
        "judge_config": {"rules": [], "plugins": [], "llm_judges": []},
        "max_rounds": 1,
    }))
    nonce = artifacts.make_nonce()
    res, _ = await run(schem, "Build a tiny app.", "p_art", nonce=nonce)
    check("artifact run ok", res.ok, f"err={res.error}")
    paths = sorted(f["path"] for f in res.artifacts)
    check("artifacts materialized as files", paths == ["README.md", "src/app.py"], f"paths={paths}")
    check("no truncated files", all(not f["truncated"] for f in res.artifacts))


async def scenario_judge_retry():
    print("scenario: judge hard-fail injects retries up to max_rounds")
    schem = from_json_text(json.dumps({
        "task_type": "freeform", "task": "short thing",
        "stages": [{"name": "write", "role": "generator",
                    "instructions": "Write something short.", "inputs": ["prompt"],
                    "max_tokens": 500}],
        "output_rules": {"format": "markdown"},
        "judge_config": {"rules": [{"type": "min_words", "value": 100000}],
                         "plugins": [], "llm_judges": []},
        "max_rounds": 2,
    }))
    res, _ = await run(schem, "Write something.", "p_judge")
    check("judge fired retry rounds", res.rounds == 2, f"rounds={res.rounds}")
    check("judge still rejects (min_words unreachable)", not res.ok)
    check("final report has hard fails", res.final_report and not res.final_report.ok)


async def scenario_fanout_diversity():
    print("scenario: concurrent fanout shards spread across providers (inflight exclusion)")
    #   a planner emits a list, then a fanout stage runs one shard per item with
    #   max_parallel=3. with the inflight-exclusion fix, simultaneous shards must land
    #   on DIFFERENT providers (mock has 4). without it they collapse onto one.
    schem = from_json_text(json.dumps({
        "task_type": "freeform", "task": "fanout spread",
        "stages": [
            {"name": "outline", "role": "extractor",
             "instructions": "Extract exactly 3 topics as JSON.", "inputs": ["prompt"],
             "max_tokens": 300},
            {"name": "sections", "role": "generator",
             "instructions": "Write the section.", "inputs": ["outline.topics.{i}"],
             "max_tokens": 300, "fanout": {"over": "outline.topics", "max_parallel": 3}},
        ],
        "output_rules": {"format": "markdown"},
        "judge_config": {"rules": [], "plugins": [], "llm_judges": []},
        "max_rounds": 1,
    }))
    res, _ = await run(schem, "Spread across providers.", "p_fanout")
    shard_provs = [s.provider for s in res.stages
                   if s.name.startswith("sections[") and s.ok and s.provider]
    check("fanout produced >=2 ok shards", len(shard_provs) >= 2, f"shards={shard_provs}")
    check("concurrent shards used >=2 distinct providers",
          len(set(shard_provs)) >= 2, f"providers={shard_provs}")


async def main():
    for sc in (scenario_freeform, scenario_artifacts, scenario_judge_retry,
               scenario_fanout_diversity):
        await sc()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL ORCHESTRATOR SCENARIOS PASSED (zero real API calls)")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(rc)
