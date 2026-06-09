from __future__ import annotations
import asyncio
import os
import secrets
import threading
import time

import httpx

import orchestrate
from content.roles import Roles, looks_like_refusal
from merge import merge_critiques, merge_panel
from oplog import log_event
from orchestrator.orchestrator import execute as orchestrator_execute
from scheduler import ProviderError, call_slot

# Panel execution with cross-provider failover.
#
# Each judge is still COMPLETELY independent - if one stalls or fails, the others
# proceed as if nothing happened, and a slow frontier model never gates the HTTP
# response. What changed from the original decoupled design: a judge now names a
# LOGICAL model and routes through the shared SlotScheduler, so on a 429/5xx/empty
# it BOUNCES to the next provider hosting the same model instead of just failing.
# Every attempt emits a per-profile metric. The two execution modes share the same
# per-judge primitive (`route_judge`, built on `call_once`):
#   - synchronous  : run_panel_slots() - asyncio.gather, returns when all settle.
#   - asynchronous : JobRunner        - fire-and-forget tasks on a background loop;
#                                        the client polls GET /api/jobs/<id> and
#                                        sees each judge flip pending->running->done
#                                        independently, with partial results.

JOB_TTL_S = 6 * 3600.0   # keep finished jobs pollable for a while, then drop

# Cross-provider failover owns retries now: instead of retrying the SAME slot,
# route_judge BOUNCES a logical model to the next provider that hosts it. So the
# legacy same-slot retry is off by default (set JUDGE_RETRIES>0 to re-enable a
# small same-slot retry on top of failover). every failure - throttle, 5xx, empty,
# hard 4xx - is a reason to try a different provider hosting the same model.
JUDGE_RETRIES = int(os.environ.get("JUDGE_RETRIES", "0"))
JUDGE_BACKOFF_S = float(os.environ.get("JUDGE_BACKOFF_S", "4"))
RETRYABLE_CODES = {"429", "5xx", "524", "http", "empty"}

# eligibility role for slot picking. judges here can play any role (slots carry the
# full role set), so we use a fixed role for the scheduler's eligibility check and
# record the REAL task role separately in metrics.
_PICK_ROLE = Roles("critiquer")


async def call_once(client: httpx.AsyncClient, picked, system_prompt: str, *,
                    user_msg: str, max_tokens: int, timeout_s: float,
                    extra_body: dict | None = None) -> dict:
    #   exactly one http exchange against one slot. never raises; returns a
    #   structured result carrying ok / code / usage / latency. extra_body carries
    #   per-model reasoning/thinking fields.
    t0 = time.monotonic()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    try:
        content, usage = await call_slot(client, picked, messages=messages,
                                         max_tokens=max_tokens, timeout_s=timeout_s,
                                         extra_body=extra_body)
        text = content.strip()
        if looks_like_refusal(text):
            res = {"ok": False, "code": "refusal", "error": f"refusal: {text[:160]!r}", "usage": usage}
        else:
            res = {"ok": True, "code": "ok", "output": text, "usage": usage}
    except ProviderError as e:
        msg = str(e)
        res = {"ok": False, "code": msg.split(":", 1)[0], "error": msg[:300], "usage": {}}
    except Exception as e:  # noqa: BLE001 - isolate every failure mode per judge
        res = {"ok": False, "code": "exc", "error": f"{type(e).__name__}: {str(e)[:180]}", "usage": {}}
    res["elapsed_s"] = round(time.monotonic() - t0, 1)
    return res


