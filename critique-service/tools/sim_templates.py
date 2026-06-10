from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

# Quota-free validation of the builtin template library: every builtin parses + validates,
# the new P3 templates are present, and a representative multi-stage + fanout template runs
# end-to-end against the mock provider (exercising extractor->fanout->transformer->judge).
#   MOCK_MODE=1 python tools/sim_templates.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("LOOM_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ.pop("METRICS_HF_REPO", None)
os.environ.pop("TEMPLATES_HF_REPO", None)
os.environ.pop("OPTIN_PROVIDERS", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"
# generous synthetic budgets: a multi-stage template makes many calls; the default
# mock budget (2-8/provider) would 429 mid-run.
for p in ("NVIDIA", "CLOUDFLARE", "OPENROUTER", "GITHUB_MODELS"):
    os.environ[f"MOCK_RPD_{p}"] = "1000"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402
import critique_service as cs  # noqa: E402
import mock_provider  # noqa: E402
import orchestrate  # noqa: E402
from orchestrator.orchestrator import execute  # noqa: E402

FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)

NEW = ["repo_audit", "design_doc", "redteam", "panel_debate"]


async def main() -> int:
    print("scenario: every builtin template parses + validates")
    store = orchestrate.TemplateStore()
    ids = [r["id"] if isinstance(r, dict) else r for r in store.list()]
    for t in NEW:
        check(f"builtin '{t}' present", t in ids)
    for t in ids:
        try:
            schem = store.get(t)
            check(f"'{t}' valid ({len(schem.stages)} stages)", len(schem.stages) >= 1)
        except Exception as e:  # noqa: BLE001
            check(f"'{t}' valid", False, f"{type(e).__name__}: {e}")

    print("scenario: a fanout template runs end-to-end (mock)")
    #   fresh panel per run so scheduler cooldown/rotation state doesn't bleed between
    #   two heavy multi-stage runs (each is independently representative).
    mock_provider.reset(None)
    panel = cs.Panel()
    schem = store.get("redteam")  # extractor(surfaces) -> fanout probe -> report -> verify
    async with httpx.AsyncClient() as client:
        res = await execute(schem, panel.scheduler, client,
                            initial_context={"prompt": "Review this tiny HTTP service: a "
                                             "token-guarded API that stores user prompts on disk."},
                            log=lambda *_: None, metrics=panel.metrics,
                            profile="p_tmpl", effort="low", nonce="")
    check("redteam run completed", res is not None and res.body is not None, f"err={getattr(res,'error',None)}")
    shard_provs = [s.provider for s in res.stages if s.name.startswith("probe[") and s.ok]
    check("redteam fanned out into multiple probe shards", len(shard_provs) >= 2, f"shards={shard_provs}")
    check("redteam produced a report body", bool(res.body and len(res.body) > 50))

    print("scenario: design_doc (7-stage, fanout approaches) runs end-to-end (mock)")
    mock_provider.reset(None)
    panel2 = cs.Panel()
    schem2 = store.get("design_doc")
    async with httpx.AsyncClient() as client:
        res2 = await execute(schem2, panel2.scheduler, client,
                             initial_context={"prompt": "Design a rate limiter for a public API."},
                             log=lambda *_: None, metrics=panel.metrics,
                             profile="p_tmpl2", effort="low", nonce="")
    check("design_doc run completed", res2 is not None and res2.body is not None,
          f"err={getattr(res2,'error',None)}")
    dd_shards = [s for s in res2.stages if s.name.startswith("deep_dive[") and s.ok]
    check("design_doc fanned out approaches", len(dd_shards) >= 2, f"shards={len(dd_shards)}")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL TEMPLATE SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
