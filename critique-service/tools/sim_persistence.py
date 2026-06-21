from __future__ import annotations
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Quota-free test of job persistence + resume-on-boot. Simulates a crash/redeploy:
# a job is persisted mid-flight, a BRAND-NEW JobRunner boots on the same store, and
# we assert it reloads + reschedules the unfinished judge to completion while keeping
# already-finished work. MOCK_MODE=1 python tools/sim_persistence.py

os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("DOOMALAYSOCREATE_LOG", "0")
os.environ["CRITIQUE_TOKEN"] = "test"
os.environ.pop("METRICS_HF_REPO", None)
os.environ.pop("JOBS_HF_REPO", None)            # local-disk persistence only
for key in ("NVIDIA_API_KEY", "CF_API_TOKEN", "CF_ACCOUNT_ID", "OPENROUTER_API_KEY", "GITHUB_TOKEN"):
    os.environ[key] = "mock"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import metrics as metrics_mod  # noqa: E402
_TMP = Path(tempfile.mkdtemp(prefix="sim-persist-"))
metrics_mod.DATA_DIR = _TMP / "metrics"
metrics_mod.FLUSH_EVERY = 1
import critique_service as cs  # noqa: E402
from jobstore import JobStore  # noqa: E402
from jobs import JobRunner  # noqa: E402

FAILS: list[str] = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond: FAILS.append(name)


def wait_until(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    jobs_dir = _TMP / "jobs"
    panel = cs.Panel()

    print("scenario: a completed panel job is persisted to disk")
    r1 = JobRunner(panel, judge_timeout_s=30, store=JobStore(jobs_dir))
    snap = r1.submit(who_list=["llama-3.3-70b", "deepseek-v3"], system_prompt="review",
                     user_msg="go", max_tokens=200, role="critiquer", merge_mode="none",
                     kind="panel", profile="p_persist")
    jid = snap["job_id"]
    jf = jobs_dir / f"{jid}.json"
    check("job file written", jf.exists(), str(jf))
    ok = wait_until(lambda: (r1.snapshot(jid) or {}).get("meta", {}).get("complete"))
    check("original runner completed the job", ok)
    saved = json.loads(jf.read_text())
    check("persisted file has _exec for resume", bool(saved.get("_exec")))
    check("persisted judges dropped ephemeral progress",
          all("progress" not in j for j in saved["judges"].values()))

    print("scenario: a crash mid-flight -> new runner resumes the unfinished judge")
    #   forge an interrupted state: one judge done, one still 'running' (as a crash
    #   would leave it), exactly what the WAL would hold mid-job.
    crash = json.loads(jf.read_text())
    whos = list(crash["judges"].keys())
    done_who, pending_who = whos[0], whos[1]
    crash["judges"][done_who] = {"model": done_who, "status": "done", "ok": True,
                                 "output": "ORIGINAL", "routed_to": "x/y"}
    crash["judges"][pending_who] = {"model": pending_who, "status": "running"}
    JobStore(jobs_dir).save(crash)

    #   boot a brand-new runner on the same store (== a redeploy / restart).
    r2 = JobRunner(panel, judge_timeout_s=30, store=JobStore(jobs_dir))
    got = wait_until(lambda: (r2.snapshot(jid) or {}).get("meta", {}).get("complete"))
    check("new runner reloaded + completed the job", got, f"snap={r2.snapshot(jid)}")
    final = r2.snapshot(jid) or {}
    judges = {j["model"]: j for j in final.get("judges", [])}
    check("finished judge preserved (not re-run)",
          judges.get(done_who, {}).get("output") == "ORIGINAL", f"{judges.get(done_who)}")
    check("unfinished judge resumed to done",
          judges.get(pending_who, {}).get("status") == "done", f"{judges.get(pending_who)}")

    print("scenario: an interrupted run job is marked, not silently lost")
    run_id = "deadbeefcafe0001"
    run_job = {"id": run_id, "type": "run", "task": "t", "task_type": "auto",
               "profile": "p", "effort": "med", "created": time.time(),
               "status": "running", "result": None, "stage_log": ["mid-stage"]}
    JobStore(jobs_dir).save(run_job)
    r3 = JobRunner(panel, judge_timeout_s=30, store=JobStore(jobs_dir))
    rsnap = r3.snapshot(run_id) or {}
    check("interrupted run reloaded as 'interrupted'", rsnap.get("status") == "interrupted",
          f"status={rsnap.get('status')}")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): {FAILS}"); return 1
    print("ALL PERSISTENCE SCENARIOS PASSED (zero real API calls)"); return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(rc)
