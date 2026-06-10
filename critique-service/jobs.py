from __future__ import annotations
import asyncio
import copy
import os
import secrets
import threading
import time

import httpx

import orchestrate
from content.roles import Roles, looks_like_refusal
from jobstore import JobStore
from merge import merge_critiques, merge_panel
from oplog import log_event
from orchestrator.orchestrator import execute as orchestrator_execute
from providers import slot_is_privacy_safe
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
                    extra_body: dict | None = None,
                    system_in_user: bool = False,
                    on_progress=None) -> dict:
    #   exactly one http exchange against one slot. never raises; returns a
    #   structured result carrying ok / code / usage / latency. extra_body carries
    #   per-model reasoning/thinking fields. system_in_user folds the system prompt
    #   into the user message for hosts that strip/override custom system prompts
    #   (e.g. GitHub's Phi-4-reasoning). on_progress streams live output counters.
    t0 = time.monotonic()
    if system_in_user:
        messages = [{"role": "user", "content": f"{system_prompt}\n\n---\n\n{user_msg}"}]
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]
    try:
        content, usage = await call_slot(client, picked, messages=messages,
                                         max_tokens=max_tokens, timeout_s=timeout_s,
                                         extra_body=extra_body, on_progress=on_progress)
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


def _privacy_tiers(candidates: list, privacy: str) -> list[list]:
    #   partition a logical model's hosts by privacy posture. the router tries each
    #   tier in order, only descending to the next when the current one is exhausted.
    #     off      -> [all]                     (no filtering)
    #     strict   -> [safe]                     (NEVER touch a training/logging host)
    #     fallback -> [safe, unsafe]             (safe first; unsafe only as last resort)
    if privacy == "off":
        return [list(candidates)]
    safe = [c for c in candidates if slot_is_privacy_safe(c)]
    if privacy == "fallback":
        return [safe, [c for c in candidates if not slot_is_privacy_safe(c)]]
    return [safe]  # strict


def _pick_tiered(scheduler, tiers: list[list], tried: set[str]):
    #   pick the best eligible host from the highest-priority non-empty tier. only
    #   descends to a lower (less private) tier when the higher one has nothing left.
    for tier in tiers:
        if not tier:
            continue
        picked = scheduler.pick_slot_from(tier, _PICK_ROLE, exclude_providers=tried)
        if picked is not None:
            return picked
    return None


