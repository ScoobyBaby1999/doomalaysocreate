from __future__ import annotations
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Quota-free end-to-end simulation of the routing gateway. Exercises rotation,
# cross-provider failover, budget exhaustion, per-profile isolation, and effort
# scaling entirely against the MOCK provider - ZERO real API calls.
#
#   run:  MOCK_MODE=1 python tools/sim_scenarios.py
#
# exits non-zero if any assertion fails.

# --- environment: mock mode + fake keys so providers register ---------------
os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("LOOM_LOG", "0")            # quiet the per-call telemetry
os.environ["CRITIQUE_TOKEN"] = "test-token"
os.environ["CACHE_ENABLED"] = "0"                 # these scenarios repeat identical calls
                                                   # to exercise ROTATION; the prompt cache
                                                   # (correctly) collapses them, so disable it
                                                   # here. cache itself is tested in sim_cache.
os.environ.pop("METRICS_HF_REPO", None)           # in-memory persistence only
os.environ.pop("OPTIN_PROVIDERS", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"
# generous default synthetic budgets; individual scenarios tighten as needed.
for p in ("NVIDIA", "CLOUDFLARE", "OPENROUTER", "GITHUB_MODELS"):
    os.environ[f"MOCK_RPD_{p}"] = "1000"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import metrics as metrics_mod  # noqa: E402
import mock_provider  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="sim-metrics-"))
metrics_mod.DATA_DIR = _TMP
metrics_mod.FLUSH_EVERY = 1

import critique_service as cs  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def fresh_panel(budgets: dict[str, int] | None = None, faults: dict[str, str] | None = None):
    #   clean metrics dir + reset the mock limiter so each scenario starts fresh.
    for f in _TMP.glob("*.jsonl"):
        f.unlink()
    for p in ("NVIDIA", "CLOUDFLARE", "OPENROUTER", "GITHUB_MODELS"):
        os.environ[f"MOCK_RPD_{p}"] = str((budgets or {}).get(p, 1000))
    mock_provider.reset(faults)
    return cs.Panel()


async def run(panel, who_list, profile, effort="med"):
    params = cs.build_panel_params(
        panel, input_text="please review this", role="critiquer", system=None,
        instructions="", output_rules="", template=None,
        panel_override=list(who_list), merge_mode="none", max_tokens=400)
    params["profile"] = profile
    cs.apply_effort(params, effort)
    return await cs.run_sync(panel, params)


def providers_used(agg: dict) -> list[str]:
    return [p for p, v in agg["by_provider"].items() if v["calls"] > 0]


async def scenario_rotation():
    print("scenario: rotation spreads load across providers")
    panel = fresh_panel()
    for _ in range(9):
        await run(panel, ["llama-3.3-70b"], "p_rot", effort="low")
    agg = panel.metrics.aggregates("p_rot")
    used = providers_used(agg)
    check("rotation used >=2 providers", len(used) >= 2, f"used={used}")
    check("rotation recorded 9 events", agg["events"] == 9, f"events={agg['events']}")


async def scenario_failover():
    print("scenario: a 429 bounces to another provider hosting the same model")
    panel = fresh_panel(faults={"cloudflare": "429"})
    res = await run(panel, ["llama-3.3-70b"], "p_fail", effort="low")
    judge = res["judges"][0]
    check("failover judge succeeded", judge.get("ok") is True, f"err={judge.get('error')}")
    routed = judge.get("routed_to") or ""
    check("failover routed away from cloudflare", not routed.startswith("cloudflare"), f"routed_to={routed}")
    check("failover tried >1 candidate", judge.get("attempts", 0) >= 2, f"attempts={judge.get('attempts')}")
    agg = panel.metrics.aggregates("p_fail")
    cf = agg["by_provider"].get("cloudflare", {})
    check("failover recorded cloudflare 429", cf.get("throttle_429", 0) >= 1, f"cloudflare={cf}")


async def scenario_budget():
    print("scenario: per-profile budget exhaustion cools the provider")
    panel = fresh_panel(budgets={"GITHUB_MODELS": 2})
    results = [await run(panel, ["deepseek-v3"], "p_bud", effort="low") for _ in range(4)]
    oks = [r["judges"][0].get("ok") for r in results]
    check("first calls ok then exhausted", oks[0] and not oks[-1], f"oks={oks}")
    roll = panel.scheduler.provider_rollup().get("github-models", {})
    agg = panel.metrics.aggregates("p_bud").get("by_provider", {}).get("github-models", {})
    check("github-models shows throttle in metrics", agg.get("throttle_429", 0) >= 1, f"gh={agg}")
    check("github-models has a cooling slot", roll.get("cooling_slots", 0) >= 1, f"rollup={roll}")


async def scenario_profile_isolation():
    print("scenario: metrics are isolated per profile")
    panel = fresh_panel()
    for _ in range(3):
        await run(panel, ["llama-3.3-70b"], "p_a", effort="low")
    await run(panel, ["llama-3.3-70b"], "p_b", effort="low")
    a = panel.metrics.aggregates("p_a")
    b = panel.metrics.aggregates("p_b")
    check("profile p_a has 3 events", a["events"] == 3, f"a={a['events']}")
    check("profile p_b has 1 event", b["events"] == 1, f"b={b['events']}")
    check("profiles listed", set(panel.metrics.profiles()) >= {"p_a", "p_b"})


async def scenario_effort():
    print("scenario: manual effort scales fan-out width + tokens")
    panel = fresh_panel()
    wide = ["llama-3.3-70b", "deepseek-v3", "glm-5.1", "deepseek-v4-pro", "kimi-k2.6"]
    low = cs.apply_effort({"who_list": list(wide), "max_tokens": 1000}, "low")
    high = cs.apply_effort({"who_list": list(wide), "max_tokens": 1000}, "high")
    check("low effort -> 1 judge", len(low["who_list"]) == 1, f"n={len(low['who_list'])}")
    check("high effort -> up to 5 judges", len(high["who_list"]) == 5, f"n={len(high['who_list'])}")
    check("low effort halves tokens", low["max_tokens"] == 500, f"tok={low['max_tokens']}")
    res = await run(panel, wide, "p_eff", effort="low")
    check("low effort ran exactly 1 judge", len(res["judges"]) == 1, f"judges={len(res['judges'])}")


async def main() -> int:
    for scenario in (scenario_rotation, scenario_failover, scenario_budget,
                     scenario_profile_isolation, scenario_effort):
        await scenario()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL SCENARIOS PASSED (zero real API calls)")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(rc)