async def route_judge(scheduler, client, logical: str, candidates: list, system_prompt: str, *,
                      role_label: str, effort: str, profile: str, metrics,
                      user_msg: str, max_tokens: int, timeout_s: float,
                      nonce: str = "", reasoning: bool = False, research: bool = False,
                      reasoning_catalog: dict | None = None) -> dict:
    #   run one logical judge with cross-provider failover. asks the scheduler for
    #   the best eligible host, calls it, and on ANY failure records the penalty
    #   (cooldown/blacklist) + a metric event and bounces to the next provider that
    #   hosts the same model. every attempt is captured per-profile. never raises.
    #   reasoning -> merge the model's thinking params; research -> web ReAct loop.
    tried: set[str] = set()
    candidates_tried: list[str] = []
    attempts = 0
    last = None
    if not candidates:
        return {"model": logical, "ok": False, "routed_to": None, "attempts": 0,
                "candidates_tried": [], "elapsed_s": 0.0,
                "error": "not registered (no configured provider hosts this model)"}

    max_hops = len(candidates) + JUDGE_RETRIES
    for _ in range(max_hops):
        picked = scheduler.pick_slot_from(candidates, _PICK_ROLE, exclude_providers=tried)
        if picked is None:
            break
        attempts += 1
        candidates_tried.append(picked.who)
        await scheduler.wait_for_provider_pacing(picked)
        #   resolve this model's thinking params when deep reasoning is requested.
        extra_body = None
        if (reasoning or research) and reasoning_catalog is not None:
            from providers import resolve_reasoning_body
            extra_body = resolve_reasoning_body(
                reasoning_catalog, logical=logical, who=picked.who,
                family=picked.model_family) or None
        if research:
            import agent
            res = await agent.research_call(
                client, picked, system_prompt, user_msg=user_msg,
                max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body)
        else:
            res = await call_once(client, picked, system_prompt, user_msg=user_msg,
                                  max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body)
        usage = res.get("usage") or {}
        in_tok, out_tok = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if res["ok"]:
            scheduler.record_success(picked)
            _emit(metrics, profile, logical, picked, role_label, effort, res,
                  in_tok, out_tok, attempts, candidates_tried, res.get("output"))
            out = {"model": logical, "routed_to": picked.who, "ok": True,
                   "output": res["output"], "elapsed_s": res["elapsed_s"],
                   "attempts": attempts, "candidates_tried": list(candidates_tried)}
            if usage.get("reasoning_content"):
                out["reasoning"] = usage["reasoning_content"]
            for k in ("steps", "searches", "tool_calls"):
                if res.get(k) is not None:
                    out[k] = res[k]
            if nonce:
                #   panel-mode artifacts: each model's reply becomes its own file
                #   tree (kept fully separate; nothing merged).
                import artifacts as _artifacts
                out["artifacts"] = _artifacts.parse_artifacts(res["output"], nonce)
            return out
        #       failure: penalise the slot, record the metric, bounce providers.
        code = res["code"]
        scheduler.record_failure(picked, code, res.get("error", ""))
        _emit(metrics, profile, logical, picked, role_label, effort, res,
              in_tok, out_tok, attempts, candidates_tried, None)
        _budget_guard(scheduler, metrics, profile, picked)
        tried.add(picked.provider.name)
        last = res
        log_event("judge_failover", logical=logical, slot=picked.who, code=code,
                  attempt=attempts, remaining=len(candidates) - len(tried))

    err = (last or {}).get("error", "all candidate providers cooling/blacklisted/unconfigured")
    return {"model": logical, "ok": False, "routed_to": None,
            "error": err[:300], "attempts": attempts,
            "candidates_tried": list(candidates_tried),
            "elapsed_s": (last or {}).get("elapsed_s", 0.0)}


def _emit(metrics, profile, logical, picked, role_label, effort, res,
          in_tok, out_tok, attempts, candidates_tried, output) -> None:
    if metrics is None:
        return
    try:
        _reason = (res.get("usage") or {}).get("reasoning_content")
        metrics.record(
            profile=profile, logical=logical, provider=picked.provider.name,
            model=picked.model, family=picked.model_family, role=role_label,
            effort=effort, latency_s=res.get("elapsed_s", 0.0),
            in_tokens=in_tok, out_tokens=out_tok, ok=res["ok"], code=res["code"],
            attempts=attempts, routed_to=picked.who,
            candidates_tried=list(candidates_tried), mock=_is_mock(), output=output,
            steps=res.get("steps"), searches=res.get("searches"),
            tool_calls=res.get("tool_calls"),
            reasoning_chars=(len(_reason) if _reason else None),
        )
    except Exception as e:  # noqa: BLE001 - metrics must never break a judge
        log_event("metrics_record_error", error=repr(e)[:200])