async def route_judge(scheduler, client, logical: str, candidates: list, system_prompt: str, *,
                      role_label: str, effort: str, profile: str, metrics,
                      user_msg: str, max_tokens: int, timeout_s: float,
                      nonce: str = "", reasoning: bool = False, research: bool = False,
                      reasoning_catalog: dict | None = None,
                      privacy: str = "off", cache=None, no_store: bool = False,
                      on_progress=None) -> dict:
    #   run one logical judge with cross-provider failover. asks the scheduler for
    #   the best eligible host, calls it, and on ANY failure records the penalty
    #   (cooldown/blacklist) + a metric event and bounces to the next provider that
    #   hosts the same model. every attempt is captured per-profile. never raises.
    #   reasoning -> merge the model's thinking params; research -> web ReAct loop.
    tried: set[str] = set()
    candidates_tried: list[str] = []
    trace: list[dict] = []          # per-hop {slot, code, latency_s} for ?trace=true
    attempts = 0
    last = None
    if not candidates:
        return {"model": logical, "ok": False, "routed_to": None, "attempts": 0,
                "candidates_tried": [], "trace": [], "elapsed_s": 0.0,
                "error": "not registered (no configured provider hosts this model)"}

    #   exact-repeat cache: a hit returns prior output with NO provider call (and sends
    #   no data anywhere - the result is already local to this single-tenant space).
    ckey = None
    if cache is not None:
        ckey = cache.key(logical=logical, system_prompt=system_prompt, user_msg=user_msg,
                         max_tokens=max_tokens, reasoning=reasoning, research=research,
                         privacy=privacy)
        hit = cache.get(ckey)
        if hit is not None:
            out = dict(hit)
            out.update(model=logical, ok=True, status="done", cached=True)
            log_event("judge_cache_hit", logical=logical, profile=profile)
            return out

    #   privacy router: try hosts by posture tier; strict never touches an unsafe host.
    tiers = _privacy_tiers(candidates, privacy)
    flat = [c for tier in tiers for c in tier]
    if not flat:
        return {"model": logical, "ok": False, "routed_to": None, "attempts": 0,
                "candidates_tried": [], "trace": [], "elapsed_s": 0.0,
                "code": "privacy_blocked",
                "error": (f"no privacy-safe host for '{logical}' (privacy={privacy}); "
                          "every configured host trains on or logs submissions")}

    max_hops = len(flat) + JUDGE_RETRIES
    for _ in range(max_hops):
        picked = _pick_tiered(scheduler, tiers, tried)
        if picked is None:
            break
        attempts += 1
        candidates_tried.append(picked.who)
        await scheduler.wait_for_provider_pacing(picked)
        #   resolve this model's thinking params when deep reasoning is requested,
        #   and its transport quirks ALWAYS (they fix how the host is talked to).
        extra_body = None
        system_in_user = False
        if reasoning_catalog is not None:
            from providers import resolve_quirks, resolve_reasoning_body
            if reasoning or research:
                extra_body = resolve_reasoning_body(
                    reasoning_catalog, logical=logical, who=picked.who,
                    family=picked.model_family) or None
            system_in_user = bool(resolve_quirks(
                reasoning_catalog, logical=logical, who=picked.who,
                family=picked.model_family).get("system_in_user"))
        if research:
            import agent
            res = await agent.research_call(
                client, picked, system_prompt, user_msg=user_msg,
                max_tokens=max_tokens, timeout_s=timeout_s, extra_body=extra_body,
                system_in_user=system_in_user)
        else:
            res = await call_once(client, picked, system_prompt, user_msg=user_msg,
                                  max_tokens=max_tokens, timeout_s=timeout_s,
                                  extra_body=extra_body, system_in_user=system_in_user,
                                  on_progress=on_progress)
        usage = res.get("usage") or {}
        in_tok, out_tok = usage.get("prompt_tokens"), usage.get("completion_tokens")
        trace.append({"slot": picked.who, "code": res["code"],
                      "latency_s": res.get("elapsed_s", 0.0)})
        if res["ok"]:
            scheduler.record_success(picked)
            #   no_store: the output must not reach metrics output-sampling either.
            _emit(metrics, profile, logical, picked, role_label, effort, res,
                  in_tok, out_tok, attempts, candidates_tried,
                  None if no_store else res.get("output"))
            out = {"model": logical, "routed_to": picked.who, "ok": True,
                   "output": res["output"], "elapsed_s": res["elapsed_s"],
                   "attempts": attempts, "candidates_tried": list(candidates_tried),
                   "trace": list(trace)}
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
            #   cache the success for exact repeats - unless the caller said no_store.
            if cache is not None and ckey is not None and not no_store:
                cache.put(ckey, dict(out))
            return out
        #       failure: penalise the slot, record the metric, bounce providers.
        code = res["code"]
        trace[-1]["fallback_reason"] = res.get("error", code)[:160]
        scheduler.record_failure(picked, code, res.get("error", ""))
        _emit(metrics, profile, logical, picked, role_label, effort, res,
              in_tok, out_tok, attempts, candidates_tried, None)
        _budget_guard(scheduler, metrics, profile, picked)
        tried.add(picked.provider.name)
        last = res
        log_event("judge_failover", logical=logical, slot=picked.who, code=code,
                  attempt=attempts, remaining=len(candidates) - len(tried))

    #   strict mode with nothing attempted = every privacy-safe host is cooling and
    #   the unsafe ones were (correctly) excluded - say so, with the explicit code,
    #   rather than the generic "all cooling" error (panel-found fallthrough).
    if privacy == "strict" and attempts == 0 and len(flat) < len(candidates):
        return {"model": logical, "ok": False, "routed_to": None, "attempts": 0,
                "candidates_tried": [], "trace": list(trace), "elapsed_s": 0.0,
                "code": "privacy_blocked",
                "error": (f"privacy-safe hosts for '{logical}' are temporarily "
                          "cooling/blacklisted; non-safe hosts excluded by privacy=strict")}
    err = (last or {}).get("error", "all candidate providers cooling/blacklisted/unconfigured")
    return {"model": logical, "ok": False, "routed_to": None,
            "error": err[:300], "attempts": attempts,
            "candidates_tried": list(candidates_tried), "trace": list(trace),
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
                          research: bool = False, privacy: str = "off",
                          no_store: bool = False) -> list[dict]:
    #   synchronous fan-out: every logical judge in parallel, each with its own
    #   cross-provider failover. returns once all have settled.
    resolved = [(who, *panel.resolve_candidates(who)) for who in who_list]
    rcat = getattr(panel, "reasoning_catalog", None)
    cache = getattr(panel, "cache", None)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[
            route_judge(panel.scheduler, client, logical, candidates, system_prompt,
                        role_label=role_label, effort=effort, profile=profile,
                        metrics=metrics, user_msg=user_msg, max_tokens=max_tokens,
                        timeout_s=timeout_s, nonce=nonce, reasoning=reasoning,
                        research=research, reasoning_catalog=rcat,
                        privacy=privacy, cache=cache, no_store=no_store)
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

    def __init__(self, panel, *, judge_timeout_s: float, store: JobStore | None = None) -> None:
        self.panel = panel
        self.judge_timeout_s = judge_timeout_s
        self.jobs: dict[str, dict] = {}
        self.store = store if store is not None else JobStore()
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self._client: httpx.AsyncClient | None = None
        ready = threading.Event()
        threading.Thread(target=self._run_loop, args=(ready,), daemon=True).start()
        ready.wait(10)
        self._reload_jobs()

    def _run_loop(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self.loop)
        self._client = httpx.AsyncClient()
        self.loop.call_soon(ready.set)
        self.loop.run_forever()

    def _persist(self, job_id: str) -> None:
        #   snapshot the job to durable storage (called after every state transition).
        #   a no_store job is kept in memory only - its content is never written to disk.
        #   deep-copy UNDER the lock: a shallow dict() would share the nested judge
        #   dicts with concurrent mutators while the store serializes them.
        with self.lock:
            job = self.jobs.get(job_id)
            snap = copy.deepcopy(job) if job is not None else None
        if snap is not None and not (snap.get("_exec") or {}).get("no_store"):
            self.store.save(snap)

    def _prune_locked(self) -> None:
        now = time.time()
        stale = [jid for jid, v in self.jobs.items() if now - v["created"] > JOB_TTL_S]
        for jid in stale:
            self.jobs.pop(jid, None)
            self.store.delete(jid)

    def _reload_jobs(self) -> None:
        #   on boot: re-hydrate persisted jobs so a deploy / restart doesn't lose work.
        #   panel jobs reschedule any unfinished judge; run jobs that were mid-flight
        #   are marked interrupted (no safe stage-level resume yet).
        try:
            saved = self.store.load_all()
        except Exception as e:  # noqa: BLE001
            log_event("jobstore_reload_error", error=repr(e)[:200])
            return
        now = time.time()
        resumed = interrupted = 0
        for job in saved:
            jid = job.get("id")
            if not jid or now - job.get("created", now) > JOB_TTL_S:
                if jid:
                    self.store.delete(jid)
                continue
            if job.get("type") == "run":
                if job.get("status") != "complete":
                    job["status"] = "interrupted"
                    job.setdefault("stage_log", []).append(
                        "[runner] interrupted by restart; resubmit to continue")
                    interrupted += 1
                with self.lock:
                    self.jobs[jid] = job
                self.store.save(job)
                continue
            # panel job: reschedule judges not yet settled
            ex = job.get("_exec")
            with self.lock:
                self.jobs[jid] = job
            pending = [who for who, j in (job.get("judges") or {}).items()
                       if j.get("status") not in ("done", "error")]
            if not ex or not pending:
                continue
            for who in pending:
                logical, candidates = self.panel.resolve_candidates(who)
                with self.lock:
                    self.jobs[jid]["judges"][who] = {"model": who, "status": "pending"}
                if not candidates:
                    with self.lock:
                        self.jobs[jid]["judges"][who] = {
                            "model": who, "ok": False, "status": "error",
                            "error": "not registered (no configured provider hosts this model)"}
                    continue
                asyncio.run_coroutine_threadsafe(
                    self._run_judge(jid, who, logical, candidates, ex["system_prompt"],
                                    ex["user_msg"], ex["max_tokens"], ex["role"],
                                    ex["profile"], ex["effort"], ex["timeout_s"],
                                    ex.get("nonce", ""), ex.get("reasoning", False),
                                    ex.get("research", False), ex.get("privacy", "off"),
                                    ex.get("no_store", False)),
                    self.loop)
                resumed += 1
            self.store.save(self.jobs[jid])
        if resumed or interrupted:
            log_event("jobs_reloaded", resumed_judges=resumed, interrupted_runs=interrupted,
                      total=len(saved))

    def submit(self, *, who_list: list[str], system_prompt: str, user_msg: str,
               max_tokens: int, role: str, merge_mode: str, kind: str,
               profile: str = "default", effort: str = "med",
               timeout_s: float | None = None, nonce: str = "",
               reasoning: bool = False, research: bool = False,
               privacy: str = "off", no_store: bool = False) -> dict:
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
        jt = timeout_s or self.judge_timeout_s
        job = {"id": job_id, "type": "panel", "kind": kind, "role": role, "merge": merge_mode,
               "profile": profile, "effort": effort,
               "created": time.time(), "judges": judges, "total": len(who_list),
               #   _exec carries everything needed to RESUME unfinished judges after a
               #   restart (who_list, not slot objects - candidates re-resolve on reload).
               "_exec": {"who_list": list(who_list), "system_prompt": system_prompt,
                         "user_msg": user_msg, "max_tokens": max_tokens, "role": role,
                         "merge_mode": merge_mode, "kind": kind, "profile": profile,
                         "effort": effort, "timeout_s": jt, "nonce": nonce,
                         "reasoning": reasoning, "research": research,
                         "privacy": privacy, "no_store": no_store}}
        #   no_store: keep the job pollable in memory but never persist its content.
        with self.lock:
            self._prune_locked()
            self.jobs[job_id] = job
        if not no_store:
            self.store.save(job)
        #   schedule each logical judge as its own task; each does cross-provider
        #   failover through the shared scheduler + emits per-profile metrics.
        for who, logical, candidates in scheduled:
            asyncio.run_coroutine_threadsafe(
                self._run_judge(job_id, who, logical, candidates, system_prompt,
                                user_msg, max_tokens, role, profile, effort, jt, nonce,
                                reasoning, research, privacy, no_store),
                self.loop,
            )
        log_event("job_submitted", job_id=job_id, req_kind=kind, role=role,
                  profile=profile, effort=effort, research=research, judges=len(who_list),
                  privacy=privacy, no_store=no_store)
        return self.snapshot(job_id)

    async def _run_judge(self, job_id: str, who: str, logical: str, candidates: list,
                         system_prompt: str, user_msg: str, max_tokens: int,
                         role_label: str, profile: str, effort: str,
                         timeout_s: float, nonce: str = "",
                         reasoning: bool = False, research: bool = False,
                         privacy: str = "off", no_store: bool = False) -> None:
        with self.lock:
            j = self.jobs.get(job_id)
            if j is not None:
                j["judges"][who] = {"model": who, "status": "running"}
        self._persist(job_id)

        def _progress(content_chars: int, reasoning_chars: int, tail: str) -> None:
            #   live "watch it think" state: poll GET /api/jobs/<id> while a judge
            #   streams and see its counters + the tail of what it's writing.
            with self.lock:
                jj = self.jobs.get(job_id)
                if jj is not None and jj["judges"].get(who, {}).get("status") == "running":
                    jj["judges"][who]["progress"] = {
                        "content_chars": content_chars,
                        "reasoning_chars": reasoning_chars,
                        "tail": tail,
                        "updated_at": round(time.time(), 1),
                    }

        res = await route_judge(
            self.panel.scheduler, self._client, logical, candidates, system_prompt,
            role_label=role_label, effort=effort, profile=profile,
            metrics=self.panel.metrics, user_msg=user_msg, max_tokens=max_tokens,
            timeout_s=timeout_s, nonce=nonce, reasoning=reasoning, research=research,
            reasoning_catalog=getattr(self.panel, "reasoning_catalog", None),
            privacy=privacy, cache=getattr(self.panel, "cache", None), no_store=no_store,
            on_progress=_progress)
        res["model"] = who
        res["status"] = "done" if res.get("ok") else "error"
        with self.lock:
            j = self.jobs.get(job_id)
            if j is not None:
                j["judges"][who] = res
        self._persist(job_id)
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
        self.store.save(job)
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
        self._persist(job_id)
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

    def snapshot(self, job_id: str, *, trace: bool = False) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job.get("type") == "run":
                return self._run_snapshot_locked(job)
            raw = [dict(v) for v in job["judges"].values()]
            kind, role, merge_mode = job["kind"], job["role"], job["merge"]
            created = job["created"]

        #   the per-hop routing chain is verbose; only surface it on ?trace=true.
        if not trace:
            for j in raw:
                j.pop("trace", None)
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
