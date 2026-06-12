# Project Decision Log — Why this project exists, and why each file exists

> Filename note: delivered as `project_decisions.md` to avoid colliding with an
> earlier `DECISIONS.md` download. This is the **canonical decision log**; commit
> it to the repo as the project's decision record (as `DECISIONS.md` or keep this
> name — author's call). Entry [0004] still refers to it by its role, not filename.

This file is the project's **rationale log**: the human-readable record of *why*
every decision was made and *why* every file exists. Until we introduce a
temporal knowledge graph (Graphiti), this file **is** our provenance store.

> How to use this log: append a `### [NNNN] <path-or-topic>` entry whenever a
> file is created or a significant decision is made. Never delete entries — if a
> decision is reversed, add a superseding entry and reference the old one by
> number. (This mirrors how the future knowledge graph supersedes facts rather
> than overwriting them, so migration is natural.)

---

## Project intent

A **prompt-to-plan engine**: it takes a messy, under-specified prompt and
progressively refines it into a fully-realized, thoroughly-documented plan.

- **Broader goal:** domain-agnostic planning.
- **Proving ground:** software specifications first (BRD/SRS), because
  requirements engineering already has mature standards to lean on.
- **Values:** free / open-source, local-first, privacy-respecting.

Five-stage methodology: **Diagnose → Interrogate → Elaborate → Specify → Persist.**

---

## Decision log

### [0000] Project foundations and chosen stack

Assemble proven open-source pieces rather than reinvent them. Each has a role:

- **GitHub Spec Kit** (MIT) — document *templates* + workflow shape. A set of
  Markdown templates, not a service; used by lifting templates into our prompts.
- **BMAD-METHOD** (MIT) — role *personas* (Analyst, PM, Architect, ...). Also
  Markdown/YAML, not a service; used as the system prompts driving each stage.
- **EARS** — five sentence patterns for unambiguous requirements; used as both
  generation format and a deterministic validation gate.
- **ISO/IEC/IEEE 29148** — the SRS document structure; used as output shape.
- **LangGraph** (MIT) — orchestration (typed state, checkpointing, HITL).
  Deferred until there is branching/persistence to manage (see [0002]).
- **Graphiti** (Apache-2.0) — temporal knowledge graph for memory/traceability.
  Deferred until we have a graph DB and move off mobile-only. `DECISIONS.md` is
  its interim stand-in.

**Cross-cutting rule:** Markdown is the human-facing source of truth; a parsed
structured form is what the machinery consumes. Stable IDs (e.g. `REQ-001`) join
the two representations.

### [0001] Five-stage pipeline as the methodological backbone

The requirements-engineering literature names every problem we face (ambiguity,
"requirements smells", under-specification) and project management names the cure
(progressive elaboration / rolling-wave planning). We encode that prior art; we
do not invent a new theory.

### [0002] Phasing — what we build first and why

**Phase 0 (current):** a headless, terminal-run vertical slice proving *messy
text in → clean validated specification out*, with one human clarification round.
No orchestration framework, no database, no web UI, single user, single document
type. Per-phase Definition-of-Done and Non-goals live in [0007]; the executed-vs-
defined stage set is reconciled in [0006].

**Defer LangGraph to Phase 1:** the Phase 0 pipeline is a straight line — nothing
to branch, checkpoint, or resume. *Mitigation:* every stage is a pure
`state → state` function over `PipelineState`, so it wraps into a LangGraph node
with no rework.

**Defer Graphiti to Phase 1+:** it needs a graph database and cannot run on
mobile. `DECISIONS.md` plus rendered Markdown carry provenance until then.

**Active from Phase 0 (not placeholders):** Spec Kit templates, BMAD personas,
EARS format + validator, ISO 29148 shape.

### [0003] Runtime — build the engine at full quality; deliver cross-device via the wrapper

> Status: CONFIRMED. (Supersedes an earlier proposal that constrained the engine
> to the standard library so it could run directly on an Android phone.)

The earlier proposal traded quality for compatibility, which we reject. The
correction separates two concerns:

- **The engine** (the backend) runs at full quality on a capable machine. It uses
  the libraries the design calls for — `pydantic`, real model SDKs and/or a local
  OpenAI-compatible endpoint, proper NLP where it helps.
