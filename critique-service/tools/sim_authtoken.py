from __future__ import annotations
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Quota-free test of auto-rotating bearer tokens: window math + grace, tamper rejection,
# end-to-end auth with a derived token, and static back-compat. MOCK_MODE=1 python tools/sim_authtoken.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("DOOMALAYSOCREATE_LOG", "0")
os.environ.pop("METRICS_HF_REPO", None)
os.environ.pop("JOBS_HF_REPO", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"

import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import authtoken  # noqa: E402

FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)

SECRET = "root-secret-never-travels"
W = 3600


def main() -> int:
    print("scenario: window math + grace + tamper")
    t0 = 1_000_000_000.0  # arbitrary fixed clock
    cur = authtoken.make_token(SECRET, now=t0, window_s=W)
    prev = authtoken.make_token(SECRET, now=t0 - W, window_s=W)
    two_ago = authtoken.make_token(SECRET, now=t0 - 2 * W, window_s=W)
    check("token stable within a window",
          cur == authtoken.make_token(SECRET, now=t0 + 5, window_s=W))
    check("token changes across windows", cur != prev != two_ago and cur != two_ago)
    check("current window accepted", authtoken.token_matches(cur, SECRET, now=t0, window_s=W, grace=1))
    check("previous window accepted (grace)", authtoken.token_matches(prev, SECRET, now=t0, window_s=W, grace=1))
    check("two-windows-ago REJECTED (expired)",
          not authtoken.token_matches(two_ago, SECRET, now=t0, window_s=W, grace=1))
    check("tampered token rejected", not authtoken.token_matches(cur[:-2] + "xy", SECRET, now=t0, window_s=W))
    check("wrong secret rejected", not authtoken.token_matches(cur, "other-secret", now=t0, window_s=W))
    check("a leaked token self-expires after window+grace",
          not authtoken.token_matches(cur, SECRET, now=t0 + 2 * W + 1, window_s=W, grace=1))

    print("scenario: end-to-end auth with a derived token (no static token set)")
    os.environ.pop("CRITIQUE_TOKEN", None)
    os.environ["CRITIQUE_ROTATION_SECRET"] = SECRET
    os.environ["TOKEN_WINDOW_S"] = str(W)
    import importlib
    importlib.reload(authtoken)
    import critique_service as cs
    importlib.reload(cs)
    PORT = 7975
    panel = cs.Panel()
    srv = cs.BoundedThreadingHTTPServer(("127.0.0.1", PORT), cs.Handler, max_workers=8)
    srv.panel = panel
    from jobs import JobRunner
    srv.jobs = JobRunner(panel, judge_timeout_s=30)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(1.0)

    def call(token):
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/stats",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            return urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code

    good = authtoken.make_token(SECRET)  # current real-clock token
    check("derived current token authenticates (200)", call(good) == 200, f"code={call(good)}")
    check("garbage token rejected (401)", call("not-a-real-token") == 401)
    check("a stale window token rejected (401)",
          call(authtoken.make_token(SECRET, now=time.time() - 5 * W)) == 401)

    h = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=10))
    check("/health reports rotation enabled", (h.get("token_rotation") or {}).get("enabled") is True,
          str(h.get("token_rotation")))
    check("/health never exposes the secret or a token",
          SECRET not in json.dumps(h) and good not in json.dumps(h))
    srv.shutdown()

    print("scenario: static token still works (back-compat)")
    os.environ.pop("CRITIQUE_ROTATION_SECRET", None)
    os.environ["CRITIQUE_TOKEN"] = "static-classic"
    importlib.reload(authtoken); importlib.reload(cs)
    PORT2 = 7976
    panel2 = cs.Panel()
    srv2 = cs.BoundedThreadingHTTPServer(("127.0.0.1", PORT2), cs.Handler, max_workers=8)
    srv2.panel = panel2
    srv2.jobs = JobRunner(panel2, judge_timeout_s=30)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    time.sleep(1.0)
    def call2(token):
        req = urllib.request.Request(f"http://127.0.0.1:{PORT2}/api/stats",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            return urllib.request.urlopen(req, timeout=10).status
        except urllib.error.HTTPError as e:
            return e.code
    check("static token authenticates", call2("static-classic") == 200)
    check("wrong static token rejected", call2("nope") == 401)
    srv2.shutdown()

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL AUTH-TOKEN SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
