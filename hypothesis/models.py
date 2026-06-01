"""
hypothesis/models.py
====================
Canonical data contracts for the prompt-to-plan engine.

These models are the *typed source of truth* that every stage reads and writes.
The rendered Markdown specification is produced FROM these objects; the objects
are never reconstructed by guessing at Markdown. The stable identifier on each
requirement is the join key between this structured form, the rendered document,
and the future knowledge-graph node.

Design rules enforced here (see project_decisions.md [0008]):
  * Slots are authoritative; `RequirementCandidate.text` is a rendered cache.
  * `extra="forbid"` everywhere, so a hallucinated/misspelled field in a model
    response is rejected at the boundary rather than corrupting a later step.
  * `strict=True` is deliberately NOT used: it rejects valid coercions (e.g. the
    string "3" for an int severity) that LLM JSON routinely produces.
  * Provenance is structured (`TraceabilityLink`), not bare strings, so "why did
    this change?" is answerable later without a rewrite.
  * `SpecDocument` is a software-domain subclass of a generic `PlanArtifact`, so
    other domains attach as sibling subclasses without touching the engine.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    """Base for every pipeline data contract.

    `extra="forbid"` is the single most valuable validation setting for this
    system: when a stage parses a model's JSON response, any field the model
    invented (or misspelled) raises immediately instead of being silently
    dropped and surfacing as a bug three stages downstream.
    """
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class SmellSource(str, Enum):
    """Distinguishes defects inherited from the user's prompt from defects the
    engine introduced while generating. Lets the engine measure whether its own
    generation is making things worse (a key signal for the critique loop)."""
    INPUT = "input"          # present in the original prompt
    GENERATED = "generated"  # introduced by the engine's own output


class SmellCategory(str, Enum):
    """Where a quality defect lives, following the requirements-smell literature."""
    LEXICAL = "lexical"      # word level: vague terms, subjective adjectives
    SYNTACTIC = "syntactic"  # sentence level: passive voice hiding the actor
    SEMANTIC = "semantic"    # meaning level: contradictions, missing conditions


class SmellType(str, Enum):
    VAGUE_TERM = "vague_term"
    SUBJECTIVE_LANGUAGE = "subjective_language"
    AMBIGUOUS_PRONOUN = "ambiguous_pronoun"
    PASSIVE_VOICE = "passive_voice"
    MISSING_ACTOR = "missing_actor"
    MISSING_CONDITION = "missing_condition"
    UNVERIFIABLE = "unverifiable"                  # no measurable acceptance criterion
    UNIVERSAL_QUANTIFIER = "universal_quantifier"  # "all", "always", "never"
    LOOPHOLE = "loophole"                          # "if possible", "as appropriate"


class AmbiguityCategory(str, Enum):
    """The ten dimensions along which a prompt can be under-specified."""
    FUNCTIONAL_SCOPE = "functional_scope"
    DATA_MODEL = "data_model"
    UX_FLOW = "ux_flow"
    NON_FUNCTIONAL = "non_functional"
    INTEGRATION = "integration"
    EDGE_CASES = "edge_cases"
    CONSTRAINTS = "constraints"
    TERMINOLOGY = "terminology"
    COMPLETION_SIGNALS = "completion_signals"
    PLACEHOLDERS = "placeholders"


class TraceSourceType(str, Enum):
    """What kind of thing justifies a requirement or a change to one."""
    PROMPT = "prompt"
    CLARIFICATION = "clarification"
    ELABORATION = "elaboration"
    CRITIQUE = "critique"      # produced by the multi-model critique loop
    HUMAN = "human"            # a direct human-in-the-loop decision


class ClarificationStatus(str, Enum):
    """Lifecycle of a single clarifying question. Replaces an earlier `skipped`
    boolean, which allowed the contradictory state of being answered AND skipped.
    Also the seam for future incremental/streaming clarification."""
    PENDING = "pending"      # asked, not yet resolved
    RESOLVED = "resolved"    # the human answered it
    DISMISSED = "dismissed"  # the human chose to skip / it no longer applies


class EarsPattern(str, Enum):
    """EARS patterns. A requirement may legitimately combine clauses (e.g. a
    While-state plus a When-trigger); that is classified COMPLEX and represented
    by populating more than one slot, not by a special structure.

    Note: COMPLEX is a structural composition label (>=2 clause slots filled),
    not a normative EARS pattern from Mavin et al. (2009). See [0014]."""
    UBIQUITOUS = "ubiquitous"   # The <system> shall <response>.
    EVENT = "event"             # When <trigger>, the <system> shall <response>.
    STATE = "state"             # While <state>, the <system> shall <response>.
    UNWANTED = "unwanted"       # If <condition>, then the <system> shall <response>.
    OPTIONAL = "optional"       # Where <feature>, the <system> shall <response>.
    COMPLEX = "complex"         # >1 clause present (slots combined)
    INVALID = "invalid"         # does not parse as EARS at all


class RequirementKind(str, Enum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    INTERFACE = "interface"
    CONSTRAINT = "constraint"


class PipelineStage(str, Enum):
    """The methodology's five stages plus terminal DONE. Phase 0 executes
    DIAGNOSE -> INTERROGATE -> SPECIFY; ELABORATE and PERSIST are defined but
    deferred (see project_decisions.md [0006])."""
    DIAGNOSE = "diagnose"
    INTERROGATE = "interrogate"
    ELABORATE = "elaborate"
    SPECIFY = "specify"
    PERSIST = "persist"
    DONE = "done"


# ---------------------------------------------------------------------------
# Leaf models
# ---------------------------------------------------------------------------

class Smell(Contract):
    """A single detected quality defect in a piece of text."""
    type: SmellType
    category: SmellCategory
    span: str = Field(description="The exact offending substring.")
    message: str = Field(description="Why this is a problem, in plain language.")
    suggestion: str | None = Field(default=None, description="A concrete fix, if known.")
    source: SmellSource = SmellSource.INPUT


class AmbiguityItem(Contract):
    """An under-specified point that may warrant a clarifying question."""
    category: AmbiguityCategory
    description: str = Field(description="What is unclear or missing.")
    suggested_question: str = Field(description="A question that would resolve it.")
    severity: int = Field(ge=1, le=5, description="1 = minor, 5 = blocks the spec.")


class TraceabilityLink(Contract):
    """Structured provenance: not just *what* justifies a requirement but *why*.
    This is the seam Graphiti will consume; a bare string list could not carry a
    reason and would force a rewrite later."""
    source_type: TraceSourceType
    source_id: str | None = Field(default=None, description="e.g. a clarification or critique id.")
    reason: str = Field(description="The justification, e.g. 'User answered No to Q4 in round 2'.")
    note: str | None = None
    timestamp: datetime | None = None


class DesignOption(Contract):
    """One option weighed during elaboration."""
    name: str
    description: str
    tradeoffs: str | None = None
    chosen: bool = False


# ---------------------------------------------------------------------------
# Stage 1 - Diagnose
# ---------------------------------------------------------------------------

class DiagnosedPrompt(Contract):
    """Output of Diagnose: the raw prompt plus everything wrong or unclear about
    it. Input to Interrogate."""
    raw_prompt: str
    smells: list[Smell] = Field(default_factory=list)
    ambiguities: list[AmbiguityItem] = Field(default_factory=list)

    @property
    def ambiguity_score(self) -> float:
        """Derived, never stored: normalized 0-1 signal of under-specification.
        Because it is computed from `ambiguities`, it can never disagree with
        the list it summarizes. Formula: mean severity scaled to 0-1."""
        if not self.ambiguities:
            return 0.0
        return sum(a.severity for a in self.ambiguities) / (5 * len(self.ambiguities))


# ---------------------------------------------------------------------------
# Stage 2 - Interrogate
# ---------------------------------------------------------------------------

class ClarificationQA(Contract):
    """A single clarifying question and its resolution."""
    question: str
    category: AmbiguityCategory
    status: ClarificationStatus = ClarificationStatus.PENDING
    answer: str | None = Field(default=None, description="Present only when RESOLVED.")

    @model_validator(mode="after")
    def _status_answer_consistency(self) -> "ClarificationQA":
        if self.status is ClarificationStatus.RESOLVED and not (self.answer and self.answer.strip()):
            raise ValueError("A RESOLVED clarification must carry a non-empty answer.")
        if self.status is not ClarificationStatus.RESOLVED and self.answer:
            raise ValueError("Only a RESOLVED clarification may carry an answer.")
        return self

    @property
    def answered(self) -> bool:
        return self.status is ClarificationStatus.RESOLVED


class ClarificationRound(Contract):
    """One round of clarifying questions. Phase 0 runs exactly one round;
    `round_index` leaves room for more (the spec holds a list of rounds)."""
    round_index: int = 0
    items: list[ClarificationQA] = Field(default_factory=list)

    @property
    def answered_items(self) -> list[ClarificationQA]:
        return [qa for qa in self.items if qa.answered]


# ---------------------------------------------------------------------------
# Stage 3 - Elaborate  (contract defined; execution deferred - [0006])
# ---------------------------------------------------------------------------

class ElaboratedPrompt(Contract):
    """Output of Elaborate: the clarified intent expanded into a structured brief
    that Specify turns into requirements. Defined now so the pipeline state has a
    real slot for it; in Phase 0 it may be skipped or filled minimally."""
    refined_statement: str
    options_considered: list[DesignOption] = Field(default_factory=list)
    derived_constraints: list[str] = Field(default_factory=list)
    open_decisions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 4 - Specify
# ---------------------------------------------------------------------------

class RequirementCandidate(Contract):
    """A single requirement.

    The decomposed EARS slots are AUTHORITATIVE. `text` is a human-readable cache
    rendered from the slots by `ears.py`; it must never be edited independently.
    Storing slots (not just a sentence) is what makes conformance mechanically
    checkable and lets the canonical sentence be regenerated deterministically.

    `provenance` and `superseded_by` power later traceability (Graphiti). They
    are populated lazily; harmless when empty.
    """
    id: str = Field(pattern=r"^REQ-\d{3,}$", description="Stable id, e.g. 'REQ-001'. Generated centrally.")
    kind: RequirementKind = RequirementKind.FUNCTIONAL
    text: str = Field(default="", description="Rendered cache of the slots; authored by ears.py, not by hand.")

    # Authoritative EARS slots (filled/validated by ears.py)
    ears_pattern: EarsPattern = EarsPattern.INVALID
    precondition: str | None = None
    trigger: str | None = None
    state: str | None = None
    feature: str | None = None
    system_name: str | None = None
    system_response: str | None = None
    ears_valid: bool = False

    rationale: str | None = Field(default=None, description="Why this requirement exists.")
    smells: list[Smell] = Field(default_factory=list)
    provenance: list[TraceabilityLink] = Field(default_factory=list)
    superseded_by: str | None = Field(default=None, description="Id of the requirement that replaces this one.")

    @field_validator("system_name", "system_response", mode="before")
    @classmethod
    def _normalize_system_fields(cls, v: object) -> object:
        """Normalize blank/whitespace-only strings to None.

        Closes the gap where _ears_invariant passed on '  ' because Python
        truthiness treats non-empty strings as truthy regardless of content.
        An LLM emitting '' or '   ' for an unused field is treated as absent."""
        if isinstance(v, str):
            stripped = v.strip()
            return stripped if stripped else None
        return v

    @model_validator(mode="after")
    def _ears_invariant(self) -> "RequirementCandidate":
        # A requirement asserted valid must have the two universally-required
        # EARS slots. Full pattern checking lives in ears.py; this is the floor.
        if self.ears_valid and not (self.system_name and self.system_response):
            raise ValueError("A valid EARS requirement requires system_name and system_response.")
        return self


class SpecIntroduction(Contract):
    """ISO/IEC/IEEE 29148 SRS - Introduction section."""
    purpose: str = ""
    scope: str = ""
    definitions: dict[str, str] = Field(default_factory=dict)
    stakeholders: list[str] = Field(default_factory=list)


class SpecOverview(Contract):
    """ISO/IEC/IEEE 29148 SRS - Overall Description section."""
    product_perspective: str = ""
    user_characteristics: str = ""
    assumptions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class QualityReport(Contract):
    """The engine's deterministic self-assessment of its own output, so the
    rendered spec reports honestly instead of merely asserting completeness.
    Carries failure *lists* (not just counts) so a reviewer can act without
    re-running the pipeline."""
    total_requirements: int = 0
    invalid_requirement_ids: list[str] = Field(default_factory=list)
    requirements_with_smell_ids: list[str] = Field(default_factory=list)
    ears_parse_errors: list[str] = Field(default_factory=list)
    inherited_smell_count: int = 0
    introduced_smell_count: int = 0

    @property
    def ears_conformant(self) -> int:
        return max(self.total_requirements - len(self.invalid_requirement_ids), 0)

    @property
    def ears_conformance_rate(self) -> float:
        if self.total_requirements == 0:
            return 0.0
        return self.ears_conformant / self.total_requirements


# ---------------------------------------------------------------------------
# Artifact base (domain-agnostic seam) and the software-domain specialization
# ---------------------------------------------------------------------------

class PlanArtifact(Contract):
    """Domain-agnostic base for any planning deliverable.

    `SpecDocument` is the software-domain subclass. Other domains (a business
    plan, a construction plan, ...) attach as sibling subclasses later WITHOUT
    touching the engine. This inheritance seam is the lightweight stand-in for a
    full schema registry, which is deliberately deferred (project_decisions.md [0008]).
    """
    artifact_id: str = Field(description="Stable id for this artifact.")
    title: str
    source_prompt: str = Field(description="The original messy prompt, kept for traceability.")
    schema_version: str = Field(default="0.1.0", description="Contract version, for migration safety.")
    revision: int = Field(default=1, ge=1)
    parent_revision_id: str | None = Field(default=None, description="Seam for Graphiti supersession.")
    created_at: datetime | None = None


class SpecDocument(PlanArtifact):
    """The engine's primary Phase 0 deliverable: a 29148-shaped Software
    Requirements Specification. Consciously MINIMAL for Phase 0 (no V&V,
    references, or full traceability matrix yet - [0007] non-goals).
    Rendered to spec.md for humans; this object stays the machine-readable truth."""
    introduction: SpecIntroduction = Field(default_factory=SpecIntroduction)
    overview: SpecOverview = Field(default_factory=SpecOverview)
    requirements: list[RequirementCandidate] = Field(default_factory=list)
    clarifications: list[ClarificationRound] = Field(default_factory=list)
    quality_report: QualityReport = Field(default_factory=QualityReport)


# ---------------------------------------------------------------------------
# Pipeline-level state - the single object every stage receives and returns
# ---------------------------------------------------------------------------

class PipelineState(Contract):
    """The unified state threaded through the whole pipeline.

    Every stage is a pure function `state -> state` over this object, which makes
    the pipeline resumable and is EXACTLY the state type a LangGraph node will
    take in Phase 1 - so that migration is wiring, not rework.

    `iteration` is the seam for future recursive refinement: a finished
    `SpecDocument` can seed a fresh DiagnosedPrompt at iteration N+1. Phase 0
    runs a single linear pass (iteration stays 0).
    """
    raw_prompt: str
    iteration: int = 0
    current_stage: PipelineStage = PipelineStage.DIAGNOSE
    diagnosed: DiagnosedPrompt | None = None
    clarifications: list[ClarificationRound] = Field(default_factory=list)
    elaborated: ElaboratedPrompt | None = None
    spec: SpecDocument | None = None
