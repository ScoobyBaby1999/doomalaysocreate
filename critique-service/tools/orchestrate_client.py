from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Orchestrator client: drive a template/schematic on the hosted panel and
# MATERIALIZE the result — the final body plus any multi-file artifacts — into a
# real workspace so you (the orchestrator) reference files, not a wall of text.
#
#   python tools/orchestrate_client.py --template research_paper \
#       --input-file prompt.md --profile research --out runs/
#   python tools/orchestrate_client.py --schematic-file my_schematic.json \
#       --input "Build a CLI todo app in Python." --out runs/
#
# URL + token default to $CRITIQUE_URL / $CRITIQUE_TOKEN.


def _req(method, url, token, body=None, timeout=60.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"error": f"network: {e}"}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "run"


def submit(url, token, body, retries=2):
    for attempt in range(retries + 1):
        status, resp = _req("POST", f"{url}/api/run", token, body, timeout=120.0)
        if status in (200, 202):
            return status, resp
        if attempt < retries and status in (0, 503, 504):
            wait = 15 * (attempt + 1)
            print(f"  submit failed ({status}); cold start? waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        raise SystemExit(f"submit failed: HTTP {status}: {resp.get('error')}")
    raise SystemExit("submit failed after retries")


def poll(url, token, job_id, *, interval, timeout):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, snap = _req("GET", f"{url}/api/jobs/{job_id}", token, timeout=60.0)
        if status == 200:
            last = snap
            for line in snap.get("stage_log", [])[-3:]:
                print(f"    {line}", file=sys.stderr)
            if snap.get("status") == "complete":
                return snap.get("result") or {}
        time.sleep(interval)
    print("  timeout; returning last partial", file=sys.stderr)
    return (last or {}).get("result") or {}


def _safe_under(root: Path, rel: str) -> Path | None:
    #   defence in depth: never write outside the run dir even if the server
    #   somehow returned an unsafe path.
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


def materialize_run(result: dict, out_root: Path) -> Path:
    #   write a run result into out_root/<ts>-<task>/: each artifact file at its
    #   path, plus _body.md, _judge.md, _manifest.json. returns the run dir.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = out_root / f"{ts}-{_slug(result.get('task', 'run'))}"
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for f in result.get("artifacts", []):
        dest = _safe_under(out, f.get("path", ""))
        if dest is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.get("content", ""), encoding="utf-8")
        written.append(str(dest.relative_to(out)) + ("  (truncated)" if f.get("truncated") else ""))
    if result.get("body") and not result.get("artifacts"):
        (out / "_body.md").write_text(result["body"], encoding="utf-8")
        written.append("_body.md")
    judge = result.get("judge") or {}
    judge_md = "# Judge report\n\n"
    judge_md += "## Hard failures\n" + ("\n".join(f"- {h}" for h in judge.get("hard_fails", [])) or "- (none)") + "\n\n"
    judge_md += "## Soft flags\n" + ("\n".join(f"- {s}" for s in judge.get("soft_flags", [])) or "- (none)") + "\n"
    (out / "_judge.md").write_text(judge_md, encoding="utf-8")
    (out / "_manifest.json").write_text(json.dumps({
        "task": result.get("task"), "ok": result.get("ok"), "rounds": result.get("rounds"),
        "error": result.get("error"), "stages": result.get("stages", []),
        "providers_used": result.get("providers_used", []),
        "artifacts": [f.get("path") for f in result.get("artifacts", [])],
    }, indent=2), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Run an orchestrator template/schematic; materialize the result as files.")
    ap.add_argument("--url", default=os.environ.get("CRITIQUE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("CRITIQUE_TOKEN", ""))
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="prompt text inline")
    src.add_argument("--input-file", help="file holding the prompt text")
    tpl = ap.add_mutually_exclusive_group()
    tpl.add_argument("--template", help="built-in template id (e.g. research_paper, freeform)")
    tpl.add_argument("--schematic-file", help="path to a custom schematic JSON")
    ap.add_argument("--profile", default="default")
    ap.add_argument("--effort", default="med")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--sync", action="store_true", help="block instead of async+poll")
    ap.add_argument("--poll-interval", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=3600.0)
    args = ap.parse_args()
    if not args.url or not args.token:
        raise SystemExit("set --url/--token or CRITIQUE_URL/CRITIQUE_TOKEN")
    prompt = Path(args.input_file).read_text(encoding="utf-8") if args.input_file else args.input

    body = {"prompt": prompt, "profile": args.profile, "effort": args.effort,
            "async": not args.sync}
    if args.template:
        body["template"] = args.template
    if args.schematic_file:
        body["schematic"] = json.loads(Path(args.schematic_file).read_text(encoding="utf-8"))

    print(f"submitting run (template={args.template}, profile={args.profile})...", file=sys.stderr)
    status, resp = submit(args.url, args.token, body)
    if status == 200:          # sync
        result = resp
    else:                       # 202 async
        print(f"  job_id={resp.get('job_id')}", file=sys.stderr)
        result = poll(args.url, args.token, resp["job_id"],
                      interval=args.poll_interval, timeout=args.timeout)

    out = materialize_run(result, Path(args.out))
    print(f"\nrun {'OK' if result.get('ok') else 'INCOMPLETE'} -> {out}")
    for f in sorted(out.rglob("*")):
        if f.is_file():
            print(f"  {f}")
    if not result.get("ok"):
        print(f"note: {result.get('error') or 'judge still has hard fails'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