- **The product surface** (the wrapper) is what must run "on most devices" — a
  web/PWA client talking to the engine over HTTP, delivered last. A phone runs
  the *client*, never the engine.

Standard client–server architecture. Development on the PC; the phone can drive
remotely (SSH/Tailscale, or the GitHub pull-request loop). Local-first preserved:
the model can be served locally (Ollama/vLLM), so no data need leave the hardware.

### [0004] `DECISIONS.md`

Makes the project's reasoning durable and inspectable, and is the interim
provenance store before Graphiti. Plain Markdown so it is readable/editable on
any device, including the phone this is being built from.

### [0005] `hypothesis/models.py` (initial version)

> Superseded by [0008], which revised the contracts after a multi-model critique
> round. Retained for history.

First cut of the canonical pydantic data contracts for the four stages.

### [0006] Reconciling the five-stage methodology with Phase 0 execution

**Problem (caught by critique):** [0001] names five stages; [0002] described
Phase 0 as a straight short line — leaving "Elaborate" ambiguous.

**Decision:** the five stages are the *methodology*. Phase 0 *executes*
**Diagnose → Interrogate → Specify**. **Elaborate** and **Persist** are real
stages with defined data contracts (`ElaboratedPrompt`; persistence via rendered
Markdown) but their *execution* is deferred. `PipelineState` carries a slot for
each so turning them on later is wiring, not a contract change. This removes the
contradiction without gold-plating Phase 0.

### [0007] Per-phase Definition of Done and Non-goals (the log is a contract, not a manifesto)

**Phase 0 — Definition of Done:**
- A messy prompt produces an ISO-29148-shaped `SpecDocument` with EARS-formatted
  requirements and one recorded human clarification round.
- Deterministic gates run on the engine's own output: EARS validation + smell
  re-check, surfaced in a `QualityReport`.
- The deterministic logic (EARS validator, smell detector) has tests that pass
  with no API key.

**Phase 0 — Non-goals (explicitly out of scope, by design):**
- No orchestration framework, database, web UI, or multi-user.
- No full 29148 artifact: no V&V section, references, or traceability matrix yet.
- No streaming/async clarification, no recursive loop-back execution, no
  automatic parse-repair. (Seams exist for all of these; see [0008].)

**Later phases (brief):** Phase 1 — LangGraph orchestration + Graphiti memory +
persistence (DoD: requirement evolution is queryable). Phase 2 — FastAPI + web
surface. Phase 3 — multi-user/sharing. Phase 4 — multi-domain + full BMAD team +
the critique loop ([0011]).

### [0008] `hypothesis/models.py` revision (post-critique)

Three independent model critiques (GLM, ChatGPT, Gemini) were reconciled into the
following contract changes. Verdicts: **DO-NOW**, **DEFER-with-seam**, **DECLINE**.

**DO-NOW (implemented):**
- **Slots authoritative; `text` is a rendered cache.** All three critics flagged
  text/slot divergence. The model now treats the EARS slots as the source of
  truth; `ears.py` ([0010]) synthesizes `text` from them. A validator enforces
  the floor invariant (a valid requirement must have `system_name` +
  `system_response`).
- **`PipelineState`** added — the single object every stage receives/returns;
  also the future LangGraph state.
- **`ambiguity_score`** is now a derived property (cannot desync from the list).
- **`ClarificationQA.status`** enum (PENDING/RESOLVED/DISMISSED) replaces a
  `skipped` bool, with a validator forbidding answer-without-resolution. Also the
  seam for streaming clarification.
- **`provenance`** is now `list[TraceabilityLink]` (carries a *reason*), not bare
  strings. `TraceSourceType` includes `CRITIQUE`, pre-wiring the critique loop
  ([0011]) into provenance.
- **`id`** is regex-constrained (`^REQ-\d{3,}$`), generated centrally.
- **`PlanArtifact` base** carries `schema_version`, `revision`,
  `parent_revision_id`; `SpecDocument` extends it (see schema-layer decision
  below).
- **`QualityReport`** carries failure *lists* (invalid ids, smell ids, parse
  errors) and inherited-vs-introduced smell counts (`Smell.source`).
- **`extra="forbid"`** on all contracts via a `Contract` base.

