"""
hypothesis/smells.py
====================
Deterministic requirements smell detector — the second deterministic gate
(after ears.py). Scans text and returns ``list[Smell]`` in document order.

No model calls. No I/O. No NLP dependency (stdlib ``re`` only). Pure functions.

Public API (see __all__):
  detect(text, source)            -> list[Smell]
  scan_requirement(req, source)   -> RequirementCandidate  (merges, returns new instance)

Detection tiers (see project_decisions.md [0015]):
  Tier 1 — lexical (dictionary):   VAGUE_TERM, SUBJECTIVE_LANGUAGE,
                                    UNIVERSAL_QUANTIFIER, LOOPHOLE
  Tier 2 — syntactic (regex):      PASSIVE_VOICE, MISSING_ACTOR
  Tier 3 — heuristic (compound):   AMBIGUOUS_PRONOUN, UNVERIFIABLE
  Tier 4 — deferred to LLM:        MISSING_CONDITION (no detector here)

The deterministic tiers run before any model call; Tier-3 findings are lower
confidence and their messages say so. Accepted false-positive rate is filtered
by the downstream LLM critique loop before a human sees it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from hypothesis.models import (
    RequirementCandidate,
    Smell,
    SmellCategory,
    SmellSource,
    SmellType,
)

__all__ = ["detect", "scan_requirement"]


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Match:
    """A single detector hit. ``start`` is the character offset, used for sorting
    and deduplication only — it is NOT stored on the public Smell."""
    span: str
    start: int
    message: str
    suggestion: str | None = None


@dataclass(frozen=True)
class _LexEntry:
    """One dictionary entry for a Tier-1 lexical detector."""
    pattern: str
    message: str
    suggestion: str | None = None
    whole_word: bool = True   # wrap in (?<!\w)..(?!\w) lookaround


@dataclass(frozen=True)
class _SmellRule:
    """Binds a smell type/category to its detector function. The registry
    (`_RULES`) is a list of these — adding a smell type = append one entry."""
    smell_type: SmellType
    category: SmellCategory
    fn: Callable[[str], list[_Match]]


# ---------------------------------------------------------------------------
# Tier-1: lexical detector factory
# ---------------------------------------------------------------------------

def _build_lexicon_fn(entries: list[_LexEntry]) -> Callable[[str], list[_Match]]:
    """Compile each entry to a regex once, return a detector closure.

    Word boundary: single words use ``(?<!\\w)..(?!\\w)`` lookaround rather than
    ``\\b`` (which misfires on hyphenated compounds like ``user-friendly``).
    Phrases (whole_word=False) match the escaped phrase directly.
    """
    compiled: list[tuple[re.Pattern[str], str, str | None]] = []
    for entry in entries:
        if entry.whole_word:
            pat = re.compile(r"(?<!\w)" + re.escape(entry.pattern) + r"(?!\w)", re.IGNORECASE)
        else:
            pat = re.compile(re.escape(entry.pattern), re.IGNORECASE)
        compiled.append((pat, entry.message, entry.suggestion))

    def _detect(text: str) -> list[_Match]:
        results: list[_Match] = []
        for pat, msg, sug in compiled:
            for m in pat.finditer(text):
                results.append(_Match(span=m.group(), start=m.start(), message=msg, suggestion=sug))
        return results

    return _detect


def _entries(
    words: list[str],
    message: str,
    suggestion: str | None,
    whole_word: bool = True,
) -> list[_LexEntry]:
    """Build a homogeneous group of lexicon entries sharing message/suggestion."""
    return [_LexEntry(pattern=w, message=message, suggestion=suggestion, whole_word=whole_word) for w in words]


# ---------------------------------------------------------------------------
# Lexicons (grounded in Femmer/Smella, ISO 29148, INCOSE GtWR, Cruz et al.)
# ---------------------------------------------------------------------------

_VAGUE_ENTRIES: list[_LexEntry] = (
    _entries(
        ["approximately", "about", "around", "roughly", "nearly",
         "generally", "typically", "usually", "normally"],
        "Approximation word — lacks precision.",
        "State an exact value or tolerance (e.g. '200 ms ± 10').",
    )
    + _entries(
        ["many", "several", "some", "various", "numerous", "few"],
        "Undefined quantity — how many?",
        "Give an exact count or range.",
    )
    + _entries(
        ["a few", "a number of", "a lot of"],
        "Undefined quantity — how many?",
        "Give an exact count or range.",
        whole_word=False,
    )
    + _entries(
        ["etc.", "and so on", "and so forth", "et al.",
         "such as", "for example", "e.g.", "i.e."],
        "Incomplete enumeration — the list is not exhaustive.",
        "Enumerate every case explicitly.",
        whole_word=False,
    )
    + _entries(
        ["sufficient", "adequate", "reasonable", "appropriate",
         "suitable", "relevant", "necessary", "certain"],
        "Undefined threshold — by whose measure?",
        "Define the measurable criterion.",
    )
    + _entries(
        ["TBD", "TBS", "placeholder"],
        "Placeholder — unfinished requirement.",
        "Replace with the real requirement.",
    )
    + _entries(
        ["to be determined", "to be supplied", "to be defined"],
        "Placeholder — unfinished requirement.",
        "Replace with the real requirement.",
        whole_word=False,
    )
    + _entries(
        ["often", "sometimes", "occasionally", "periodically", "regularly"],
        "Uncertain frequency — no defined rate.",
        "State the exact rate or schedule.",
    )
    + _entries(
        ["soon", "immediately", "quickly", "shortly"],
        "Missing temporal precision — no deadline.",
        "State an exact deadline (e.g. 'within 500 ms').",
    )
)

_SUBJECTIVE_ENTRIES: list[_LexEntry] = (
    _entries(
        ["intuitive", "easy", "simple", "seamless", "elegant",
         "clean", "smart", "modern", "beautiful"],
        "Subjective quality adjective — undefined.",
        "Define measurable acceptance criteria.",
    )
    + _entries(
        ["user-friendly"],
        "Subjective quality adjective — undefined.",
        "Define measurable usability criteria (e.g. task-completion time).",
        whole_word=False,
    )
    + _entries(
        ["fast", "quick", "slow", "responsive", "lightweight"],
        "Subjective performance term — no metric.",
        "State a measurable latency/throughput target.",
    )
    + _entries(
        ["real-time", "low-latency"],
        "Subjective performance term — no metric.",
        "State a measurable latency target.",
        whole_word=False,
    )
    + _entries(
        ["robust", "graceful", "reliable", "resilient", "stable"],
        "Subjective robustness term — undefined threshold.",
        "Subjective unless paired with a measurable SLA (e.g. 99.9% uptime).",
    )
    + _entries(
        ["fault-tolerant"],
        "Subjective robustness term — undefined threshold.",
        "Pair with a measurable SLA.",
        whole_word=False,
    )
    + _entries(
        ["flexible", "versatile", "powerful", "advanced",
         "comprehensive", "extensible", "scalable"],
        "Subjective capability term — undefined scope.",
        "Specify the exact capability and scope.",
    )
    + _entries(
        ["maximize", "minimize", "optimize", "best", "worst"],
        "Superlative without a target.",
        "State the target value being optimized toward.",
    )
    + _entries(
        ["best-in-class", "state-of-the-art"],
        "Superlative without a target.",
        "State the target value being optimized toward.",
        whole_word=False,
    )
    + _entries(
        ["highly", "very", "extremely", "significantly",
         "substantially", "considerably", "somewhat"],
        "Vague degree modifier.",
        "Quantify the degree.",
    )
    + _entries(
        ["efficient", "performant"],
        "Subjective efficiency term — no metric.",
        "State a measurable resource/throughput target.",
    )
)

_UNIVERSAL_ENTRIES: list[_LexEntry] = (
    _entries(
        ["all", "always", "never", "every", "none", "nothing", "each",
         "everyone", "everybody", "everywhere", "anywhere", "anytime",
         "whenever", "any"],
        "Universal quantifier — a single exception invalidates it.",
        "Scope explicitly (which cases?) or state the exception handling.",
    )
    + _entries(
        ["at all times", "without exception", "under all circumstances", "in all cases"],
        "Universal quantifier — a single exception invalidates it.",
        "Scope explicitly or state the exception handling.",
        whole_word=False,
    )
)

_LOOPHOLE_ENTRIES: list[_LexEntry] = _entries(
    ["if possible", "as appropriate", "where applicable", "where appropriate",
     "when appropriate", "as needed", "as necessary", "as required",
     "if necessary", "if needed", "when needed", "if feasible", "where feasible",
     "when feasible", "as far as possible", "as much as possible",
     "to the extent possible", "to the maximum extent", "subject to availability",
     "time permitting", "resources permitting", "budget permitting",
     "at the discretion of", "depending on circumstances", "to the best of"],
    "Loophole / escape clause — makes the obligation optional.",
    "Remove the escape clause, or if it defers to an external standard, "
    "cite that standard explicitly (e.g. 'as required by ISO 27001 §8.3').",
    whole_word=False,
)


# ---------------------------------------------------------------------------
# Tier-2: passive voice
# ---------------------------------------------------------------------------

_BE_VERB_PAT = (
    r"(?:shall have been|will have been|"
    r"shall be|will be|may be|might be|could be|would be|should be|"
    r"has been|have been|had been|"
    r"am|is|are|was|were|be|been|being)"
)

_IRREGULAR_PP: frozenset[str] = frozenset({
    # Core English irregular past participles
    "arisen", "awoken", "begun", "bitten", "blown", "broken", "brought", "built",
    "bought", "caught", "chosen", "come", "cut", "dealt", "done", "drawn",
    "driven", "eaten", "fallen", "fed", "felt", "fought", "found", "flown",
    "forgotten", "forgiven", "frozen", "given", "gone", "grown", "gotten", "heard",
    "held", "hidden", "hit", "hung", "hurt", "kept", "known", "laid", "led", "left",
    "lent", "lain", "lit", "lost", "made", "meant", "met", "overcome", "paid",
    "proven", "put", "read", "ridden", "risen", "run", "said", "seen", "sent", "set",
    "sewn", "shaken", "shown", "shut", "sung", "sunk", "sat", "slept", "spoken",
    "spent", "stood", "stolen", "struck", "sworn", "swept", "taken", "taught",
    "torn", "told", "thought", "thrown", "understood", "woken", "worn", "won", "written",
    # British/Commonwealth -t variants (Round-2 panel)
    "burnt", "learnt", "spoilt", "dreamt", "spelt", "smelt",
    # Tech-common forms (regular -ed; listed for documentation/test readability)
    "configured", "initialized", "instantiated", "deprecated", "serialized",
    "deserialized", "validated", "parsed", "authenticated", "authorized",
    "cached", "indexed", "migrated", "deployed", "overridden", "overwritten",
    "decoupled", "refactored", "abstracted", "registered", "encrypted",
    "decrypted", "compressed", "decompressed", "rendered", "compiled", "linked",
    "packaged", "published", "subscribed", "triggered", "scheduled",
})

# Non-capturing adverb slots: a capturing group inside a quantified repeat makes
# the engine track per-repetition state (backtracking risk). (Round-1 panel P1.)
_PASSIVE_RE = re.compile(
    r"\b(" + _BE_VERB_PAT + r")"
    r"(?:\s+\w+){0,2}\s+"
    r"(\w+(?:ed|en))\b",
    re.IGNORECASE,
)

_IRREGULAR_RE = re.compile(
    r"\b(" + _BE_VERB_PAT + r")"
    r"(?:\s+\w+){0,2}\s+"
    r"(" + "|".join(sorted(_IRREGULAR_PP, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_BY_ACTOR_RE = re.compile(r"\bby\s+\w[\w\s]{0,30}", re.IGNORECASE)


def _iter_passive(text: str) -> list[tuple[int, int, str]]:
    """Single source of truth for passive spans, used by BOTH _detect_passive and
    _detect_missing_actor (so they cannot drift). Runs both the regular (-ed/-en)
    and irregular-participle passes; dedups by start offset, keeping the longest
    match at each start. Returns (start, end, span) tuples in document order."""
    spans: dict[int, tuple[int, int, str]] = {}
    for rx in (_PASSIVE_RE, _IRREGULAR_RE):
        for m in rx.finditer(text):
            st = m.start()
            length = m.end() - m.start()
            if st not in spans or length > (spans[st][1] - spans[st][0]):
                spans[st] = (m.start(), m.end(), m.group(0))
    return [spans[k] for k in sorted(spans)]


_PASSIVE_MSG = "Passive voice — the actor performing the action may be hidden."
_PASSIVE_SUG = "Rewrite in active voice: name the actor that performs the action."


def _detect_passive(text: str) -> list[_Match]:
    return [
        _Match(span=span, start=st, message=_PASSIVE_MSG, suggestion=_PASSIVE_SUG)
        for st, _en, span in _iter_passive(text)
    ]


def _extract_clause(text: str, match_start: int, match_end: int) -> str:
    """Return the sentence fragment enclosing a match (previous sentence boundary
    to the next). ``left`` defaults to 0 so a first-sentence passive still includes
    any 'by [actor]' that PRECEDES the verb (Round-1 panel P2 / F1).

    Known Phase-0 limitation: the boundary class also matches periods inside
    abbreviations (e.g./i.e./etc.), so a clause may truncate early there."""
    boundary = r"[.!?;]"
    left = 0
    for m in re.finditer(boundary, text[:match_start]):
        left = m.end()
    right = len(text)
    m = re.search(boundary, text[match_end:])
    if m:
        right = match_end + m.start()
    return text[left:right]


_ACTOR_HIDDEN_MSG = "Passive voice with no named actor — who performs this action?"
_ACTOR_HIDDEN_SUG = "Name the responsible actor (e.g. '... by the validation service')."


def _detect_missing_actor(text: str) -> list[_Match]:
    """A passive span with no 'by [actor]' anywhere in its enclosing clause.
    Distinct from PASSIVE_VOICE (style) — this is a responsibility-assignment
    defect, and both can fire on the same span. Evaluated per-sentence by design;
    a 'by [actor]' in a different sentence is intentionally not tracked."""
    out: list[_Match] = []
    for st, en, span in _iter_passive(text):
        clause = _extract_clause(text, st, en)
        if not _BY_ACTOR_RE.search(clause):
            out.append(_Match(span=span, start=st, message=_ACTOR_HIDDEN_MSG, suggestion=_ACTOR_HIDDEN_SUG))
    return out


# ---------------------------------------------------------------------------
# Tier-3: heuristics (lower confidence — messages say "verify")
# ---------------------------------------------------------------------------

_PRONOUN_RE = re.compile(r"\b(it|this|these|those|they|their|them)\b", re.IGNORECASE)

# Copula/definitional exclusion MUST cover modal forms — EARS is "shall be"-
# dominated, so a present/past-only pattern would flag most legitimate pronouns
# in definitional position. (Round-2 panel R1, flagged by 2 judges.)
_COPULA_RE = re.compile(
    r"\b(it|this|these|those|they)\s+"
    r"(?:is|are|was|were|shall\s+be|will\s+be|must\s+be|should\s+be|may\s+be|might\s+be)\b",
    re.IGNORECASE,
)

_PRONOUN_MSG = "Pronoun may lack a clear antecedent — verify the referent is unambiguous to all readers."
_PRONOUN_SUG = "Replace the pronoun with the specific noun it refers to."


def _detect_pronouns(text: str) -> list[_Match]:
    """Flag referential pronouns, excluding definitional copula constructions."""
    excluded: set[int] = {m.start() for m in _COPULA_RE.finditer(text)}
    out: list[_Match] = []
    for m in _PRONOUN_RE.finditer(text):
        if m.start() in excluded:
            continue
        out.append(_Match(span=m.group(), start=m.start(), message=_PRONOUN_MSG, suggestion=_PRONOUN_SUG))
    return out


_QUALITY_NOUNS = (
    "reliability", "usability", "maintainability", "scalability", "availability",
    "throughput", "latency", "accuracy", "correctness", "completeness",
    "durability", "portability", "interoperability",
)
# "performance", "security", "quality" intentionally excluded — too common in
# legitimate compound nouns without POS tagging. (Round-1 panel P5.)
_QUALITY_NOUN_RE = re.compile(
    r"(?<!\w)(" + "|".join(_QUALITY_NOUNS) + r")(?!\w)", re.IGNORECASE
)

_COMPARATIVE_RE = re.compile(
    r"(?<!\w)("
    r"better|faster|slower|higher|lower|larger|smaller|stronger|weaker|"
    r"greater|cheaper|safer|simpler|broader|"
    r"(?:more|less)\s+\w+"
    r")(?!\w)",
    re.IGNORECASE,
)

_DIGIT_RE = re.compile(r"\d")

_UNVERIFIABLE_MSG = "No measurable acceptance criterion — specify a threshold, rate, or test method."
_UNVERIFIABLE_SUG = "Add a measurable target (e.g. a number, rate, or pass/fail test)."


def _detect_unverifiable(text: str) -> list[_Match]:
    """Compound heuristic: fire when >=2 of three indicators hold —
    (1) no digits anywhere, (2) a quality '-ness' noun present,
    (3) a bare comparative present. Emits ONE smell whose span is the first
    matched quality noun or comparative (the most actionable target).

    Helpers return the match (for position), not a bare bool, so _Match.start
    has a real offset to carry. (Round-2 panel R/F4.)"""
    has_no_digits = _DIGIT_RE.search(text) is None
    qn = _QUALITY_NOUN_RE.search(text)
    cmp_ = _COMPARATIVE_RE.search(text)

    indicators = sum((has_no_digits, qn is not None, cmp_ is not None))
    if indicators < 2:
        return []

    # span/position = whichever concrete marker appears first
    candidates = [m for m in (qn, cmp_) if m is not None]
    anchor = min(candidates, key=lambda m: m.start())
    return [_Match(span=anchor.group(), start=anchor.start(),
                   message=_UNVERIFIABLE_MSG, suggestion=_UNVERIFIABLE_SUG)]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_RULES: list[_SmellRule] = [
    # Tier 1: lexical
    _SmellRule(SmellType.VAGUE_TERM,           SmellCategory.LEXICAL,   _build_lexicon_fn(_VAGUE_ENTRIES)),
    _SmellRule(SmellType.SUBJECTIVE_LANGUAGE,  SmellCategory.LEXICAL,   _build_lexicon_fn(_SUBJECTIVE_ENTRIES)),
    _SmellRule(SmellType.UNIVERSAL_QUANTIFIER, SmellCategory.LEXICAL,   _build_lexicon_fn(_UNIVERSAL_ENTRIES)),
    _SmellRule(SmellType.LOOPHOLE,             SmellCategory.LEXICAL,   _build_lexicon_fn(_LOOPHOLE_ENTRIES)),
    # Tier 2: syntactic
    _SmellRule(SmellType.PASSIVE_VOICE,        SmellCategory.SYNTACTIC, _detect_passive),
    _SmellRule(SmellType.MISSING_ACTOR,        SmellCategory.SYNTACTIC, _detect_missing_actor),
    # Tier 3: heuristic
    _SmellRule(SmellType.AMBIGUOUS_PRONOUN,    SmellCategory.SYNTACTIC, _detect_pronouns),
    _SmellRule(SmellType.UNVERIFIABLE,         SmellCategory.SEMANTIC,  _detect_unverifiable),
    # MISSING_CONDITION: emitted only by the LLM critique loop (see [0015]); no detector here.
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect(text: str, source: SmellSource = SmellSource.INPUT) -> list[Smell]:
    """Scan text for all detectable requirements smells, in document order.

    Deduplicated on ``(smell_type, start_pos)`` — the same word at two positions
    yields two smells (both occurrences matter); the key only collapses two rules
    firing at the SAME position (e.g. the regular and irregular passive passes).

    source=INPUT      — Diagnose: scanning the user's raw prompt.
    source=GENERATED  — Specify re-check: scanning engine-generated text.
    """
    if not text.strip():
        return []

    seen: set[tuple[SmellType, int]] = set()
    results: list[tuple[int, Smell]] = []
    for rule in _RULES:
        for match in rule.fn(text):
            key = (rule.smell_type, match.start)
            if key in seen:
                continue
            seen.add(key)
            results.append((match.start, Smell(
                type=rule.smell_type,
                category=rule.category,
                span=match.span,
                message=match.message,
                suggestion=match.suggestion,
                source=source,
            )))
    results.sort(key=lambda t: t[0])
    return [smell for _, smell in results]


def scan_requirement(
    req: RequirementCandidate,
    source: SmellSource = SmellSource.GENERATED,
) -> RequirementCandidate:
    """Scan ``req.text``, merge new smells into ``req.smells``, return a NEW instance.

    Deduplicated across the merge on ``(type, span, source)`` — so a GENERATED
    smell on the same span as an INPUT smell co-exists (they record different
    pipeline stages). Uses model_validate (not model_copy) so all validators re-run.

    WARNING: the default source is GENERATED. Pass source=SmellSource.INPUT when
    scanning user-authored text (Diagnose) — accidental GENERATED tagging corrupts
    the inherited/introduced smell-count distinction.
    """
    new_smells = detect(req.text, source=source)

    merged: list[Smell] = list(req.smells)
    seen: set[tuple[SmellType, str, SmellSource]] = {(s.type, s.span, s.source) for s in merged}
    for s in new_smells:
        key = (s.type, s.span, s.source)
        if key in seen:
            continue
        seen.add(key)
        merged.append(s)

    data = req.model_dump()
    data["smells"] = [s.model_dump() for s in merged]
    return RequirementCandidate.model_validate(data)
