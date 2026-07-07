from __future__ import annotations
import asyncio
import os
import time

import httpx

import web_tools
from content.roles import looks_like_refusal
from oplog import log_event
from scheduler import ProviderError, call_slot

# Provider-agnostic research agent. Two backends behind one entry point:
#
#   1. research_call_strands — a real Strands Agent with native web_search /
#      web_fetch @tools, stacked context management (context_manager="auto" →
#      SummarizingConversationManager with proactive compression + a
#      ContextOffloader), and stream_async. This is the full-capability path:
#      the SDK owns the ReAct loop, tool dispatch, conversation management, and
#      context window safety — no hand-rolled ACTION/OBSERVATION parsing.
#
#   2. _research_call_httpx — the legacy hand-rolled ReAct loop over httpx
#      (kept as the fallback for providers Strands/LiteLLM can't drive, and for
#      environments where strands-agents isn't installed).
#
# research_call() dispatches: Strands first when available + the provider is
# OpenAI-compatible (the /chat/completions URL signal), else the httpx loop.
# Both produce the same result shape (ok/code/usage/elapsed_s/steps/searches/
# tool_calls/output?/error?) so jobs.call_once is unchanged.

RESEARCH_MAX_STEPS = int(os.environ.get("RESEARCH_MAX_STEPS", "8"))
# Flip to "0" to force the legacy httpx loop (escape hatch / A-B testing).
RESEARCH_USE_STRANDS = os.environ.get("RESEARCH_USE_STRANDS", "1").strip() in ("1", "true", "yes")


async def research_call(client: httpx.AsyncClient, picked, system_prompt: str, *,
                        user_msg: str, max_tokens: int, timeout_s: float,
                        extra_body: dict | None = None, max_steps: int | None = None,
                        system_in_user: bool = False) -> dict:
    max_steps = RESEARCH_MAX_STEPS if max_steps is None else max_steps
    # Try the Strands path first (full-capability: native tools, context mgmt,
    # stream_async). Returns None to signal "fall back to httpx".
    if RESEARCH_USE_STRANDS:
        try:
            res = await research_call_strands(
                client, picked, system_prompt, user_msg=user_msg,
                max_tokens=max_tokens, timeout_s=timeout_s, max_steps=max_steps)
            if res is not None:
                return res
        except Exception as e:  # noqa: BLE001 — never let the Strands path kill research
            log_event("research_strands_error", slot=getattr(picked, "who", ""),
                      error=f"{type(e).__name__}: {str(e)[:200]}")
    return await _research_call_httpx(
        client, picked, system_prompt, user_msg=user_msg, max_tokens=max_tokens,
        timeout_s=timeout_s, extra_body=extra_body, max_steps=max_steps,
        system_in_user=system_in_user)


