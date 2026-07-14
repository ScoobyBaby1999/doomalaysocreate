from __future__ import annotations
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Live, provider-by-provider smoke against the deployed Space. Tests each
# CONFIGURED provider one at a time with a basic AND a frontier model, using a
# quick-but-formidable prompt (reveals real reasoning, finishes in seconds), then
# proves the orchestrator + multi-file artifacts end-to-end with one /api/run.
#
#   CRITIQUE_URL=https://<you>.hf.space CRITIQUE_TOKEN=... python tools/live_provider_test.py
#
# Forces a specific provider/model by passing a physical "provider/model" slot as
# the panel override, so each call exercises exactly one carrier.

URL = os.environ.get("CRITIQUE_URL", "").rstrip("/")
TOKEN = os.environ.get("CRITIQUE_TOKEN", "")

# (basic, frontier) physical slots per provider.
PROVIDER_MODELS = {
    "nvidia":        ("nvidia/meta/llama-3.3-70b-instruct", "nvidia/z-ai/glm-5.1"),
    "cerebras":      ("cerebras/llama3.1-8b", "cerebras/qwen-3-235b-a22b-instruct-2507"),
    "openrouter":    ("openrouter/google/gemma-3-27b-it:free", "openrouter/z-ai/glm-4.5-air:free"),
    "groq":          ("groq/llama-3.1-8b-instant", "groq/llama-3.3-70b-versatile"),
    "cloudflare":    ("cloudflare/@cf/meta/llama-3.1-8b-instruct-fast", "cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
    "github-models": ("github-models/openai/gpt-4o-mini", "github-models/deepseek/deepseek-v3-0324"),
    "google":        ("google/gemini-2.0-flash", "google/gemini-2.5-pro"),
    "zai":           ("zai/glm-4.5-air", "zai/glm-5.1"),
    "moonshot":      ("moonshot/kimi-k2-0905-preview", "moonshot/kimi-k2.6"),
}

# quick but formidable: the empty-list ZeroDivisionError is an easy tell of real
# reasoning vs. surface pattern-matching, and finishes in a few seconds.
BUG_PROMPT = (
    "Find the single most important bug in this Python function and give the "
    "corrected version. Be concise (<=120 words).\n\n"
    "```python\ndef average(nums):\n    return sum(nums) / len(nums)\n```"
)


def call(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read()), time.monotonic() - t0
        except Exception:
            return e.code, {"error": f"HTTP {e.code}"}, time.monotonic() - t0
    except Exception as e:
        return 0, {"error": repr(e)[:160]}, time.monotonic() - t0


def panel_one(slot, profile):
    body = {"input": BUG_PROMPT, "role": "generator", "panel": [slot],
            "merge": "none", "max_tokens": 350, "effort": "low", "profile": profile}
    status, resp, dt = call("POST", "/api/panel", body, timeout=150)
    if status != 200:
        return False, None, dt, f"HTTP {status}: {resp.get('error')}"
    judges = resp.get("judges", [])
    j = judges[0] if judges else {}
    ok = bool(j.get("ok"))
    snippet = (j.get("output") or j.get("error") or "")[:90].replace("\n", " ")
    return ok, j.get("routed_to"), dt, snippet


def main():
    if not URL or not TOKEN:
        raise SystemExit("set CRITIQUE_URL and CRITIQUE_TOKEN")
    status, health, _ = call("GET", "/health", timeout=40)
    configured = set(health.get("providers_configured", []))
    has_run = any("/api/run" in k for k in (health.get("endpoints") or {}))
    print(f"Space: {URL}")
    print(f"providers_configured: {sorted(configured)}")
    print(f"has /api/run (new code deployed): {has_run}\n")

    print("== per-provider basic + frontier (POST /api/panel, one model each) ==")
    for prov in PROVIDER_MODELS:
        if prov not in configured:
            continue
        basic, frontier = PROVIDER_MODELS[prov]
        for tier, slot in (("basic", basic), ("frontier", frontier)):
            ok, routed, dt, info = panel_one(slot, profile=f"live_{prov}")
            flag = "OK " if ok else "FAIL"
            print(f"  [{flag}] {prov:13} {tier:8} {dt:5.1f}s  {slot}")
            print(f"         -> {info}")

    if has_run:
        print("\n== orchestrator + multi-file artifacts (POST /api/run, async) ==")
        schem = {"task_type": "code_spec", "task": "reverse-lines CLI",
                 "stages": [{"name": "build", "role": "generator",
                             "instructions": "Create a tiny Python CLI that reverses the lines of a file, "
                                             "with a short README. Keep it minimal.",
                             "inputs": ["prompt"], "max_tokens": 1500}],
                 "output_rules": {"format": "files"},
                 "judge_config": {"rules": [], "plugins": [], "llm_judges": []}, "max_rounds": 1}
        status, snap, _ = call("POST", "/api/run",
                               {"prompt": "Reverse the lines of a text file.", "schematic": schem,
                                "profile": "live_run", "async": True})
        jid = snap.get("job_id")
        print(f"  submitted run job_id={jid}")
        result = {}
        for _ in range(40):
            time.sleep(3)
            _, jp, _ = call("GET", f"/api/jobs/{jid}", timeout=40)
            if jp.get("status") == "complete":
                result = jp.get("result") or {}
                break
        files = [a["path"] for a in result.get("artifacts", [])]
        print(f"  run ok={result.get('ok')} artifacts={files} routed={result.get('providers_used')}")
    else:
        print("\n(skipping /api/run — Space is on old code; deploy this branch first)")

    print("\n== /api/stats rotation + /api/metrics ==")
    _, stats, _ = call("GET", "/api/stats", timeout=40)
    if isinstance(stats, dict) and "providers" in stats:
        print("  provider calls:", {k: v.get("calls") for k, v in stats["providers"].items()})
    else:
        print("  /api/stats not available (old code)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
