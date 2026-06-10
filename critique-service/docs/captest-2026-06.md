# Max-capacity panel self-review — standing benchmark (2026-06)

The panel reviews **its own `scheduler.py`** at `effort=max`, `reasoning=true`,
async, profile `captest`. Self-review is the ideal capacity test: every claim is
verifiable against the code, so real finds prove capability and hallucinations
expose weak models — while the run itself exercises failover, streaming, pacing,
and metrics at full load. **Re-run this whenever the roster changes.**

How to run: POST `/api/panel` with `{"input": <scheduler.py source>, "system":
<reviewer prompt>, "panel": [<all frontier logicals>], "effort": "max",
"reasoning": true, "profile": "captest", "merge": "none", "async": true}` and
poll `/api/jobs/<id>`; cross-check every claimed defect against the code before
believing it.

## Round 1 results (job a46c0a490e6cef8d)

| Judge | Routed | Time | Verdict |
|---|---|---|---|
| glm-5.1 | nvidia | 22 min | **Deepest**: 28k thinking tokens, 2 unique majors (budget-cooldown erase; 4xx→"http" misclass), 0 hallucinations. Async-only. |
| nemotron-ultra | openrouter :free | 8 min | **Best density**: 4 real finds (rpm=0 crash; string upstream codes; SSE multi-line; backwards comment), 1 false claim ("refusal" dead code — it isn't). |
| gemma-4-26b | cloudflare | 3.7 min | **Best value**: 1 real bug (rpm=0), correct fix, 922 chars. Burned 24k thinking tokens (enable_thinking works on CF). |
| gpt-oss-120b | cloudflare | 44 s | Fast, mixed: 1 real find among confident false positives (called unused `earliest_wake_ts` "critical"). |
| kimi-k2.6 | cloudflare | 3 min | Genuine insight in-trace but **cut mid-thought** → exposed CF needs `max_completion_tokens` (fixed in catalog). |
| phi-4-reasoning | github | 1 min | Derailed — `in_tokens=233`: GitHub strips custom system prompts for it; never saw the code. → `system_in_user` quirk added. |
| deepseek-r1-distill | cloudflare (failover from 403'd GitHub R1) | 23 s | ⚠️ Proposed `await asyncio.sleep` **while holding the threading.Lock** — a deadlock. **REMOVED from roster.** |
| qwen3-coder-480b | nvidia | 5 min | `empty:` — no output. Retest pending; twice-empty ⇒ remove. |
| deepseek-v4-pro | nvidia | killed at 35+ min | Still thinking at `reasoning_effort:max` when a redeploy restarted the Space. Default lowered to `high`; live progress field added to watch it. |

## Verified bugs fixed from this round (commit eaa6287)
- `record_success` erased BUDGET cooldowns (GLM-5.1) — now clears error cooldowns only.
- Unknown 4xx (402…) misclassified as network-class "http" and retried forever (GLM-5.1).
- `rpm=0` crash in 429 cooldown (gemma-4 + nemotron + gpt-oss, independently).
- Upstream STRING codes (`rate_limit_exceeded`…) bypassed 429/401 routing (nemotron).
- Non-streamed responses dropped `message.reasoning_content` (kimi).
- Two backwards comments (nemotron + GLM).

## Operational lessons
- **Deploys kill running jobs** — the JobRunner is in-memory; pushing to `c`
  restarts the Space. Never deploy while a long judge is mid-think.
- CF deprecates `max_tokens` → newer CF slots get `max_completion_tokens` via
  reasoning_catalog or output truncates mid-trace.
- GitHub Models strips/overrides custom system prompts for Phi-4-reasoning →
  `quirks.system_in_user` folds system into the user message.
- GLM's 22-min run only survived because JUDGE_TIMEOUT_S was raised to 1800.
- Don't trust unverified model fixes: r1-distill's "critical fix" was a deadlock.

## Round 2 (roster-tuning retest) — results appended below when run
