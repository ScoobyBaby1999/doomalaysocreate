"""
hypothesis/ears.py
==================
Deterministic EARS classifier, validator, and canonical-sentence synthesizer.

No model calls. No I/O. Pure functions over RequirementCandidate.

Public API (see __all__):
  classify(req)   -> EarsPattern       -- what pattern the slots form
  validate(req)   -> list[str]         -- error strings; empty = valid
  synthesize(req) -> str               -- the canonical EARS sentence
  apply(req)      -> RequirementCandidate  -- all three in one call; returns new instance

Design decisions recorded in project_decisions.md [0014].
"""

from __future__ import annotations

from hypothesis.models import EarsPattern, RequirementCandidate

__all__ = ["classify", "validate", "synthesize", "apply"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical clause-slot order: most-framing first, closest-to-action last.
# Both classify() and synthesize() iterate this tuple so ordering can never
# silently diverge between the two functions.
CLAUSE_SLOTS: tuple[str, ...] = ("precondition", "state", "feature", "trigger")

# Frozen validation message constants. Tests and downstream tooling assert
# against these names, not against string literals.
ERR_SYSTEM_NAME     = "system_name is required"
ERR_SYSTEM_RESPONSE = "system_response is required"
ERR_INVALID_PATTERN = "requirement does not form a legal EARS construction"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(s: str | None) -> str | None:
    """Collapse None / empty / whitespace-only strings to None.

    Called on every slot value before any classification, validation, or
    synthesis logic runs. An LLM emitting '', '  ', or '\t' for an unused
    slot is treated identically to None."""
    if s is None:
        return None
    stripped = s.strip()
    return stripped if stripped else None


def _filled_clauses(req: RequirementCandidate) -> dict[str, str]:
    """Return {slot_name: normalized_value} for clause slots that are non-None
    after normalization. Iterates CLAUSE_SLOTS so order is always canonical."""
    result: dict[str, str] = {}
    for slot in CLAUSE_SLOTS:
        val = _normalize(getattr(req, slot))
        if val is not None:
            result[slot] = val
    return result


def _article(system_name: str) -> str:
    """Return 'the ' unless system_name already starts with 'the ' (case-insensitive).

    Prevents double-article in all sentence templates:
      'The The Payment System shall...'  <-- broken without this guard
      'When X, the The Auth System shall...'  <-- same bug in non-UBIQUITOUS
    Applied in every template that prepends an article before system_name."""
    return "" if system_name.strip().lower().startswith("the ") else "the "


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def classify(req: RequirementCandidate) -> EarsPattern:
    """Classify the requirement's EARS pattern from its normalized slot values.

    COMPLEX is a structural composition label (>=2 clause slots filled), not a
    normative EARS pattern from Mavin et al. (2009). See project_decisions.md [0014].
    """
    sn = _normalize(req.system_name)
    sr = _normalize(req.system_response)

    if sn is None or sr is None:
        return EarsPattern.INVALID

    filled = _filled_clauses(req)
    count = len(filled)

    if count == 0:
        return EarsPattern.UBIQUITOUS
    if count >= 2:
        return EarsPattern.COMPLEX
    # exactly one clause slot filled
    (slot,) = filled.keys()
    return {
        "trigger":      EarsPattern.EVENT,
        "state":        EarsPattern.STATE,
        "precondition": EarsPattern.UNWANTED,
        "feature":      EarsPattern.OPTIONAL,
    }[slot]


def validate(req: RequirementCandidate) -> list[str]:
    """Return a list of error strings describing why the requirement is not valid.

    Empty list means the requirement is valid EARS.

    Check order:
      1. system_name present and non-blank
      2. system_response present and non-blank
      3. If both pass but classify() still returns INVALID — forward-compat gate
         (currently dead code: no input reaches here with floor fields passing;
         retained for future classify() extensions, documented in [0014]).
    """
    errors: list[str] = []
    if _normalize(req.system_name) is None:
        errors.append(ERR_SYSTEM_NAME)
    if _normalize(req.system_response) is None:
        errors.append(ERR_SYSTEM_RESPONSE)
    if not errors and classify(req) is EarsPattern.INVALID:
        errors.append(ERR_INVALID_PATTERN)
    return errors


def synthesize(req: RequirementCandidate) -> str:
    """Build the canonical EARS sentence from the requirement's slots.

    Casing rules:
    - Clause content is preserved exactly (OAuth, iPhone, gRPC, Auth0 unchanged).
    - Only the first character of the whole synthesized sentence is uppercased.
    - system_name is never lowercased.
    - _article() prevents double-article when system_name begins with 'the'.

    'then' rule (EARS UNWANTED pattern, Mavin et al. 2009):
    - 'then' appears when precondition is the SOLE clause slot.
    - In COMPLEX patterns (precondition + other clauses), 'then' is omitted;
      'If X, then while Y...' is non-standard and reads poorly.

    Returns '' for INVALID requirements — stale text is not preserved.
    """
    if classify(req) is EarsPattern.INVALID:
        return ""

    sn: str = _normalize(req.system_name)  # type: ignore[assignment]  # INVALID guard above
    sr: str = _normalize(req.system_response)  # type: ignore[assignment]
    art = _article(sn)
    filled = _filled_clauses(req)
    sole_clause = list(filled.keys())[0] if len(filled) == 1 else None

    parts: list[str] = []
    for slot in CLAUSE_SLOTS:
        val = filled.get(slot)
        if val is None:
            continue
        if slot == "precondition":
            if sole_clause == "precondition":
                parts.append(f"if {val}, then")
            else:
                parts.append(f"if {val},")
        elif slot == "state":
            parts.append(f"while {val},")
        elif slot == "feature":
            parts.append(f"where {val},")
        elif slot == "trigger":
            parts.append(f"when {val},")

    parts.append(f"{art}{sn} shall {sr}.")

    sentence = " ".join(parts)
    return sentence[0].upper() + sentence[1:]


def apply(req: RequirementCandidate) -> RequirementCandidate:
    """Classify, validate, and synthesize in one call. Returns a NEW instance.

    Invariants:
    - Never mutates the input instance.
    - Idempotent: apply(apply(req)) produces the same ears_pattern, ears_valid,
      and text as apply(req).
    - Uses model_validate (not model_copy) so _ears_invariant and all field
      validators are re-run on the result. model_copy uses model_construct and
      would bypass them silently.
    - ears_valid is derived from validate(), not from classify() — validate() is
      the single source of truth for validity. This prevents divergence if
      validate() gains new checks that classify() doesn't cover.
    """
    pattern = classify(req)
    errors  = validate(req)
    text    = synthesize(req)   # "" for INVALID; stale text is never preserved

    data = req.model_dump()
    data.update({
        "ears_pattern": pattern.value,
        "ears_valid":   len(errors) == 0,
        "text":         text,
    })
    return RequirementCandidate.model_validate(data)
