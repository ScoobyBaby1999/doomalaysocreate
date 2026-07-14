from __future__ import annotations
import json
import re


def judge_text(j: dict) -> str:
    #   a judge's produced text lives under "output" (generic /api/panel) or
    #   "critique" (legacy /api/critique). tolerate both.
    return (j.get("output") or j.get("critique") or "") if j.get("ok") else ""

# deterministic merge of the judge panel's critiques into one consolidated list.
#
# why deterministic (not an extra LLM pass): one judge being down must never break
# the merge, and the user's stated priority is panel *diversity* + resilience, not
# prose polish. a code merge has no provider to 429. it also surfaces *consensus* -
# when multiple judges independently raise the same issue, that's the strongest
# signal in the whole response, so we keep a count and sort by it.

_BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$")
_WS_RE = re.compile(r"\s+")
_NONWORD_RE = re.compile(r"[^a-z0-9\s]")
_STOP = {
    "the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "is", "are",
    "this", "that", "it", "its", "with", "without", "no", "not", "be", "as",
    "section", "line", "stage", "use", "used", "add", "remove", "fix", "should",
}


def _extract_bullets(text: str) -> list[str]:
    #   pull bullet lines out of one judge's critique. continuation lines (indented,
    #   no marker) are folded into the preceding bullet so multi-line points survive.
    bullets: list[str] = []
    for raw in text.splitlines():
        m = _BULLET_RE.match(raw)
        if m:
            item = m.group(1).strip()
            if item:
                bullets.append(item)
        elif bullets and raw.strip() and (raw.startswith((" ", "\t"))):
            bullets[-1] = f"{bullets[-1]} {raw.strip()}"
    return bullets


def _tokens(bullet: str) -> set[str]:
    norm = _NONWORD_RE.sub(" ", bullet.lower())
    return {t for t in _WS_RE.sub(" ", norm).split() if t and t not in _STOP}


def _similar(a: set[str], b: set[str], threshold: float = 0.5) -> bool:
    #   jaccard over content tokens - collapses paraphrases of the same issue.
    if not a or not b:
        return False
    inter = len(a & b)
    if inter == 0:
        return False
    return inter / len(a | b) >= threshold


def merge_critiques(judges: list[dict]) -> str:
    #   judges: the per-judge result dicts; only ok==True / 'critique' present ones
    #   contribute. returns a single consolidated bullet list as markdown.
    groups: list[dict] = []   # {"text": str, "tokens": set, "judges": set[str]}

    for j in judges:
        text = judge_text(j)
        if not text:
            continue
        model = j.get("model", "?")
        for bullet in _extract_bullets(text):
            toks = _tokens(bullet)
            placed = False
            for g in groups:
                if _similar(toks, g["tokens"]):
                    g["judges"].add(model)
                    #       keep the most detailed phrasing as the representative.
                    if len(bullet) > len(g["text"]):
                        g["text"] = bullet
                        g["tokens"] = toks
                    placed = True
                    break
            if not placed:
                groups.append({"text": bullet, "tokens": toks, "judges": {model}})

    if not groups:
        return ""

    #   consensus first (most judges agreeing), then longer/more-specific bullets.
    groups.sort(key=lambda g: (-len(g["judges"]), -len(g["text"])))

    lines: list[str] = []
    for g in groups:
        n = len(g["judges"])
        tag = f" _(flagged by {n} judges)_" if n > 1 else ""
        lines.append(f"- {g['text']}{tag}")
    return "\n".join(lines)


_PASS_RE = re.compile(r'pass["\']?\s*[:=]\s*(true|false|yes|no)', re.IGNORECASE)


def _verdict(text: str) -> tuple[bool | None, str]:
    #   parse a verifier judge's {"pass": bool, "reason": str}. tolerant of fences
    #   and of small models that answer in prose ("pass: yes because ...").
    t = text.strip()
    if t.startswith("```"):
        nl, last = t.find("\n"), t.rfind("```")
        if nl != -1 and last > nl:
            t = t[nl + 1:last].strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict) and "pass" in obj:
            return bool(obj["pass"]), str(obj.get("reason", ""))[:300]
    except (ValueError, TypeError):
        pass
    m = _PASS_RE.search(t)
    if m:
        return m.group(1).lower() in ("true", "yes"), t[:300]
    return None, t[:300]


def merge_votes(judges: list[dict]) -> str:
    #   tally verifier verdicts into a consensus PASS/FAIL with per-judge reasons.
    passes = fails = 0
    rows: list[str] = []
    for j in judges:
        text = judge_text(j)
        if not text:
            continue
        verdict, reason = _verdict(text)
        if verdict is True:
            passes += 1
            mark = "PASS"
        elif verdict is False:
            fails += 1
            mark = "FAIL"
        else:
            mark = "?"
        rows.append(f"- **{j.get('model', '?')}**: {mark} — {reason}")
    total = passes + fails
    if total == 0:
        return ""
    overall = "PASS" if passes > fails else ("FAIL" if fails > passes else "SPLIT")
    head = f"VERDICT: {overall} ({passes}/{total} judges voted pass)"
    return head + "\n" + "\n".join(rows)


def merge_concat(judges: list[dict]) -> str:
    #   label each judge's full output and stack them. used for generate/transform
    #   style tasks where you want to see and compare all N answers.
    parts = []
    for j in judges:
        text = judge_text(j)
        if text:
            parts.append(f"### {j.get('model', '?')}\n\n{text}")
    return "\n\n---\n\n".join(parts)


def merge_panel(judges: list[dict], mode: str) -> str:
    #   dispatch to the requested merge strategy.
    if mode == "dedupe":
        return merge_critiques(judges)
    if mode == "vote":
        return merge_votes(judges)
    if mode == "concat":
        return merge_concat(judges)
    return ""  # "none"
