from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

# Quota-free test of codebase ingestion: pack filtering, budget fitting (drop order),
# render shape, and the 'files' request form end-to-end with per-judge coverage.
#   MOCK_MODE=1 python tools/sim_repopack.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("DOOMALAYSOCREATE_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ["CACHE_ENABLED"] = "0"
os.environ.pop("METRICS_PUBLIC_HF_REPO", None)
os.environ.pop("OPTIN_PROVIDERS", None)
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import critique_service as cs  # noqa: E402
import repopack  # noqa: E402

FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)


def main() -> int:
    print("scenario: pack filtering (binaries, lockfiles, excluded dirs)")
    raw = {
        "src/app.py": "def main():\n    return 1\n" * 50,
        "src/util.py": "x = 1\n" * 30,
        "tests/test_app.py": "def test_main():\n    assert True\n" * 20,
        "docs/guide.md": "# Guide\nwords here\n" * 40,
        "assets/logo.png": "\x00\x89PNG-binary-bytes",
        "package-lock.json": '{"lockfileVersion": 2}',
        "node_modules/dep/index.js": "module.exports = 1",
        "big/generated_schema.json": '{"a": 1}\n' * 4000,
    }
    pack = repopack.pack_files(raw)
    check("text sources kept", set(pack.files) >= {"src/app.py", "src/util.py", "tests/test_app.py", "docs/guide.md"})
    skipped_paths = {s["path"] for s in pack.skipped}
    check("binary skipped (NUL)", "assets/logo.png" in skipped_paths)
    check("lockfile skipped by default", "package-lock.json" in skipped_paths)
    check("node_modules skipped", "node_modules/dep/index.js" in skipped_paths)
    pack_l = repopack.pack_files(raw, include_lockfiles=True)
    check("lockfile kept when opted in", "package-lock.json" in pack_l.files)

    print("scenario: budget fitting drops generated/big first, tests/docs last")
    budget = pack.total_tokens - repopack.estimate_tokens(
        "big/generated_schema.json", raw["big/generated_schema.json"]) + 10
    reduced, dropped = repopack.fit_to_budget(pack, budget)
    check("generated file dropped first", dropped and dropped[0] == "big/generated_schema.json", f"dropped={dropped}")
    check("tests/docs survived the cut",
          "tests/test_app.py" in reduced.files and "docs/guide.md" in reduced.files)
    same, none_dropped = repopack.fit_to_budget(pack, pack.total_tokens + 1000)
    check("fitting is a no-op when within budget", not none_dropped)

    print("scenario: render shape (tree, omitted section, file delimiters)")
    blob = repopack.render(reduced, dropped=dropped)
    check("tree header present", blob.startswith("# REPOSITORY PACK"))
    check("omitted section names the dropped file",
          "you have NOT seen these" in blob and "big/generated_schema.json" in blob)
    check("file delimiter present", "===== FILE: src/app.py =====" in blob)

    print("scenario: 'files' form end-to-end with per-judge budget + coverage")
    panel = cs.Panel()
    #   llama-3.3-70b's best host ctx is 24000; kimi-k2.6's is 262144. build a repo
    #   that fits kimi whole but forces a reduced pack for llama.
    big_repo = {f"mod_{i}/impl.py": ("def f():\n    pass\n" * 800) for i in range(12)}
    big_repo["tests/test_core.py"] = "def test():\n    assert True\n" * 100
    payload = {"files": big_repo, "role": "critiquer", "panel": ["kimi-k2.6", "llama-3.3-70b"],
               "effort": "high", "max_tokens": 2000, "privacy": "off", "profile": "p_repo"}
    handler = cs.Handler.__new__(cs.Handler)  # use the param builder without a socket
    params = handler._params_panel(payload, panel)
    pj = params.get("per_judge", {})
    check("pack_meta computed", params.get("pack_meta", {}).get("files") == 13,
          str(params.get("pack_meta")))
    check("kimi fits the full pack (no reduced prompt)",
          "system_prompt" not in pj.get("kimi-k2.6", {}), str(pj.get("kimi-k2.6", {}).get("coverage")))
    check("llama got a REDUCED pack", "system_prompt" in pj.get("llama-3.3-70b", {}))
    cov = pj.get("llama-3.3-70b", {}).get("coverage", {})
    check("llama coverage reports partial view",
          0 < cov.get("files_included", 0) < cov.get("files_total", 99), str(cov))
    check("tests survived llama's cut", not any("tests/" in d for d in cov.get("dropped", [])),
          f"dropped={cov.get('dropped')}")

    res = asyncio.run(cs.run_sync(panel, params))
    check("panel ran ok over the pack", res["meta"]["judges_ok"] == 2, str(res["meta"]))
    by_model = {j["model"]: j for j in res["judges"]}
    check("response carries pack meta", res.get("pack", {}).get("files") == 13)
    check("llama judge result carries coverage",
          by_model.get("llama-3.3-70b", {}).get("coverage", {}).get("files_included", 0) > 0)

    print("scenario: oversized for EVERY budget -> clear 400")
    #   every file individually exceeds llama's ~20k-token budget, so fit_to_budget
    #   cannot keep even one -> the request must fail with the explicit budget error.
    huge = {f"f{i}.py": "x = 1\n" * 60000 for i in range(3)}
    try:
        handler._params_panel({"files": huge, "role": "critiquer",
                               "panel": ["llama-3.3-70b"], "max_tokens": 2000,
                               "effort": "high", "privacy": "off"}, panel)
        check("oversized pack -> explicit 400 with budget math", False, "no error raised")
    except cs._BadRequest as e:
        check("oversized pack -> explicit 400 with budget math", "cannot fit" in str(e), str(e)[:90])

    print("scenario: request validation")
    for bad, why in [({"files": {}, "role": "critiquer"}, "empty files"),
                     ({"files": {"a.py": 1}, "role": "critiquer"}, "non-string content"),
                     ({"files": {"a.py": "x"}, "input": "hi", "role": "critiquer"}, "files+input"),
                     ({"files": {"a.py": "x"}, "artifacts": True, "role": "critiquer"}, "files+artifacts"),
                     ({"repo_url": "https://gitlab.com/x/y", "role": "critiquer"}, "non-github url")]:
        try:
            handler._params_panel(bad, panel)
            check(f"rejects {why}", False)
        except cs._BadRequest:
            check(f"rejects {why}", True)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL REPOPACK SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
