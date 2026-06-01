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

---

## Entry template (copy for each new file)

```
### [NNNN] <relative/path/to/file>

**What it is:** <one or two sentences>

**Why it exists:** <the problem it solves>

**Key choices baked in:** <decisions a future reader should know about>

**Anticipates / will be replaced by:** <what later phase changes this, if any>
```
