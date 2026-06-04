from __future__ import annotations
import asyncio
import secrets
import threading
import time

import httpx

from content.roles import looks_like_refusal
from merge import merge_critiques, merge_panel
from oplog import log_event
from scheduler import ProviderError, call_slot

# Decoupled panel execution.
#
# The user's requirement: judges must be COMPLETELY independent - if one stalls
# or fails, the others proceed as if nothing happened, and a slow frontier model
# (DeepSeek V4 Pro can think for minutes) must never gate the HTTP response.
#
# So there is no shared scheduler, no rotation, no cross-judge semaphore: each
# judge is its own coroutine calling exactly its assigned model. Two execution
# modes share the same per-judge primitive (`call_judge`):
#   - synchronous  : run_panel_slots() - asyncio.gather, returns when all settle.
#   - asynchronous : JobRunner        - fire-and-forget tasks on a background loop;
#                                        the client polls GET /api/jobs/<id> and
#                                        sees each judge flip pending->running->done
#                                        independently, with partial results.

JOB_TTL_S = 6 * 3600.0   # keep finished jobs pollable for a while, then drop


async def call_judge(client: httpx.AsyncClient, picked, system_prompt: str, *,
                     user_msg: str, max_tokens: int, timeout_s: float) -> dict:
    #   one model, one call. never raises - always returns a result dict so a
    #   failure is just data, not an exception that could disturb a sibling.
    t0 = time.monotonic()
    try:
        content = await call_slot(
            client, picked,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max_tokens, timeout_s=timeout_s,
        )
        text = content.strip()
        if looks_like_refusal(text):
            res = {"model": picked.who, "ok": False, "error": f"refusal: {text[:160]!r}"}
        else:
            res = {"model": picked.who, "ok": True, "output": text}
    except ProviderError as e:
        res = {"model": picked.who, "ok": False, "error": str(e)[:300]}
    except Exception as e:  # noqa: BLE001 - isolate every failure mode per judge
        res = {"model": picked.who, "ok": False, "error": f"{type(e).__name__}: {str(e)[:180]}"}
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    return res


async def run_panel_slots(panel, who_list: list[str], system_prompt: str, *,
                          user_msg: str, max_tokens: int, timeout_s: float) -> list[dict]:
    #   synchronous fan-out: every judge in parallel, fully independent. returns
    #   once all have settled (or hit timeout). unresolved models report ok:false.
    picked = []
    missing = []
    for who in who_list:
        s = panel.resolve(who)
        if s is None:
            missing.append({"model": who, "ok": False,
                            "error": "not registered (provider has no API key configured)"})
        else:
            picked.append(s)
    results: list[dict] = []
    if picked:
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(*[
                call_judge(client, s, system_prompt, user_msg=user_msg,
                           max_tokens=max_tokens, timeout_s=timeout_s)
                for s in picked
            ])
    return results + missing


def finalize_judges(judges: list[dict], kind: str, merge_mode: str) -> tuple[list[dict], str]:
    #   shape the per-judge dicts + compute the merged view. for the legacy
    #   critique kind, each judge's text is exposed as "critique" (not "output").
    out = [dict(j) for j in judges]
    if kind == "critique":
        for j in out:
            if j.get("ok") and "output" in j:
                j["critique"] = j.pop("output")
        merged = merge_critiques(out)
    else:
        merged = merge_panel(out, merge_mode)
    return out, merged


class JobRunner:
    """Runs panels as fire-and-forget jobs on a dedicated background event loop."""

    def __init__(self, panel, *, judge_timeout_s: float) -> None:
        self.panel = panel
        self.judge_timeout_s = judge_timeout_s
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self._client: httpx.AsyncClient | None = None
        ready = threading.Event()
        threading.Thread(target=self._run_loop, args=(ready,), daemon=True).start()
        ready.wait(10)

    def _run_loop(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self.loop)
        self._client = httpx.AsyncClient()
        self.loop.call_soon(ready.set)
        self.loop.run_forever()

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [jid for jid, v in self.jobs.items() if now - v["created"] > JOB_TTL_S]
        for jid in stale:
            self.jobs.pop(jid, None)

    def submit(self, *, who_list: list[str], system_prompt: str, user_msg: str,
               max_tokens: int, role: str, merge_mode: str, kind: str) -> dict:
        job_id = secrets.token_hex(8)
        judges: dict[str, dict] = {}
        picked = []
        for who in who_list:
            s = self.panel.resolve(who)
            if s is None:
                judges[who] = {"model": who, "ok": False, "status": "error",
                               "error": "not registered (provider has no API key configured)"}
            else:
                judges[who] = {"model": who, "status": "pending"}
                picked.append((who, s))
        job = {"id": job_id, "kind": kind, "role": role, "merge": merge_mode,
               "created": time.time(), "judges": judges, "total": len(who_list)}
        with self.lock:
            self._prune_locked()
            self.jobs[job_id] = job
        #   schedule each judge as its own task - independent, no shared scheduler.
        for who, s in picked:
            asyncio.run_coroutine_threadsafe(
                self._run_judge(job_id, who, s, system_prompt, user_msg, max_tokens),
                self.loop,
            )
        log_event("job_submitted", job_id=job_id, req_kind=kind, role=role, judges=len(who_list))
        return self.snapshot(job_id)

    async def _run_judge(self, job_id: str, who: str, picked, system_prompt: str,
                         user_msg: str, max_tokens: int) -> None:
        with self.lock:
            j = self.jobs.get(job_id)
            if j is not None:
                j["judges"][who] = {"model": who, "status": "running"}
        res = await call_judge(self._client, picked, system_prompt,
                               user_msg=user_msg, max_tokens=max_tokens,
                               timeout_s=self.judge_timeout_s)
        res["status"] = "done" if res.get("ok") else "error"
        with self.lock:
            j = self.jobs.get(job_id)
            if j is not None:
                j["judges"][who] = res
        log_event("job_judge_settled", job_id=job_id, model=who,
                  ok=res.get("ok"), elapsed_s=res.get("elapsed_s"))

    def snapshot(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            raw = [dict(v) for v in job["judges"].values()]
            kind, role, merge_mode = job["kind"], job["role"], job["merge"]
            created = job["created"]

        settled = [j for j in raw if j.get("status") in ("done", "error")]
        done_count = len(settled)
        total = len(raw)
        complete = done_count == total
        ok = sum(1 for j in raw if j.get("ok"))
        #   merge over whatever has finished so far (partial merges are useful).
        finished, merged = finalize_judges(settled, kind, merge_mode)
        #   include still-running judges in the judges list (without text) so the
        #   client can see exactly who is pending.
        pending = [j for j in raw if j.get("status") not in ("done", "error")]
        return {
            "job_id": job_id,
            "status": "complete" if complete else "running",
            "kind": kind,
            "role": role,
            "merge": merge_mode,
            "judges": finished + pending,
            "merged": merged,
            "meta": {
                "judges_ok": ok, "judges_total": total, "judges_settled": done_count,
                "complete": complete, "age_s": round(time.time() - created, 1),
            },
        }
