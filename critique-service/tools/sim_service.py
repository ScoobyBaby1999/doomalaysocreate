from __future__ import annotations
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# In-process test of the service wiring for /api/run (no HTTP / no background
# server — those are flaky under the sandbox). Exercises: template listing,
# schematic resolution, the sync run path (run_orchestration_sync), and the
# async path (JobRunner.submit_run + snapshot polling). Zero real API calls.
#   MOCK_MODE=1 python tools/sim_service.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("LOOM_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ.pop("METRICS_HF_REPO", None)
for key in ("NVIDIA_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"
for p in ("NVIDIA", "CEREBRAS", "OPENROUTER", "GITHUB_MODELS"):
    os.environ[f"MOCK_RPD_{p}"] = "1000"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import metrics as metrics_mod  # noqa: E402
_TMP = Path(tempfile.mkdtemp(prefix="sim-svc-"))
metrics_mod.DATA_DIR = _TMP
metrics_mod.FLUSH_EVERY = 1

import critique_service as cs  # noqa: E402
import orchestrate  # noqa: E402
from jobs import JobRunner  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


ARTIFACT_SCHEM = {
    "task_type": "code_spec", "task": "tiny app",
    "stages": [{"name": "build", "role": "generator",
                "instructions": "Build the requested project.", "inputs": ["prompt"],
                "max_tokens": 2000}],
    "output_rules": {"format": "files"},
    "judge_config": {"rules": [], "plugins": [], "llm_judges": []}, "max_rounds": 1,
}


async def scenario_templates_and_sync():
    print("scenario: templates listing + schematic resolution + sync run")
    ids = [t["id"] for t in orchestrate.list_templates()]
    check("built-in templates listed", set(ids) >= {"freeform", "lesson_plan", "research_paper"}, f"ids={ids}")

    schem, nonce = orchestrate.resolve_schematic("Write notes.", template_id="freeform", schematic_obj=None)
    check("freeform resolves, no nonce (markdown)", schem.task_type == "freeform" and nonce == "")
    schem2, nonce2 = orchestrate.resolve_schematic("x", template_id=None, schematic_obj=ARTIFACT_SCHEM)
    check("files schematic gets a nonce", bool(nonce2), f"nonce={nonce2!r}")

    panel = cs.Panel()
    res = await cs.run_orchestration_sync(panel, schem, "Write a haiku.",
                                          profile="svc", effort="med", nonce=nonce)
    check("sync run ok", res.get("ok"), f"err={res.get('error')}")
    check("sync run has body", bool(res.get("body")))

    res2 = await cs.run_orchestration_sync(panel, schem2, "Build a CLI.",
                                           profile="svc", effort="med", nonce=nonce2)
    paths = sorted(a["path"] for a in res2.get("artifacts", []))
    check("sync run yields file artifacts", paths == ["README.md", "src/app.py"], f"paths={paths}")
    agg = panel.metrics.aggregates("svc")
    check("run metrics recorded per profile", agg["events"] >= 2, f"events={agg['events']}")


def scenario_async_jobrunner():
    print("scenario: async JobRunner.submit_run + snapshot polling")
    panel = cs.Panel()
    runner = JobRunner(panel, judge_timeout_s=60.0)
    schem, nonce = orchestrate.resolve_schematic("x", template_id=None, schematic_obj=ARTIFACT_SCHEM)
    snap = runner.submit_run(schematic=schem, prompt="Build a CLI.", nonce=nonce,
                             profile="svc_async", effort="high")
    check("submit returns run snapshot", snap.get("type") == "run" and snap.get("status") == "running")
    final = None
    for _ in range(30):
        time.sleep(0.5)
        final = runner.snapshot(snap["job_id"])
        if final and final.get("status") == "complete":
            break
    check("async run completes", final and final.get("status") == "complete",
          f"status={(final or {}).get('status')}")
    result = (final or {}).get("result") or {}
    check("async run ok", result.get("ok"), f"err={result.get('error')}")
    check("async run artifacts present", sorted(a["path"] for a in result.get("artifacts", [])) == ["README.md", "src/app.py"])


async def scenario_panel_artifacts():
    print("scenario: panel-mode artifacts keep each model's tree separate")
    panel = cs.Panel()
    params = cs.build_panel_params(
        panel, input_text="build a tiny tool", role="generator", system=None,
        instructions="", output_rules="", template=None,
        panel_override=["llama-3.3-70b", "glm-5.1"], merge_mode="none",
        max_tokens=2000, want_artifacts=True)
    params["profile"] = "pa"
    cs.apply_effort(params, "high")
    check("artifacts panel got a nonce", bool(params.get("nonce")))
    res = await cs.run_sync(panel, params)
    judges = [j for j in res["judges"] if j.get("ok")]
    check("multiple judges answered", len(judges) >= 2, f"n={len(judges)}")
    each_have = all(sorted(a["path"] for a in j.get("artifacts", [])) == ["README.md", "src/app.py"]
                    for j in judges)
    check("each judge has its OWN file tree", each_have,
          detail=str([(j["model"], [a["path"] for a in j.get("artifacts", [])]) for j in judges]))


def scenario_materialize():
    print("scenario: client materializes a run result to disk")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from orchestrate_client import materialize_run
    result = {
        "task": "demo", "ok": True, "rounds": 0,
        "body": "ignored when artifacts present",
        "artifacts": [{"path": "README.md", "content": "# hi\n", "truncated": False},
                      {"path": "src/app.py", "content": "print(1)\n", "truncated": False}],
        "judge": {"hard_fails": [], "soft_flags": ["minor note"]},
        "stages": [], "providers_used": ["nvidia"],
    }
    dest = materialize_run(result, _TMP / "runs")
    files = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
    check("materialized files on disk", files >= {"README.md", "src/app.py", "_judge.md", "_manifest.json"}, f"files={files}")
    check("artifact content written", (dest / "src/app.py").read_text() == "print(1)\n")
    #   path-traversal defence
    bad = materialize_run({"task": "t", "ok": True, "artifacts": [
        {"path": "../escape.txt", "content": "x"}], "judge": {}}, _TMP / "runs2")
    check("traversal artifact not written outside dir", not (_TMP / "escape.txt").exists())


async def main():
    await scenario_templates_and_sync()
    scenario_async_jobrunner()
    await scenario_panel_artifacts()
    scenario_materialize()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}")
        return 1
    print("ALL SERVICE-WIRING SCENARIOS PASSED (zero real API calls)")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(rc)