**DECLINE (gold-plating for Phase 0), with reasons:**
- **`strict=True`** — declined; it rejects valid coercions (e.g. `"3"` → int)
  that model JSON routinely emits. `extra="forbid"` gives the safety we want
  without that cost.
- **Full `SchemaProvider`/`TemplateRegistry`** (domain-agnostic engine) —
  declined *for now*; replaced by the lightweight `PlanArtifact` inheritance
  seam. Other domains attach as sibling subclasses; a registry can come later if
  the inheritance seam proves insufficient.
- **Nested `composite_parts`** for COMPLEX EARS — declined; the slots are
  independent optionals and already represent combined clauses (a "While…,
  When…" requirement sets both `state` and `trigger`). Revisit only if a real
  requirement cannot be modeled flat.
- **`frozen=True` on `SpecDocument`** — declined; the document is mutated during
  the build/critique loop. Revisit when a document is "sealed".

**DEFER with a seam:** recursive loop-back (`PipelineState.iteration`); streaming
HITL (the `status` enum); parse-and-repair (the per-requirement isolation that
`extra="forbid"` enables — the repair pass will tie into [0011]).

**Implementation note (found via testing):** `@computed_field` + `extra="forbid"`
breaks JSON round-trips (computed fields serialize but are rejected as "extra" on
reload). Resolved by using plain `@property` for the three derived values — they
stay un-desyncable but are not serialized. Verified by a round-trip test.

### [0009] Cross-cutting decisions (previously unstated)

- **LLM integration strategy:** provider-agnostic; a local OpenAI-compatible
  endpoint (Ollama/vLLM) is the default, hosted APIs a config swap. Structured
  output via JSON / tool-calls validated by the `Contract` models. Prompts live
  as editable files under `prompts/`. The multi-provider *rotation* is owned by
  the orchestrator being integrated in [0011].
- **Error-handling philosophy:** fail-soft per requirement. `extra="forbid"`
  catches malformed responses at the boundary; a single bad `RequirementCandidate`
  is isolated rather than failing the whole document; an automatic repair pass is
  deferred ([0008] defer list).
- **Rendering:** `SpecDocument → spec.md` via a dedicated `render.py` (planned),
  using Spec Kit's template shape. The structured object remains source of truth.
- **Human-in-the-loop mechanism (Phase 0):** terminal stdin/stdout, one batch
  round. The `status` enum seams richer interaction later.
- **Testing strategy:** pytest unit tests on the deterministic pieces (EARS
  validator, smell detector) runnable with no API key; mock-LLM integration tests
  for the stages; golden-file tests on rendered specs.

### [0010] `hypothesis/ears.py` (planned — next file)

**What it will be:** the EARS validator/classifier and the authoritative
slot → sentence synthesizer. Pure logic, no model calls, fully unit-testable.

**Why it exists:** it makes [0008]'s "slots authoritative" decision real — it
parses/validates the EARS slots, assigns the `EarsPattern` (including COMPLEX when
multiple clauses are present), and renders the canonical `text` so what is
validated is exactly what is displayed.

### [0011] Multi-model critique loop as core architecture

**Decision:** the adversarial critique we have been doing manually (multiple LLMs
from different providers critiquing each generation) becomes a **core part of the
engine**, not an external step. When the engine produces an artifact (notably the
SRS), a rotation of models across providers critiques it; the lead model plus the
human-in-the-loop hold final say.

**Status / open work:** the user has built an orchestrator (rotates LLMs/providers
and feeds context). It has now been read and the integration is **designed and
confirmed in [0012]**. `TraceSourceType.CRITIQUE` lets a critique be recorded as
provenance on any requirement it changes, so debate outcomes become part of the
traceable record.

### [0012] Integrating the multi-model orchestrator (critique loop as core)

> Status: CONFIRMED — all four integration forks confirmed by the author.

The author's senior-project orchestrator (a generic, template-driven, role-based
staged pipeline with multi-provider rotation; providers: OpenRouter, Cerebras,
Groq, NVIDIA) is folded into the engine as follows:

