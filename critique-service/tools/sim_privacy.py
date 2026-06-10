from __future__ import annotations
import asyncio
import os
import sys
import time
from pathlib import Path

# Quota-free test of the privacy router. Proves strict NEVER routes to a training/logging
# host, fallback uses one only as a last resort, off uses all, and the roster reports the
# >=2-frontier guarantee. MOCK_MODE=1 python tools/sim_privacy.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("LOOM_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ["CACHE_ENABLED"] = "0"
os.environ.pop("METRICS_HF_REPO", None)
os.environ.pop("OPTIN_PROVIDERS", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402
import critique_service as cs  # noqa: E402
from jobs import route_judge  # noqa: E402
from providers import slot_is_privacy_safe  # noqa: E402

FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)


async def _judge(panel, candidates, privacy):
    async with httpx.AsyncClient() as client:
        return await route_judge(
            panel.scheduler, client, "nemotron-ultra", candidates, "review this",
            role_label="critiquer", effort="low", profile="p_priv", metrics=panel.metrics,
            user_msg="go", max_tokens=128, timeout_s=30,
            reasoning_catalog=panel.reasoning_catalog, privacy=privacy)


def _cool(panel, who):
    #   force a slot ineligible (as a 429 cooldown would), without any real call.
    panel.scheduler.slot_state[who].cooldown_until = time.time() + 1000


def main() -> int:
    panel = cs.Panel()
    _, cands = panel.resolve_candidates("nemotron-ultra")
    safe = [c for c in cands if slot_is_privacy_safe(c)]
    unsafe = [c for c in cands if not slot_is_privacy_safe(c)]
    print(f"nemotron-ultra hosts: safe={[c.who for c in safe]} unsafe={[c.who for c in unsafe]}")
    check("model has >=1 safe and >=1 unsafe host (setup)", bool(safe and unsafe))

    print("scenario: OFF uses any host (incl. training ones)")
    r = asyncio.run(_judge(panel, cands, "off"))
    check("off: judge ok", r.get("ok"), r.get("error"))

    print("scenario: STRICT never touches an unsafe host even when the safe one is down")
    for c in safe:
        _cool(panel, c.who)
    r = asyncio.run(_judge(panel, cands, "strict"))
    tried = r.get("candidates_tried", [])
    check("strict: judge did NOT succeed (safe host cooling, unsafe forbidden)", not r.get("ok"),
          f"routed_to={r.get('routed_to')}")
    check("strict: never tried any unsafe host",
          all(t not in [u.who for u in unsafe] for t in tried), f"tried={tried}")
    check("strict: explicit privacy_blocked code (not generic 'all cooling')",
          r.get("code") == "privacy_blocked", f"code={r.get('code')} err={str(r.get('error'))[:80]}")

    print("scenario: FALLBACK uses the unsafe host only as a last resort")
    r = asyncio.run(_judge(panel, cands, "fallback"))
    check("fallback: judge ok via the unsafe host", r.get("ok"), r.get("error"))
    check("fallback: routed to an unsafe host (safe was down)",
          r.get("routed_to") in [u.who for u in unsafe], f"routed_to={r.get('routed_to')}")

    print("scenario: STRICT with ONLY unsafe hosts -> privacy_blocked (no leak)")
    r = asyncio.run(_judge(panel, unsafe, "strict"))
    check("strict: privacy_blocked code", r.get("code") == "privacy_blocked", f"code={r.get('code')}")
    check("strict: no host tried at all", not r.get("candidates_tried"), f"tried={r.get('candidates_tried')}")

    print("scenario: roster reports the >=2-frontier guarantee over privacy-safe hosts")
    roster = panel.roster()
    fr = [m for m in roster["models"] if m["frontier"]]
    check("roster has frontier models", len(fr) >= 2, f"frontier={len(fr)}")
    check("frontier_ok true on a healthy fresh panel", roster["frontier_ok"],
          f"safe_avail={roster['frontier_privacy_safe_available']}")
    check("models sorted by benchmark desc",
          all((roster["models"][i]["arena_elo"] or 0) >= (roster["models"][i+1]["arena_elo"] or 0)
              for i in range(len(roster["models"]) - 1)))

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL PRIVACY SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
