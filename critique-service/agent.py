from __future__ import annotations
import os
import time

import httpx

import web_tools
from content.roles import looks_like_refusal
from oplog import log_event
from scheduler import ProviderError, call_slot

# Provider-agnostic research agent: a ReAct think -> act -> observe loop around a
# single model. The model calls web_tools by emitting one ACTION line; we execute
# it, feed back an OBSERVATION, and loop until it writes a final answer (no ACTION)
# or hits the step/budget cap. Reasoning params ride in extra_body; the streamed
# thinking trace is surfaced via usage["reasoning_content"]. Never raises - mirrors
# jobs.call_once's result shape (+ steps/searches/tool_calls).

RESEARCH_MAX_STEPS = int(os.environ.get("RESEARCH_MAX_STEPS", "8"))


async def research_call(client: httpx.AsyncClient, picked, system_prompt: str, *,
                        user_msg: str, max_tokens: int, timeout_s: float,
                        extra_body: dict | None = None, max_steps: int | None = None) -> dict:
    max_steps = RESEARCH_MAX_STEPS if max_steps is None else max_steps
    t0 = time.monotonic()
    messages = [
        {"role": "system", "content": system_prompt + web_tools.TOOLS_PROTOCOL},
        {"role": "user", "content": user_msg},
    ]
    steps = searches = fetches = 0
    reasoning_all: list[str] = []
    in_tok = out_tok = 0
    deadline = t0 + timeout_s

    try:
        for step in range(max_steps + 1):
            final_turn = step == max_steps
            msgs = messages
            if final_turn:
                msgs = messages + [{"role": "user",
                                    "content": "Tool budget reached. Write your FINAL answer now, no ACTION lines."}]
            remaining = max(15.0, deadline - time.monotonic())
            content, usage = await call_slot(client, picked, messages=msgs,
                                             max_tokens=max_tokens, timeout_s=remaining,
                                             extra_body=extra_body)
            in_tok += int(usage.get("prompt_tokens") or 0)
            out_tok += int(usage.get("completion_tokens") or 0)
            if usage.get("reasoning_content"):
                reasoning_all.append(usage["reasoning_content"])

            action = None if final_turn else web_tools.parse_action(content)
            if action is None:
                text = (content or "").strip()
                if looks_like_refusal(text):
                    return _result(False, "refusal", error=f"refusal: {text[:160]!r}",
                                   steps=steps, searches=searches, tool_calls=searches + fetches,
                                   t0=t0, in_tok=in_tok, out_tok=out_tok, reasoning=reasoning_all)
                return _result(True, "ok", output=text, steps=steps, searches=searches,
                               tool_calls=searches + fetches, t0=t0, in_tok=in_tok,
                               out_tok=out_tok, reasoning=reasoning_all)

            tool, args = action
            steps += 1
            if tool == "web_search":
                searches += 1
                obs = await web_tools.web_search(str(args.get("query", "")), http_client=client)
            else:
                fetches += 1
                obs = await web_tools.web_fetch(str(args.get("url", "")), http_client=client)
            log_event("research_step", slot=picked.who, step=steps, tool=tool,
                      arg=str(args)[:120], obs_chars=len(obs))
            messages = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"OBSERVATION:\n{obs}"},
            ]
        return _result(False, "empty", error="no final answer", steps=steps, searches=searches,
                       tool_calls=searches + fetches, t0=t0, in_tok=in_tok, out_tok=out_tok,
                       reasoning=reasoning_all)
    except ProviderError as e:
        msg = str(e)
        return _result(False, msg.split(":", 1)[0], error=msg[:300], steps=steps,
                       searches=searches, tool_calls=searches + fetches, t0=t0,
                       in_tok=in_tok, out_tok=out_tok, reasoning=reasoning_all)
    except Exception as e:  # noqa: BLE001
        return _result(False, "exc", error=f"{type(e).__name__}: {str(e)[:180]}", steps=steps,
                       searches=searches, tool_calls=searches + fetches, t0=t0,
                       in_tok=in_tok, out_tok=out_tok, reasoning=reasoning_all)


def _result(ok, code, *, output=None, error=None, steps, searches, tool_calls, t0,
            in_tok, out_tok, reasoning) -> dict:
    usage = {"prompt_tokens": in_tok, "completion_tokens": out_tok}
    if reasoning:
        usage["reasoning_content"] = "\n\n".join(reasoning)
    res = {"ok": ok, "code": code, "usage": usage,
           "elapsed_s": round(time.monotonic() - t0, 1),
           "steps": steps, "searches": searches, "tool_calls": tool_calls}
    if output is not None:
        res["output"] = output
    if error is not None:
        res["error"] = error
    return res