- **HARVEST** `providers.py` + `scheduler.py` + `oplog.py` as the engine's **LLM
  transport layer** — this replaces the hand-written `llm.py` planned in [0009].
  Rationale: it already does provider rotation, per-RPM pacing, health/cooldown/
  blacklist routing, model-family scoring, and OpenAI-compatible calls with
  `response_format` (structured output straight into our pydantic). It is better
  than building anew and is the author's own code (license-compatible).
- **REUSE** the `critiquer → transformer → verifier` role pattern as a **critique
  Task template**, invoked at gate points. The "debate" is the `critiquer` fanned
  out across rotated slots; "the lead model + human have final say" is a reconcile
  stage plus a human gate.
- **KEEP** the typed pydantic + LangGraph spine for the plan-engine domain logic.
  The orchestrator's untyped string/JSON `context` dict is NOT used for the typed
  requirement core. (The build-everything-on-top-of-the-orchestrator alternative
  was considered and declined: it would forfeit typed contracts and EARS/29148
  rigor.)
- **BOUNDARY:** in-process import, behind a `CritiquePanel` protocol, so a later
  switch to a separate HTTP service needs no pipeline change.
- **GATE PLACEMENT:** critique gate after Specify always; after Diagnose/Elaborate
  optional/flagged (to bound token spend).
- **BRIDGE:** `SpecDocument` → rendered markdown → orchestrator critique Task →
  parsed back into typed `Critique` / `CritiqueRound`. Applied critiques recorded
  as `TraceabilityLink(source_type=CRITIQUE)` (already wired in models.py).
- **NOT YET IN REPO / SECURITY:** the orchestrator code is not in the repo, and the
  shipped archive contained a `.env` with live API keys (author advised to rotate
  them; never commit secrets). Next-session action: author adds the needed
  orchestrator modules (`providers.py`, `scheduler.py`, `oplog.py`, `roles.py`,
  `schematics.py`, `content/prompts/*`) WITHOUT the `.env`, so the adapter can be
  wired.
- **NEXT ARTIFACTS for this integration:** the critique-loop Task template (JSON),
  the `Critique` / `CritiqueRound` pydantic contracts, and the `CritiquePanel`
  adapter wrapping the scheduler.

### [0013] Maximum-rigor directive (model selection)

> Status: CONFIRMED.

The author's directive is **maximum quality**: use top-tier models and run full
multi-model critique on generations, rather than minimizing cost via aggressive
tiering. This is consistent with the project's whole reason for existing — the
orchestrator's provider rotation is precisely what makes many top-tier calls
affordable without tripping rate limits. Cost-tiering (cheap models for bulk
classification) remains available but is secondary; the rotation engine, not
tier-downgrading, is the primary throughput mechanism.

### [0014] `hypothesis/ears.py`

**What it is:** Deterministic EARS classifier, validator, and canonical-sentence synthesizer.
Pure Python, no model calls, fully unit-testable without an API key. 32 tests, all green.

**Why it exists:** Makes [0008]'s "slots are authoritative" real. `RequirementCandidate.text`
must be synthesized from the slots by this single authoritative function — never hand-authored,
never LLM-written. Every stage that produces or mutates a requirement calls `ears.apply()`.

**Key choices baked in:**
- `CLAUSE_SLOTS` constant defines canonical ordering for both classification and synthesis —
  they cannot silently diverge.
- `_normalize()` collapses None/empty/whitespace-only to None before any logic runs; an LLM
  emitting `""` or `"  "` for an unused slot is treated as absent.
- `COMPLEX` is a **structural composition label** (≥2 clause slots filled), NOT a normative EARS
  pattern from Mavin et al. (2009). Pre-existing in `EarsPattern` ([0008]); future consumers must
  not treat it as a sixth normative pattern.
- Synthesis preserves all clause content exactly — `OAuth`, `iPhone`, `gRPC`, `Auth0` unchanged.
  Only the first character of the whole sentence is uppercased.
- `_article()` prevents double-article ("The The Payment System shall...") in all sentence templates,
  not just UBIQUITOUS.
- "then" appears in UNWANTED (sole precondition) per EARS canonical form; omitted in COMPLEX
  patterns where precondition is one of several clauses.
- `synthesize()` returns `""` for INVALID requirements — stale text is never preserved.
- `apply()` uses `model_validate` (not `model_copy`) so `_ears_invariant` and all field
  validators re-run on the result. `model_copy` uses `model_construct` and bypasses them silently.
