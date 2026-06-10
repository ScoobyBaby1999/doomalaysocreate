from __future__ import annotations
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Quota-free test of research mode: the ReAct agent loop + reasoning threading,
# all against the mock provider (which plays the ACTION/OBSERVATION protocol) and
# mock web tools. MOCK_MODE=1 python tools/sim_research.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("LOOM_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ.pop("METRICS_HF_REPO", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"
for p in ("NVIDIA", "CLOUDFLARE", "OPENROUTER", "GITHUB_MODELS"):
    os.environ[f"MOCK_RPD_{p}"] = "1000"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import metrics as metrics_mod  # noqa: E402
_TMP = Path(tempfile.mkdtemp(prefix="sim-research-"))
metrics_mod.DATA_DIR = _TMP
metrics_mod.FLUSH_EVERY = 1
import critique_service as cs  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)


async def run(panel, who, profile, *, research=False, reasoning=False):
    params = cs.build_panel_params(
        panel, input_text="What is the latest on X? Research it.", role="generator",
        system=None, instructions="", output_rules="", template=None,
        panel_override=[who], merge_mode="none", max_tokens=400,
        research=research, reasoning=reasoning)
    params["profile"] = profile
    cs.apply_effort(params, "med")
    return await cs.run_sync(panel, params)


async def main():
    panel = cs.Panel()
    print("scenario: research mode runs the ReAct loop + surfaces reasoning")
    res = await run(panel, "glm-5.1", "p_research", research=True)
    j = res["judges"][0]
    check("research judge ok", j.get("ok"), f"err={j.get('error')}")
    check("final answer returned", "Final answer" in (j.get("output") or ""), f"out={(j.get('output') or '')[:60]}")
    check("agent took >=1 step", (j.get("steps") or 0) >= 1, f"steps={j.get('steps')}")
    check("agent searched the web", (j.get("searches") or 0) >= 1, f"searches={j.get('searches')}")
    check("reasoning trace surfaced", bool(j.get("reasoning")), f"reasoning={bool(j.get('reasoning'))}")
    check("research budget applied (>=16000 tok)", res["meta"].get("judges_total") == 1)
    agg = panel.metrics.aggregates("p_research")
    check("research metrics recorded", agg["events"] >= 1, f"events={agg['events']}")

    print("scenario: reasoning-only flag threads without web loop")
    res2 = await run(panel, "deepseek-v3", "p_reason", reasoning=True)
    j2 = res2["judges"][0]
    check("reasoning-only judge ok", j2.get("ok"), f"err={j2.get('error')}")
    check("reasoning-only did NOT run agent steps", j2.get("steps") is None)

    print("scenario: system_in_user quirk folds system into the user message")
    import jobs as jobs_mod
    captured: dict = {}
    orig_call_slot = jobs_mod.call_slot
    async def spy_call_slot(client, picked, messages, **kw):
        captured["messages"] = messages
        return await orig_call_slot(client, picked, messages, **kw)
    jobs_mod.call_slot = spy_call_slot
    try:
        res3 = await run(panel, "phi-4-reasoning", "p_quirk")
    finally:
        jobs_mod.call_slot = orig_call_slot
    msgs = captured.get("messages") or []
    check("quirk: single user message (no system role)",
          len(msgs) == 1 and msgs[0]["role"] == "user",
          f"roles={[m['role'] for m in msgs]}")
    check("quirk: system prompt folded into user content",
          len(msgs) == 1 and "Produce the requested output now." in msgs[0]["content"]
          and len(msgs[0]["content"]) > 200,
          f"len={len(msgs[0]['content']) if msgs else 0}")
    check("quirk judge still ok", res3["judges"][0].get("ok"),
          f"err={res3['judges'][0].get('error')}")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL RESEARCH SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(rc)
