from __future__ import annotations
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Quota-free test of the prompt cache: miss -> store -> hit (no second provider call),
# key sensitivity, no_store skips writes, and TTL expiry. MOCK_MODE=1 python tools/sim_cache.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("DOOMALAYSOCREATE_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ.pop("METRICS_HF_REPO", None)
os.environ.pop("OPTIN_PROVIDERS", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402
import critique_service as cs  # noqa: E402
from jobs import route_judge  # noqa: E402
from promptcache import PromptCache  # noqa: E402

FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)

_TMP = Path(tempfile.mkdtemp(prefix="sim-cache-"))


async def _judge(panel, cache, *, sysprompt="review", user="go", no_store=False,
                 privacy="off"):
    async with httpx.AsyncClient() as client:
        return await route_judge(
            panel.scheduler, client, "llama-3.3-70b", panel.resolve_candidates("llama-3.3-70b")[1],
            sysprompt, role_label="critiquer", effort="low", profile="p_cache",
            metrics=panel.metrics, user_msg=user, max_tokens=128, timeout_s=30,
            reasoning_catalog=panel.reasoning_catalog, privacy=privacy,
            cache=cache, no_store=no_store)


def main() -> int:
    panel = cs.Panel()
    cache = PromptCache(enabled=True, dirpath=_TMP / "cache", ttl_s=3600, max_entries=8)

    print("scenario: miss -> store -> hit (no second provider call)")
    r1 = asyncio.run(_judge(panel, cache))
    check("first call is a live miss", r1.get("ok") and not r1.get("cached"), f"cached={r1.get('cached')}")
    out1 = r1.get("output")
    r2 = asyncio.run(_judge(panel, cache))
    check("second identical call is a cache hit", r2.get("cached") is True, f"cached={r2.get('cached')}")
    check("cached output matches the stored output", r2.get("output") == out1)
    check("cache recorded exactly 1 hit / 1 miss", cache.stats()["hits"] == 1 and cache.stats()["misses"] == 1,
          str(cache.stats()))
    check("cache mirrored to disk", any((_TMP / "cache").glob("*.json")))

    print("scenario: a different prompt is a separate key (miss)")
    r3 = asyncio.run(_judge(panel, cache, user="different question"))
    check("different user_msg -> miss (not served from cache)", not r3.get("cached"), f"cached={r3.get('cached')}")

    print("scenario: privacy mode is part of the key (no off->strict bypass)")
    #   the identical request under privacy=strict must NOT be served the result the
    #   privacy=off call produced (it may have come from a training host).
    rs = asyncio.run(_judge(panel, cache, privacy="strict"))
    check("same prompt under strict -> miss, fresh call", not rs.get("cached"), f"cached={rs.get('cached')}")
    rs2 = asyncio.run(_judge(panel, cache, privacy="strict"))
    check("strict repeat -> hit on the strict-keyed entry", rs2.get("cached") is True, f"cached={rs2.get('cached')}")

    print("scenario: no_store skips the write")
    cache2 = PromptCache(enabled=True, dirpath=_TMP / "cache2", ttl_s=3600, max_entries=8)
    asyncio.run(_judge(panel, cache2, sysprompt="secret", no_store=True))
    r4 = asyncio.run(_judge(panel, cache2, sysprompt="secret", no_store=True))
    check("no_store: repeat is still a miss (nothing was cached)", not r4.get("cached"), f"cached={r4.get('cached')}")
    check("no_store: nothing written to disk", not any((_TMP / "cache2").glob("*.json")))

    print("scenario: TTL expiry forces a fresh call")
    cache3 = PromptCache(enabled=True, dirpath=_TMP / "cache3", ttl_s=0.3, max_entries=8)
    asyncio.run(_judge(panel, cache3, sysprompt="ttl-test"))
    time.sleep(0.5)
    r5 = asyncio.run(_judge(panel, cache3, sysprompt="ttl-test"))
    check("expired entry -> miss", not r5.get("cached"), f"cached={r5.get('cached')}")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL CACHE SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(rc)