def _is_mock() -> bool:
    import mock_provider
    return mock_provider.ENABLED


def _budget_guard(scheduler, metrics, profile: str, picked) -> None:
    #   if this profile has spent the provider's published daily request/token
    #   budget, cool the whole provider until ~midnight so we stop routing to it.
    if metrics is None:
        return
    prov = picked.provider
    if not (prov.rpd or prov.tpd):
        return
    try:
        calls, tokens = metrics.provider_day_usage(profile, prov.name)
    except Exception:  # noqa: BLE001
        return
    over = (prov.rpd and calls >= prov.rpd) or (prov.tpd and tokens >= prov.tpd)
    if over:
        scheduler.set_budget_cooldown(prov.name, time.time() + 3600.0,
                                      reason=f"daily budget: calls={calls}/{prov.rpd} tok={tokens}/{prov.tpd}")


async def run_panel_slots(panel, who_list: list[str], system_prompt: str, *,
                          role_label: str, effort: str, profile: str, metrics,
                          user_msg: str, max_tokens: int, timeout_s: float,
                          nonce: str = "", reasoning: bool = False,
                          research: bool = False) -> list[dict]:
    #   synchronous fan-out: every logical judge in parallel, each with its own
    #   cross-provider failover. returns once all have settled.
    resolved = [(who, *panel.resolve_candidates(who)) for who in who_list]
    rcat = getattr(panel, "reasoning_catalog", None)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[
            route_judge(panel.scheduler, client, logical, candidates, system_prompt,
                        role_label=role_label, effort=effort, profile=profile,
                        metrics=metrics, user_msg=user_msg, max_tokens=max_tokens,
                        timeout_s=timeout_s, nonce=nonce, reasoning=reasoning,
                        research=research, reasoning_catalog=rcat)
            for (_who, logical, candidates) in resolved
        ])
    return results


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
               max_tokens: int, role: str, merge_mode: str, kind: str,
               profile: str = "default", effort: str = "med",
               timeout_s: float | None = None, nonce: str = "",
               reasoning: bool = False, research: bool = False) -> dict:
        job_id = secrets.token_hex(8)
        judges: dict[str, dict] = {}
        scheduled = []
        for who in who_list:
            logical, candidates = self.panel.resolve_candidates(who)
            if not candidates:
                judges[who] = {"model": who, "ok": False, "status": "error",
                               "error": "not registered (no configured provider hosts this model)"}
            else:
                judges[who] = {"model": who, "status": "pending"}
                scheduled.append((who, logical, candidates))
        job = {"id": job_id, "type": "panel", "kind": kind, "role": role, "merge": merge_mode,
               "profile": profile, "effort": effort,
               "created": time.time(), "judges": judges, "total": len(who_list)}
        with self.lock:
            self._prune_locked()
            self.jobs[job_id] = job
        jt = timeout_s or self.judge_timeout_s
        #   schedule each logical judge as its own task; each does cross-provider
        #   failover through the shared scheduler + emits per-profile metrics.
        for who, logical, candidates in scheduled:
            asyncio.run_coroutine_threadsafe(
                self._run_judge(job_id, who, logical, candidates, system_prompt,
                                user_msg, max_tokens, role, profile, effort, jt, nonce,
                                reasoning, research),
                self.loop,
            )
        log_event("job_submitted", job_id=job_id, req_kind=kind, role=role,
                  profile=profile, effort=effort, research=research, judges=len(who_list))
        return self.snapshot(job_id)

    async def _run_judge(self, job_id: str, who: str, logical: str, candidates: list,
                         system_prompt: str, user_msg: str, max_tokens: int,
                         role_label: str, profile: str, effort: str,
                         timeout_s: float, nonce: str = "",
                         reasoning: bool = False, research: bool = False) -> None:
        with self.lock:
            j = self.jobs.get(job_id)
            if j is not None:
                j["judges"][who] = {"model": who, "status": "running"}
        res = await route_judge(
            self.panel.scheduler, self._client, logical, candidates, system_prompt,
            role_label=role_label, effort=effort, profile=profile,
            metrics=self.panel.metrics, user_msg=user_msg, max_tokens=max_tokens,
            timeout_s=timeout_s, nonce=nonce, reasoning=reasoning, research=research,
            reasoning_catalog=getattr(self.panel, "reasoning_catalog", None))
        res["model"] = who
        res["status"] = "done" if res.get("ok") else "error"
        with self.lock:
            j = self.jobs.get(job_id)
            if j is not None:
                j["judges"][who] = res
        log_event("job_judge_settled", job_id=job_id, model=who,
                  routed_to=res.get("routed_to"), ok=res.get("ok"),
                  elapsed_s=res.get("elapsed_s"))

    def submit_run(self, *, schematic, prompt: str, nonce: str,
                   profile: str = "default", effort: str = "med",
                   plan_mode: bool = False) -> dict:
        #   schedule a multi-stage orchestrator run on the background loop. when
        #   plan_mode, `schematic` is None and the planner builds it inside the task.
        job_id = secrets.token_hex(8)
        job = {"id": job_id, "type": "run",
               "task": (schematic.task if schematic is not None else "(planning)"),
               "task_type": (schematic.task_type if schematic is not None else "auto"),
               "profile": profile, "effort": effort,
               "created": time.time(), "status": "running", "result": None,
               "stage_log": []}
        with self.lock:
            self._prune_locked()
            self.jobs[job_id] = job
        asyncio.run_coroutine_threadsafe(
            self._run_orchestration(job_id, schematic, prompt, profile, effort, nonce, plan_mode),
            self.loop,
        )
        log_event("run_submitted", job_id=job_id, task=job["task"],
                  task_type=job["task_type"], profile=profile, effort=effort, plan_mode=plan_mode)
        return self.snapshot(job_id)

    async def _run_orchestration(self, job_id, schematic, prompt, profile, effort,
                                 nonce, plan_mode=False) -> None:
        def _log(msg: str) -> None:
            with self.lock:
                j = self.jobs.get(job_id)
                if j is not None:
                    j["stage_log"].append(str(msg)[:200])
                    j["stage_log"] = j["stage_log"][-50:]
        try:
            if plan_mode:
                _log("[planner] building schematic from prompt")
                schematic, nonce = await orchestrate.plan_now(
                    prompt, self.panel.scheduler, self._client, log=_log)
                with self.lock:
                    j = self.jobs.get(job_id)
                    if j is not None:
                        j["task"], j["task_type"] = schematic.task, schematic.task_type
            res = await orchestrator_execute(
                schematic, self.panel.scheduler, self._client,
                initial_context={"prompt": prompt}, log=_log,
                metrics=self.panel.metrics, profile=profile, effort=effort, nonce=nonce)
            serialized = orchestrate.serialize_run_result(res, task=schematic.task)
        except Exception as e:  # noqa: BLE001 - a run must never crash the loop
            task = schematic.task if schematic is not None else "(planning failed)"
            serialized = {"ok": False, "task": task, "body": None,
                          "artifacts": [], "error": f"{type(e).__name__}: {str(e)[:200]}",
                          "judge": {"hard_fails": [], "soft_flags": []}, "stages": []}
        with self.lock:
            j = self.jobs.get(job_id)
            if j is not None:
                j["status"] = "complete"
                j["result"] = serialized
        log_event("run_settled", job_id=job_id, ok=serialized.get("ok"),
                  rounds=serialized.get("rounds"), error=serialized.get("error"))

    def _run_snapshot_locked(self, job: dict) -> dict:
        return {
            "job_id": job["id"], "type": "run", "status": job["status"],
            "task": job["task"], "task_type": job["task_type"],
            "profile": job["profile"], "effort": job["effort"],
            "result": job.get("result"),
            "stage_log": list(job.get("stage_log", []))[-12:],
            "meta": {"complete": job["status"] == "complete",
                     "age_s": round(time.time() - job["created"], 1)},
        }

    def snapshot(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job.get("type") == "run":
                return self._run_snapshot_locked(job)
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
