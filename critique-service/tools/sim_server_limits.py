from __future__ import annotations
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Quota-free test of the production concurrency bounds: the bounded HTTP server returns
# 503 over its worker cap, drops slowloris connections at the socket timeout, and the
# JobRunner returns 429 past MAX_INFLIGHT_JOBS. MOCK_MODE=1 python tools/sim_server_limits.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("LOOM_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ["MAX_WORKERS"] = "2"
os.environ["REQUEST_TIMEOUT_S"] = "2"
os.environ["MAX_INFLIGHT_JOBS"] = "3"
os.environ["CACHE_ENABLED"] = "0"
os.environ.pop("METRICS_HF_REPO", None)
os.environ.pop("JOBS_HF_REPO", None)
os.environ.pop("OPTIN_PROVIDERS", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"
# slow every mock call so sync requests stay in-flight long enough to saturate workers.
os.environ["MOCK_LATENCY_S"] = "1.5"

import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import critique_service as cs  # noqa: E402

PORT = 7971
TOK = "test"
FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)


def _serve():
    panel = cs.Panel()
    srv = cs.BoundedThreadingHTTPServer(("127.0.0.1", PORT), cs.Handler, max_workers=2)
    srv.panel = panel
    from jobs import JobRunner
    srv.jobs = JobRunner(panel, judge_timeout_s=30)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _post(path, body, timeout=20):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, (json.load(e) if e.headers.get("Content-Type","").startswith("application/json") else {}), dict(e.headers)


def main() -> int:
    srv = _serve()
    time.sleep(1.5)

    print("scenario: over MAX_WORKERS concurrent sync requests -> 503 + Retry-After")
    results: list = []
    def hit():
        try:
            results.append(_post("/api/panel", {"input": "hi", "role": "verifier",
                                                "panel": ["llama-3.3-70b"], "effort": "low",
                                                "privacy": "off"}))
        except Exception as e:  # noqa: BLE001
            results.append(("EXC", repr(e)[:60], {}))
    # 6 concurrent against max_workers=2 + slow (1.5s) mock -> some must be rejected.
    threads = [threading.Thread(target=hit) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    codes = [r[0] for r in results]
    n503 = sum(1 for c in codes if c == 503)
    n200 = sum(1 for c in codes if c == 200)
    check("some requests succeeded", n200 >= 1, f"codes={codes}")
    check("excess requests got 503", n503 >= 1, f"codes={codes}")
    ra = [h.get("Retry-After") for c, _b, h in results if c == 503]
    check("503 carries Retry-After", any(ra), f"retry_after={ra}")

    print("scenario: slowloris connection dropped at REQUEST_TIMEOUT_S")
    time.sleep(3.0)  # let the prior slow sync requests drain so workers are free
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    #   valid auth so the handler proceeds to READ the body, then we send none -> the
    #   socket read must hit REQUEST_TIMEOUT_S and the worker is freed (not pinned).
    s.sendall(b"POST /api/panel HTTP/1.0\r\n"
              b"Authorization: Bearer " + TOK.encode() + b"\r\n"
              b"Content-Type: application/json\r\n"
              b"Content-Length: 1000\r\n\r\n")  # promise 1000 bytes, send none
    s.settimeout(8)
    t0 = time.time()
    try:
        data = s.recv(100)  # server should close (timeout) within ~REQUEST_TIMEOUT_S(2s)
        #   acceptable "not pinned" outcomes: connection closed (b""), a timeout/400 status,
        #   or an immediate 503 (capacity) - any means the worker isn't held for our 8s.
        dropped = (data == b"" or any(code in data for code in (b"400", b"408", b"503")))
    except (socket.timeout, ConnectionResetError, OSError):
        dropped = True
    elapsed = time.time() - t0
    s.close()
    check("slowloris dropped within the timeout (worker not pinned)", dropped and elapsed < 7,
          f"elapsed={elapsed:.1f}s data={data[:40] if 'data' in dir() else 'exc'!r}")

    print("scenario: async submits past MAX_INFLIGHT_JOBS -> 429 + Retry-After")
    codes2 = []
    for _ in range(6):
        c, _b, h = _post("/api/panel", {"input": "x", "role": "verifier",
                                        "panel": ["llama-3.3-70b"], "effort": "low",
                                        "privacy": "off", "async": True})
        codes2.append((c, h.get("Retry-After")))
    n429 = sum(1 for c, _ra in codes2 if c == 429)
    n202 = sum(1 for c, _ra in codes2 if c == 202)
    check("some async jobs accepted (<=cap)", n202 >= 1, f"codes={codes2}")
    check("async jobs past cap rejected with 429", n429 >= 1, f"codes={codes2}")
    check("429 carries Retry-After", any(ra for c, ra in codes2 if c == 429), f"codes={codes2}")

    print("scenario: /health exposes saturation counters")
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/health")
    h = json.load(urllib.request.urlopen(req, timeout=10))
    check("/health has workers.max", (h.get("workers") or {}).get("max") == 2, str(h.get("workers")))
    check("/health has jobs_inflight", "jobs_inflight" in h, str(h.get("jobs_inflight")))

    srv.shutdown()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL SERVER-LIMIT SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