- `ears_valid` is derived from `validate()`, not `classify()` — `validate()` is the single
  source of truth for validity, preventing divergence if `validate()` gains new checks later.
- Named `ERR_*` constants freeze the validation message contract for tests and downstream tooling.
- `validate()`'s third check (INVALID after floor fields pass) is currently dead code — no input
  reaches it. Retained as a forward-compatibility gate; noted here so future readers don't trace it.

**Test notes (caught during testing):**
- `"then"` as a substring check is ambiguous — "au**then**ticate" contains it. Tests correctly
  check for `", then "` (the UNWANTED construct) to avoid false positives.
- Parametrized whitespace test covers `""`, `" "`, `"\t"`, `"\n"`, `"  \t  "` for clause slots.
- Two test assertions needed fixing after first run (substring vs. lowercased search); implementation
  was correct throughout. Noted per "test the deterministic pieces yourself" working agreement.

**Follow-up items (not in scope for Phase 0):**
- `_normalize()` is private to `ears.py`. When `smells.py` needs the same logic, extract to
  `hypothesis/utils.py`. Do not extract prematurely.
- Interrogate stage question count: default 5, LLM-determined (not hard-capped). Implemented
  in `prompts/interrogate.md` when that stage is built; no models or ears change needed.
- `models.py` `_ears_invariant` whitespace gap closed by `@field_validator` on `system_name`/
  `system_response` added in commit 2 of this session.

**Anticipates / will be replaced by:** Phase 1 may add composite-part nesting if flat slots prove
insufficient ([0008] DECLINE). Template strings may be externalized to `prompts/` for per-domain
variants. Next file: `hypothesis/smells.py` (deterministic smell detector).

---

### [0015] `hypothesis/smells.py`

**What it is:** Deterministic requirements smell detector. Scans text, returns
`list[Smell]` in document order. No model calls, no API key, stdlib `re` only.

**Why it exists:** Second deterministic gate alongside `ears.py`. Implements the
`SmellSource.INPUT` vs `SmellSource.GENERATED` distinction — letting the engine measure
whether its own generation introduced new defects vs. inherited them from the prompt.
Feeds `QualityReport.inherited_smell_count` / `introduced_smell_count` (already defined
in `models.py`).

**Plan provenance:** The design went through two adversarial critique rounds before
implementation. Round 1: manual multi-agent review (8 findings P1–P8, all accepted).
Round 2: live hosted panel — kimi-k2.6, deepseek-v4-flash, nemotron-ultra, 3/3 judges OK
via the critique service — 12 triaged findings R1–R12: 8 accepted, 2 rejected as false
positives (with reasoning recorded in the plan), 2 deferred. Plus a code-review pass that
caught a CRITICAL `_extract_clause` boundary-initialization bug before any code was written.

**Key choices baked in:**
- Registry pattern (`_SmellRule` + `_RULES` list) — each detector independently testable;
  adding a smell type = append one entry. Coupling via data, not code (same idea as
  `CLAUSE_SLOTS` in ears.py).
- `_LexEntry` + `_build_lexicon_fn()` factory builds all four Tier-1 lexical detectors;
  patterns compile once at module load. Word boundary is `(?<!\w)..(?!\w)` lookaround,
  not `\b` (which misfires on hyphenated compounds like "user-friendly").
- Tiering: 1 lexical (VAGUE_TERM, SUBJECTIVE_LANGUAGE, UNIVERSAL_QUANTIFIER, LOOPHOLE),
  2 syntactic regex (PASSIVE_VOICE, MISSING_ACTOR), 3 heuristic (AMBIGUOUS_PRONOUN,
  UNVERIFIABLE), 4 deferred to LLM (MISSING_CONDITION — enum value exists, no detector;
  the critique loop emits it through the same merge path).
- `_iter_passive()` is the single source of truth for passive spans, shared by BOTH
  `_detect_passive` and `_detect_missing_actor` so the two can never drift. Runs the
  regular (-ed/-en) and irregular-participle passes; keeps the longest match per start
  offset. Adverb slots are non-capturing `(?:\s+\w+){0,2}` (panel P1: backtracking).