async def research_call_strands(client: httpx.AsyncClient, picked, system_prompt: str,
                                *, user_msg: str, max_tokens: int, timeout_s: float,
                                max_steps: int) -> dict | None:
    """Full-capability Strands research path. Builds a real Agent with
    web_search/web_fetch @tools + context_manager="auto", drives it via
    invoke_async (with a turn limit), and returns the result dict — or None to
    signal the caller to fall back to the httpx loop.

    Returns None (not an error dict) when Strands isn't installed or the
    provider isn't OpenAI-compatible, so research_call() can fall back cleanly.
    """
    t0 = time.monotonic()
    try:
        from strands import Agent
        from strands.models.litellm import LiteLLMModel
        from strands.types.agent import Limits
    except Exception:
        return None  # strands not installed → fall back

    p = picked.provider
    base_url = getattr(p, "url", "") or ""
    # Only drive OpenAI-compatible /chat/completions endpoints via LiteLLM's
    # openai/ prefix. Non-OpenAI providers fall back to the httpx loop.
    if not base_url or "/chat/completions" not in base_url:
        return None
    base_url = base_url[: base_url.rfind("/chat/completions")]
    api_key = getattr(p, "api_key", "") or ""
    if not api_key:
        return None

    model_id = picked.model
    # LiteLLM needs a provider prefix; openai/ works for any OpenAI-compatible
    # endpoint when api_base is set.
    if "/" not in model_id:
        model_id = f"openai/{model_id}"
    elif not model_id.startswith(("openai/", "groq/", "anthropic/", "bedrock/")):
        model_id = f"openai/{model_id}"

    try:
        llm = LiteLLMModel(client_args={"api_key": api_key, "api_base": base_url},
                           model_id=model_id)
    except Exception:
        return None

    # reuse the StrandsAdapter's web @tool builders so the research agent and
    # the agent-tab agent share the exact same SSRF-guarded web tools.
    try:
        from agent_sessions import _build_web_strands_tools
        tools = _build_web_strands_tools(client)
    except Exception:
        tools = []

    searches = fetches = 0
    reasoning_all: list[str] = []
    output_parts: list[str] = []

    def _cb(**kw):
        nonlocal searches, fetches
        # tool-use start events carry the tool name in event.contentBlockStart.start.toolUse
        ev = kw.get("event") or {}
        tu = (ev.get("contentBlockStart") or {}).get("start", {}).get("toolUse")
        if isinstance(tu, dict):
            nm = tu.get("name")
            if nm == "web_search":
                searches += 1
            elif nm == "web_fetch":
                fetches += 1
        if kw.get("reasoningText"):
            reasoning_all.append(str(kw["reasoningText"]))
        if kw.get("data"):
            output_parts.append(str(kw["data"]))

    agent = Agent(
        model=llm, tools=tools, system_prompt=system_prompt,
        # "auto" = SummarizingConversationManager with proactive compression +
        # a ContextOffloader — keeps long research loops inside the context
        # window without the silent ContextWindowOverflowError drops the legacy
        # loop was vulnerable to.
        context_manager="auto",
        callback_handler=_cb,
        load_tools_from_directory=False,
    )

    try:
        remaining = max(15.0, timeout_s - (time.monotonic() - t0))
        result = await asyncio.wait_for(
            agent.invoke_async(user_msg, limits=Limits(max_turns=max_steps + 1)),
            timeout=remaining)
    except asyncio.TimeoutError:
        return _result(False, "timeout", error=f"strands research timed out after {timeout_s}s",
                       steps=searches + fetches, searches=searches,
                       tool_calls=searches + fetches, t0=t0, in_tok=0, out_tok=0,
                       reasoning=reasoning_all)
    except Exception as e:  # noqa: BLE001
        # Provider/network errors here → return None so research_call falls
        # back to the httpx loop (which has richer per-provider error handling).
        log_event("research_strands_fallback", slot=getattr(picked, "who", ""),
                  error=f"{type(e).__name__}: {str(e)[:200]}")
        return None

    # Extract the final assistant text from the result message.
    text = ""
    msg = getattr(result, "message", None) or {}
    for block in (msg.get("content") or []):
        if isinstance(block, dict) and block.get("text"):
            text = block["text"]
    if not text:
        text = "".join(output_parts).strip()

    # usage from the event-loop metrics
    in_tok = out_tok = 0
    metrics = getattr(result, "metrics", None)
    if metrics is not None:
        usage = getattr(metrics, "accumulated_usage", {}) or {}
        in_tok = int(usage.get("inputTokens") or 0)
        out_tok = int(usage.get("outputTokens") or 0)

    if not text:
        return _result(False, "empty", error="no final answer",
                       steps=searches + fetches, searches=searches,
                       tool_calls=searches + fetches, t0=t0, in_tok=in_tok,
                       out_tok=out_tok, reasoning=reasoning_all)
    if looks_like_refusal(text):
        return _result(False, "refusal", error=f"refusal: {text[:160]!r}",
                       steps=searches + fetches, searches=searches,
                       tool_calls=searches + fetches, t0=t0, in_tok=in_tok,
                       out_tok=out_tok, reasoning=reasoning_all)
    log_event("research_strands_ok", slot=getattr(picked, "who", ""),
              searches=searches, fetches=fetches, out_chars=len(text))
    return _result(True, "ok", output=text, steps=searches + fetches,
                   searches=searches, tool_calls=searches + fetches, t0=t0,
                   in_tok=in_tok, out_tok=out_tok, reasoning=reasoning_all)


async def _research_call_httpx(client: httpx.AsyncClient, picked, system_prompt: str, *,
                               user_msg: str, max_tokens: int, timeout_s: float,
                               extra_body: dict | None = None, max_steps: int | None = None,
                               system_in_user: bool = False) -> dict:
    """Legacy hand-rolled ReAct loop over httpx — the fallback when Strands
    isn't available or the provider isn't OpenAI-compatible. The model calls
    web_tools by emitting one ACTION line; we execute it, feed back an
    OBSERVATION, and loop until it writes a final answer (no ACTION) or hits the
    step/budget cap. Never raises — mirrors jobs.call_once's result shape."""
    max_steps = RESEARCH_MAX_STEPS if max_steps is None else max_steps
    t0 = time.monotonic()
    prompt_full = system_prompt + web_tools.TOOLS_PROTOCOL
    if system_in_user:
        #   hosts that strip custom system prompts (e.g. GitHub Phi-4) get the whole
        #   protocol folded into the first user message instead.
        messages = [{"role": "user", "content": f"{prompt_full}\n\n---\n\n{user_msg}"}]
    else:
        messages = [
            {"role": "system", "content": prompt_full},
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
