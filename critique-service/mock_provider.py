from __future__ import annotations
import hashlib
import os
import re
import threading
import time

from oplog import log_event

# Quota-free mock provider. When MOCK_MODE=1, scheduler.call_slot routes here
# instead of hitting a real endpoint, so the whole rotation / failover / metrics
# / effort machinery can be exercised end-to-end with ZERO real API calls.
#
# Two failure sources, both deterministic:
#   1. MOCK_FAULTS env  - explicit rules "selector=code", comma-separated. The
#      selector matches either a full slot "provider/model" or a bare "provider".
#      e.g. MOCK_FAULTS="nvidia/z-ai/glm-5.1=429,google=5xx"
#   2. MockRateLimiter  - simulates each provider's per-process request budget
#      (a fraction of its catalog rpd, or MOCK_RPD_<PROVIDER>) and raises 429 once
#      a provider is "exhausted", so budget-exhaustion + cooldown are observable
#      in /api/stats without waiting on a real clock.

ENABLED = os.environ.get("MOCK_MODE", "0").strip() in ("1", "true", "yes")
_LATENCY = float(os.environ.get("MOCK_LATENCY_S", "0"))


def _parse_faults() -> dict[str, str]:
    rules: dict[str, str] = {}
    for part in os.environ.get("MOCK_FAULTS", "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        sel, code = part.split("=", 1)
        rules[sel.strip()] = code.strip()
    return rules


class MockRateLimiter:
    """per-provider synthetic request budget, exhausting into 429s."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}

    def _budget(self, prov) -> int:
        override = os.environ.get(f"MOCK_RPD_{prov.name.upper().replace('-', '_')}")
        if override:
            try:
                return int(override)
            except ValueError:
                pass
        #       default synthetic budget: small so sims exhaust quickly. derive a
        #       cheap, provider-distinct number from the catalog rpd/rpm.
        if prov.rpd:
            return max(2, min(8, prov.rpd // 1000 or 3))
        return max(2, prov.rpm // 10)

    def check(self, picked) -> None:
        with self._lock:
            n = self._calls.get(picked.provider.name, 0) + 1
            self._calls[picked.provider.name] = n
            budget = self._budget(picked.provider)
        if n > budget:
            raise _err("429", f"mock budget exhausted for {picked.provider.name} "
                              f"({n}>{budget})")


_LIMITER = MockRateLimiter()
_FAULTS = _parse_faults()


def reset(faults: dict[str, str] | None = None) -> None:
    #   test helper: clear the synthetic request budget and set/refresh fault rules.
    global _FAULTS
    with _LIMITER._lock:
        _LIMITER._calls.clear()
    _FAULTS = dict(faults) if faults is not None else _parse_faults()


def _err(code: str, reason: str):
    from scheduler import ProviderError
    return ProviderError(f"{code}:{reason}")


def _match_fault(picked) -> str | None:
    if picked.who in _FAULTS:
        return _FAULTS[picked.who]
    if picked.provider.name in _FAULTS:
        return _FAULTS[picked.provider.name]
    return None


async def mock_call_slot(client, picked, messages, *, max_tokens: int,
                         timeout_s: float = 600.0, response_format=None):
    #   drop-in for scheduler.call_slot. returns (content, usage) or raises
    #   ProviderError with the exact same code strings the real path uses.
    import asyncio
    if _LATENCY > 0:
        await asyncio.sleep(_LATENCY)

    fault = _match_fault(picked)
    if fault:
        log_event("mock_fault", slot=picked.who, code=fault)
        if fault == "empty":
            return ("", {"prompt_tokens": 0, "completion_tokens": 0})
        raise _err(fault, f"mock fault for {picked.who}")

    _LIMITER.check(picked)

    system = messages[0]["content"] if messages else ""
    prompt = messages[-1]["content"] if messages else ""
    digest = hashlib.sha1(f"{picked.who}:{prompt}".encode()).hexdigest()[:8]
    #   research ReAct protocol: if tools are offered and we haven't observed yet,
    #   emit one search action; after an OBSERVATION, write the final answer.
    if "## Research tools" in system:
        #   only look at non-system turns (the protocol text in `system` mentions
        #   "OBSERVATION:", which must not be mistaken for a real tool observation).
        joined = " ".join(m.get("content", "") for m in messages if m.get("role") != "system")
        if "OBSERVATION:" not in joined and "FINAL answer now" not in joined:
            content = f'ACTION: web_search {{"query": "mock query {digest}"}}'
        else:
            content = (f"Final answer ({digest}): based on the search, here is the synthesized "
                       f"result. Source: [Mock](https://example.com/a).\n\n- key finding one")
        out_tokens = min(max_tokens, max(8, len(content) // 4))
        return (content, {"prompt_tokens": max(1, len(system) // 4), "completion_tokens": out_tokens,
                          "reasoning_content": f"(mock thinking {digest})"})
    content = _mock_content(picked, system, digest)
    in_tokens = max(1, len(system) // 4)
    out_tokens = min(max_tokens, max(8, len(content) // 4))
    usage = {"prompt_tokens": in_tokens, "completion_tokens": out_tokens}
    return (content, usage)


_FILE_MARKER_RE = re.compile(r"BEGIN FILE \[([0-9a-fA-F]+)\] path=")


def _mock_content(picked, system: str, digest: str) -> str:
    #   role/format-aware synthetic output so the multi-stage orchestrator + judge
    #   can run fully offline:
    #     - file mode (system carries the BEGIN FILE protocol) -> emit marker files
    #     - JSON mode (verifier/extractor/planner skeletons) -> emit a small object
    #     - otherwise -> prose that includes a markdown bullet (reviewer needs one)
    m = _FILE_MARKER_RE.search(system)
    if m:
        nonce = m.group(1)
        return (
            f"Here are the files ({digest}).\n\n"
            f"===== BEGIN FILE [{nonce}] path=README.md =====\n"
            f"# Mock artifact {digest}\n\nGenerated by {picked.who} for offline testing.\n"
            f"===== END FILE [{nonce}] path=README.md =====\n\n"
            f"===== BEGIN FILE [{nonce}] path=src/app.py =====\n"
            f"def main():\n    print('mock {digest}')\n"
            f"===== END FILE [{nonce}] path=src/app.py =====\n"
        )
    low = system.lower()
    if "json" in low and ("task_type" in low or "schematic" in low or "stages" in low):
        return ('{"task_type": "freeform", "task": "mock planned task", '
                '"stages": [{"name": "write", "role": "generator", '
                '"instructions": "Produce what the prompt asks for.", '
                '"inputs": ["prompt"], "max_tokens": 1000}], '
                '"output_rules": {"format": "markdown"}, '
                '"judge_config": {"rules": [], "plugins": [], "llm_judges": []}, '
                '"max_rounds": 1}')
    if "json" in low and ('"pass"' in low or "verifier" in low or "pass:" in low):
        return '{"pass": true, "reason": "mock verifier approves"}'
    if "json" in low and ("extract" in low or "topics" in low):
        return '{"topics": [{"name": "Mock topic", "scope": "x", "target_words": 700}]}'
    return (
        f"[mock:{picked.who}] synthetic output {digest}. Deterministic placeholder "
        f"text for offline testing, long enough to read as real prose.\n\n"
        f"- key point one ({digest})\n- key point two\n"
    )