- MISSING_ACTOR searches the whole enclosing sentence via `_extract_clause()` (panel P2:
  a forward-only window misses "By X, the Y shall be Z"). `left` initializes to 0, not
  `match_start` — the CRITICAL pre-implementation catch (F1): without it, first-sentence
  "by"-before-verb clauses were cut off and MISSING_ACTOR false-fired.
- AMBIGUOUS_PRONOUN copula exclusion covers modal forms (`it shall be`, `this will be`…)
  — EARS text is "shall be"-dominated, so present/past-only exclusion would flag most
  legitimate definitional pronouns (panel R1, the headline Round-2 catch).
- UNVERIFIABLE is a compound heuristic (≥2 of: no digits / quality-ness noun / bare
  comparative). "performance", "security", "quality" are deliberately NOT quality nouns
  (panel P5: compound-noun false positives without POS tagging). Helpers return the regex
  match, not bool, so the emitted span carries a real position.
- Lexicon corrections from the panel: "rarely" excluded from VAGUE_TERM (defined direction
  quantifier, P4); "somewhat" added; British -t participles (burnt/learnt/spoilt/dreamt/
  spelt/smelt) added; LOOPHOLE "as required"/"as necessary" keep firing but the suggestion
  directs users to cite the external standard explicitly (P6 — regulatory text).
- Two-level deduplication, intentionally asymmetric: `detect()` dedups on
  `(type, start_pos)` — same word at two positions = two smells; two rules at one position
  (regular + irregular passive) = one. `scan_requirement()` merge dedups on
  `(type, span, source)` — position isn't stable across pipeline stages, and INPUT vs
  GENERATED on the same span are distinct events that must coexist (P3).
- `scan_requirement()` uses `model_validate` (not `model_copy`) for the same reason as
  `ears.apply()`: all validators re-run. Default `source=GENERATED` is a documented
  footgun — callers scanning user text must pass `source=INPUT` explicitly (P8/R5;
  regression-tested).
- `detect()` early-returns `[]` on empty/whitespace-only text (guards the UNVERIFIABLE
  no-digits indicator from firing on nothing).

**Known accepted limitations (Phase 0):**
- Passive-voice F1 ~70%: perfect tense and adjectival participles ("the broken link")
  false-positive; `-en` suffix admits non-participles ("open", "golden"). LLM critique
  layer filters before human review.
- `_extract_clause` boundary class `[.!?;]` fires on periods inside abbreviations
  (e.g./i.e.), occasionally truncating the clause window (R2).
- UNVERIFIABLE misses adjective-form unverifiables ("durable and reliable") — quality
  nouns only; ≥1 threshold was considered and rejected as FP blow-up (R3).
- "any" realistically ~80% precision, not the ~95% of the absolute universals (R4).
- Tier-3 messages say "verify…" — they are flags for review, not verdicts.

**Test notes:** 67 tests, all green first run; full suite 99 (32 ears + 67 smells), zero
regressions. Regression tests pin every panel finding that changed behavior: F1 (by-clause
before passive), R1 (modal copula), P4 (rarely), P5 (performance), P8/R5 (source override),
dedup overlap (tech-verb single smell), per-sentence actor scoping, different-source
coexistence.

**Follow-up items:**
- Lexicons are Python constants for Phase 0; externalize to `hypothesis/data/*.json` for
  per-domain customization (regulated industries need broader loophole lexicons).
- Permissive-modal smell ("may/might" as requirement language) is a real gap but a NEW
  smell category — deferred (R11).
- Abbreviation-aware sentence segmentation for `_extract_clause` (R2).
- MISSING_CONDITION goes active when the critique loop is wired (next milestone).

**Anticipates / will be replaced by:** Phase 1 per-domain lexicons and possible POS tagging
if passive-voice F1 proves too low in practice. The critique loop consumes the same `Smell`
contract, so its findings merge through `scan_requirement()` unchanged.

---

## Entry template (copy for each new file)

```
### [NNNN] <relative/path/to/file>

**What it is:** <one or two sentences>

**Why it exists:** <the problem it solves>

**Key choices baked in:** <decisions a future reader should know about>

**Anticipates / will be replaced by:** <what later phase changes this, if any>
```
