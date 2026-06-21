from __future__ import annotations
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

# Quota-free guard that the service never leaks key material: boot with marker-valued
# provider keys, hit every endpoint, and assert the markers appear in NO response; also
# assert repr(provider) masks api_key. MOCK_MODE=1 python tools/sim_secrets.py

MARK = "SECRETMARKER_d0Not_Leak_123456"
os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("DOOMALAYSOCREATE_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "smoke-token"
os.environ.pop("METRICS_HF_REPO", None)
os.environ.pop("JOBS_HF_REPO", None)
os.environ["NVIDIA_API_KEY"] = "nvapi-" + MARK
os.environ["CF_API_TOKEN"] = "cf-" + MARK
os.environ["CF_ACCOUNT_ID"] = "acct-" + MARK
os.environ["GITHUB_TOKEN"] = "ghp_" + MARK
os.environ["OPENROUTER_API_KEY"] = "sk-or-" + MARK

import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import critique_service as cs  # noqa: E402
import providers as pv  # noqa: E402

PORT = 7973
FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)


def main() -> int:
    print("scenario: provider repr masks api_key")
    p = pv.provider(name="x", url="u", api_key="nvapi-" + MARK, models=("m",))
    check("repr(provider) hides the key", MARK not in repr(p) and MARK not in str(p), repr(p))

    print("scenario: no endpoint response contains key material")
    panel = cs.Panel()
    srv = cs.BoundedThreadingHTTPServer(("127.0.0.1", PORT), cs.Handler, max_workers=8)
    srv.panel = panel
    from jobs import JobRunner
    srv.jobs = JobRunner(panel, judge_timeout_s=30)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(1.0)

    def get(path):
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                     headers={"Authorization": "Bearer smoke-token"})
        try:
            return urllib.request.urlopen(req, timeout=10).read().decode()
        except Exception as e:  # noqa: BLE001
            return f"(err {e})"

    for ep in ("/health", "/api/stats", "/api/roster", "/api/metrics", "/api/templates"):
        body = get(ep)
        check(f"{ep} leaks no key", MARK not in body, "MARKER FOUND" if MARK in body else "")

    # an error path (bad job id) must not leak either
    check("/api/jobs/<bad> leaks no key", MARK not in get("/api/jobs/deadbeef"))
    # /health must not expose the bearer token value, only a boolean
    h = get("/health")
    check("/health exposes token as boolean only", '"token_configured"' in h and "smoke-token" not in h)

    srv.shutdown()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL SECRET-SAFETY SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
